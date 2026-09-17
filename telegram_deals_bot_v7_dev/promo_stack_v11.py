from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Iterable


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class PromoLayer:
    kind: str
    label: str = ""
    percent: float = 0.0
    value: float = 0.0
    cap: float | None = None
    min_spend: float = 0.0
    min_quantity: int = 1
    payment_method: str | None = None
    membership: str | None = None
    account_specific: bool = False
    verified: bool = False
    stackable: bool | None = None
    stack_group: str | None = None
    order: int = 100
    source: str = ""


@dataclass(slots=True)
class PromoContext:
    quantity: int = 1
    payment_method: str | None = None
    memberships: tuple[str, ...] = ()
    account_promos_allowed: bool = False
    checkout_price: float | None = None
    checkout_confirmed: bool = False


def _eligible(layer: PromoLayer, current: float, ctx: PromoContext) -> tuple[bool, str]:
    if not layer.verified:
        return False, "unverified"
    if current < max(0.0, layer.min_spend):
        return False, "min_spend"
    if ctx.quantity < max(1, int(layer.min_quantity or 1)):
        return False, "quantity"
    if layer.payment_method:
        if not ctx.payment_method or layer.payment_method.lower() not in ctx.payment_method.lower():
            return False, "payment_method"
    if layer.membership:
        if not any(layer.membership.lower() == str(x).lower() for x in ctx.memberships):
            return False, "membership"
    if layer.account_specific and not ctx.account_promos_allowed:
        return False, "account_specific"
    return True, "ok"


def _discount_amount(layer: PromoLayer, current: float) -> float:
    if layer.kind in {"percent", "product_percent", "quantity_percent", "bank_percent", "wallet_percent", "membership_percent", "subscribe_save", "bogo"}:
        amount = current * max(0.0, min(100.0, layer.percent)) / 100.0
    elif layer.kind in {"fixed", "coupon_fixed", "voucher_fixed", "task_fixed", "wallet_fixed"}:
        amount = max(0.0, layer.value)
    else:
        amount = 0.0
    if layer.cap is not None and layer.cap >= 0:
        amount = min(amount, layer.cap)
    return min(current, amount)


def evaluate_stack(base_price: float, layers: Iterable[PromoLayer | dict], context: PromoContext | None = None) -> dict:
    """Conservative sequential promo calculator.

    Unknown stackability is NOT treated as permission to stack. Layers in the same
    stack_group compete; explicit stackable=True layers may stack sequentially.
    Checkout-confirmed price, when supplied, is authoritative.
    """
    base = max(0.0, _num(base_price))
    ctx = context or PromoContext()
    parsed: list[PromoLayer] = []
    for item in layers or []:
        if isinstance(item, PromoLayer):
            parsed.append(item)
        elif isinstance(item, dict):
            allowed = {k: v for k, v in item.items() if k in PromoLayer.__dataclass_fields__}
            parsed.append(PromoLayer(**allowed))

    if base <= 0:
        return {
            "base_price": base, "final_price": base, "saving": 0.0,
            "effective_discount_percent": 0.0, "confidence": "none",
            "checkout_authoritative": False, "applied": [], "skipped": [],
            "conditions": [],
        }

    # Checkout is authoritative and does not require reconstructing every promo.
    if ctx.checkout_confirmed and ctx.checkout_price is not None:
        final = min(base, max(0.0, _num(ctx.checkout_price)))
        saving = base - final
        return {
            "base_price": base,
            "final_price": round(final, 2),
            "saving": round(saving, 2),
            "effective_discount_percent": round((saving / base) * 100.0, 1),
            "confidence": "checkout_confirmed",
            "checkout_authoritative": True,
            "applied": [],
            "skipped": [],
            "conditions": ["checkout-confirmed final price"],
        }

    candidates: list[PromoLayer] = []
    skipped: list[dict] = []
    conditions: list[str] = []
    for layer in sorted(parsed, key=lambda x: (x.order, x.label, x.kind)):
        ok, reason = _eligible(layer, base, ctx)
        if not ok:
            skipped.append({"layer": asdict(layer), "reason": reason})
            if reason != "unverified":
                conditions.append(f"{layer.label or layer.kind}: {reason}")
            continue
        candidates.append(layer)

    # Within a group, keep the best discount at the price before that group.
    grouped: dict[str, list[PromoLayer]] = {}
    ungrouped: list[PromoLayer] = []
    for layer in candidates:
        if layer.stack_group:
            grouped.setdefault(layer.stack_group, []).append(layer)
        else:
            ungrouped.append(layer)

    selected: list[PromoLayer] = []
    for group_layers in grouped.values():
        best = max(group_layers, key=lambda x: _discount_amount(x, base))
        selected.append(best)
        for x in group_layers:
            if x is not best:
                skipped.append({"layer": asdict(x), "reason": "exclusive_group"})

    selected.extend(ungrouped)
    selected.sort(key=lambda x: (x.order, x.label, x.kind))

    current = base
    applied: list[dict] = []
    prior_applied = False
    for layer in selected:
        # Unknown/false stackability may be used alone, but never blindly combined.
        if prior_applied and layer.stackable is not True:
            skipped.append({"layer": asdict(layer), "reason": "stackability_not_confirmed"})
            continue
        amount = _discount_amount(layer, current)
        if amount <= 0:
            skipped.append({"layer": asdict(layer), "reason": "zero_effect"})
            continue
        before = current
        current = max(0.0, current - amount)
        applied.append({
            "kind": layer.kind,
            "label": layer.label or layer.kind,
            "before": round(before, 2),
            "discount": round(amount, 2),
            "after": round(current, 2),
            "source": layer.source,
        })
        prior_applied = True

    saving = max(0.0, base - current)
    confidence = "verified_layers" if applied else "none"
    return {
        "base_price": round(base, 2),
        "final_price": round(current, 2),
        "saving": round(saving, 2),
        "effective_discount_percent": round((saving / base) * 100.0, 1),
        "confidence": confidence,
        "checkout_authoritative": False,
        "applied": applied,
        "skipped": skipped,
        "conditions": sorted(set(conditions)),
    }


def from_legacy_item(item: dict) -> PromoLayer:
    t = str(item.get("type") or "").strip().lower()
    mapping = {
        "product_percent": "product_percent",
        "quantity_percent": "quantity_percent",
        "subscribe_save": "subscribe_save",
        "bogo": "bogo",
        "task_fixed": "task_fixed",
        "coupon_fixed": "coupon_fixed",
        "bank_percent": "bank_percent",
    }
    kind = mapping.get(t, t or "unknown")
    pct = _num(item.get("percent"))
    if t == "bogo" and pct <= 0:
        pct = 50.0
    return PromoLayer(
        kind=kind,
        label=str(item.get("label") or t or "promo"),
        percent=pct,
        value=_num(item.get("value")),
        cap=_num(item.get("cap"), -1) if item.get("cap") is not None else None,
        min_spend=_num(item.get("min_spend")),
        min_quantity=int(item.get("minimum_quantity") or 1),
        payment_method=str(item.get("payment_method") or "") or None,
        membership=str(item.get("membership") or "") or None,
        account_specific=bool(item.get("account_specific")),
        verified=bool(item.get("verified")),
        stackable=item.get("stackable") if "stackable" in item else None,
        stack_group=str(item.get("stack_group") or "") or ("primary_percent" if t in {"product_percent", "quantity_percent", "subscribe_save", "bogo"} else None),
        order=int(item.get("order") or 100),
        source=str(item.get("source") or "legacy_page"),
    )


def evaluate_legacy_amazon(base_price: float, stack: Iterable[dict]) -> dict:
    """Compatibility result for amazon_promo_stack_v7.

    Conservative rule: when legacy extraction does not explicitly prove that two
    promos stack, use the single strongest eligible non-bank promo as verified
    effective price. This prevents fake arithmetic stacks while retaining strong
    promo discovery. Richer adapters can pass explicit stackable=True later.
    """
    base = max(0.0, _num(base_price))
    items = list(stack or [])
    layers: list[PromoLayer] = []
    for item in items:
        layer = from_legacy_item(item)
        # Legacy parser did not encode actual stackability. Treat all ordinary
        # extracted promos as competing unless an adapter explicitly says True.
        if "stackable" not in item:
            layer.stackable = False
        if not layer.stack_group and layer.kind not in {"bank_percent"}:
            layer.stack_group = "legacy_unproven_stack"
        elif layer.kind in {"coupon_fixed", "task_fixed"}:
            layer.stack_group = "legacy_unproven_stack"
        layers.append(layer)

    # Legacy task promotions are account-specific; do not call them verified
    # final price without an account/checkout confirmation.
    ctx = PromoContext(quantity=1, account_promos_allowed=False)
    calc = evaluate_stack(base, layers, ctx)

    primary_percent = max([
        _num(x.get("percent")) for x in items
        if x.get("verified") and x.get("type") in {"product_percent", "quantity_percent", "subscribe_save", "bogo"}
    ] or [0.0])
    task_discount = max([_num(x.get("value")) for x in items if x.get("verified") and x.get("type") == "task_fixed"] or [0.0])
    coupon_discount = max([_num(x.get("value")) for x in items if x.get("verified") and x.get("type") == "coupon_fixed"] or [0.0])
    bank_components = [x for x in items if x.get("verified") and x.get("type") == "bank_percent"]
    effective = _num(calc.get("effective_discount_percent"))
    saving = _num(calc.get("saving"))
    lines = [
        f"🏷️ {x['label']}: -{_num(x['discount']):,.2f} جنيه"
        for x in calc.get("applied", [])
    ]
    if saving > 0:
        lines += [
            f"💰 السعر النهائي المؤكد حسابيًا: {_num(calc.get('final_price')):,.2f} جنيه",
            f"🔥 التوفير الفعلي: {saving:,.2f} جنيه ({effective:g}%)",
        ]
    if calc.get("skipped"):
        lines.append("ℹ️ لم يتم جمع عروض لم تثبت قابليتها للتجميع معًا.")
    if task_discount > 0:
        lines.append("⚠️ خصم المهام لم يدخل السعر النهائي بدون تأكيد أهلية الحساب/السلة.")
    if bank_components:
        lines.append("💳 خصم البنك لم يدخل السعر النهائي بدون تأكيد وسيلة الدفع.")

    return {
        "final_price": _num(calc.get("final_price"), base),
        "saving": saving,
        "effective_discount_percent": effective,
        "ultra": bool(effective >= 40.0 or (effective >= 25.0 and saving >= 5000.0)),
        "super_ultra": bool(effective > 60.0),
        "primary_percent": primary_percent,
        "task_discount": task_discount,
        "coupon_discount": coupon_discount,
        "bank_only": bool(bank_components) and not bool(calc.get("applied")),
        "lines": lines,
        "promo_v11": calc,
    }

