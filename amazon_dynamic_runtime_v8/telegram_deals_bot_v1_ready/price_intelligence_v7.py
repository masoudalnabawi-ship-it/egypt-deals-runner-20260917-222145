from __future__ import annotations

from statistics import median

from comparison import product_key
from db import connect


def signal_key(deal):
    return (
        str(getattr(deal, "store", "")).lower(),
        str(getattr(deal, "url", "")),
        str(getattr(deal, "title", "")),
        round(float(getattr(deal, "current_price", 0) or 0), 2),
    )


def _pct_drop(reference, current):
    try:
        reference = float(reference)
        current = float(current)
    except Exception:
        return 0.0
    if reference <= 0 or current <= 0 or current >= reference:
        return 0.0
    return round(((reference - current) / reference) * 100.0, 1)


def _prior_prices(deal, limit=24):
    key = product_key(deal)
    store = str(getattr(deal, "store", "")).lower()
    url = str(getattr(deal, "url", ""))

    with connect() as con:
        rows = con.execute(
            """
            SELECT current_price, seen_at
            FROM market_observations
            WHERE product_key=? AND lower(store)=? AND url=?
            ORDER BY seen_at DESC
            LIMIT ?
            """,
            (key, store, url, int(limit)),
        ).fetchall()

    values = []
    for row in rows:
        try:
            p = float(row["current_price"])
        except Exception:
            continue
        if p > 0:
            values.append(p)
    return values


def _market_groups(market):
    groups = {}
    for deal in market:
        groups.setdefault(product_key(deal), []).append(deal)
    return groups


def build_price_signals(market):
    groups = _market_groups(market)
    result = {}

    for deal in market:
        current = float(getattr(deal, "current_price", 0) or 0)
        direct_old = getattr(deal, "old_price", None)
        direct_discount = float(getattr(deal, "discount_percent", 0) or 0)

        prior = _prior_prices(deal)
        previous = prior[0] if prior else None
        typical = median(prior) if prior else None
        hist_low = min(prior) if prior else None
        hist_count = len(prior)

        history_drop = _pct_drop(typical, current) if typical else 0.0

        others = []
        for other in groups.get(product_key(deal), []):
            if other is deal:
                continue
            if str(getattr(other, "store", "")).lower() == str(getattr(deal, "store", "")).lower():
                continue
            try:
                op = float(getattr(other, "current_price", 0) or 0)
            except Exception:
                continue
            if op > 0:
                others.append(op)

        market_reference = median(others) if others else None
        market_advantage = _pct_drop(market_reference, current) if market_reference else 0.0

        history_verified = hist_count >= 2 and history_drop >= 5.0
        market_verified = bool(others) and market_advantage >= 5.0

        effective = max(
            direct_discount,
            history_drop if history_verified else 0.0,
            market_advantage if market_verified else 0.0,
        )

        reference_price = None
        reference_source = None

        if direct_old and float(direct_old) > current:
            reference_price = float(direct_old)
            reference_source = "store_old_price"
        elif history_verified and typical and typical > current:
            reference_price = float(typical)
            reference_source = "price_history"
        elif market_verified and market_reference and market_reference > current:
            reference_price = float(market_reference)
            reference_source = "market_reference"

        previous_change = None
        direction = "new"
        if previous and previous > 0:
            previous_change = round(((current - previous) / previous) * 100.0, 1)
            if current < previous:
                direction = "down"
            elif current > previous:
                direction = "up"
            else:
                direction = "same"

        saving = (
            max(0.0, float(reference_price) - current)
            if reference_price
            else max(0.0, float(getattr(deal, "saving", 0) or 0))
        )

        is_ultra = (
            effective >= 40.0
            or (effective >= 25.0 and saving >= 5000.0)
            or effective >= 70.0
        )

        is_super_ultra = effective > 60.0

        result[signal_key(deal)] = {
            "history_count": hist_count,
            "previous_price": previous,
            "historical_typical_price": typical,
            "historical_low": hist_low,
            "historical_drop_percent": history_drop,
            "market_reference_price": market_reference,
            "market_advantage_percent": market_advantage,
            "history_verified": history_verified,
            "market_verified": market_verified,
            "effective_discount_percent": round(effective, 1),
            "reference_price": reference_price,
            "reference_source": reference_source,
            "previous_change_percent": previous_change,
            "direction": direction,
            "saving": round(saving, 2),
            "is_ultra": is_ultra,
            "is_super_ultra": is_super_ultra,
        }

    return result


def qualifies_v7(deal, signal):
    direct = float(getattr(deal, "discount_percent", 0) or 0) >= 5.0
    return bool(direct or signal.get("history_verified") or signal.get("market_verified"))


def signal_verified(signal):
    return bool(signal.get("history_verified") or signal.get("market_verified"))


def signal_text(signal):
    lines = []

    previous = signal.get("previous_price")
    change = signal.get("previous_change_percent")
    direction = signal.get("direction")

    if previous and change is not None:
        arrow = "📉" if direction == "down" else ("📈" if direction == "up" else "➡️")
        lines.append(
            f"{arrow} السعر السابق المرصود: {previous:,.2f} ج.م | "
            f"التغير: {change:+.1f}%"
        )

    typical = signal.get("historical_typical_price")
    drop = signal.get("historical_drop_percent") or 0
    if signal.get("history_verified") and typical:
        lines.append(
            f"📚 السعر التاريخي المعتاد: {typical:,.2f} ج.م | "
            f"الانخفاض الحقيقي: {drop:.1f}%"
        )

    market = signal.get("market_reference_price")
    adv = signal.get("market_advantage_percent") or 0
    if signal.get("market_verified") and market:
        lines.append(
            f"🌐 سعر السوق المرجعي: {market:,.2f} ج.م | "
            f"أقل من السوق: {adv:.1f}%"
        )

    return "\n".join(lines)
