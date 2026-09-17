
import time
import re
import statistics
import hashlib
import json


def _f(v):
    try:
        return float(v or 0)
    except Exception:
        return 0.0


def record_price_sample(rec, price):
    price = _f(price)
    if price <= 0:
        return

    now = int(time.time())
    hist = rec.setdefault("price_intel_samples", [])

    if not hist or abs(_f(hist[-1].get("p")) - price) >= max(1.0, price * .002):
        hist.append({"t": now, "p": round(price, 2)})

    rec["price_intel_samples"] = hist[-48:]


ACCESSORY_WORDS = (
    "cover", "case", "bag", "stand", "holder", "cable",
    "adapter", "charger", "remote", "spare", "replacement",
    "lubricant", "oil", "belt only",
    "غطاء", "جراب", "حامل", "كابل", "شاحن",
    "قطعة غيار", "زيت", "ريموت",
)

SEMANTIC_FLOORS = (
    (("treadmill", "walking pad", "مشاية كهرب", "جهاز المشي"), 2500, "treadmill"),
    (("air conditioner", "split ac", "تكييف"), 5000, "air-conditioner"),
    (("laptop", "notebook", "لاب توب", "لابتوب"), 3000, "laptop"),
    (("smartphone", "mobile phone", "هاتف ذكي", "موبايل"), 1500, "smartphone"),
    (("refrigerator", "ثلاجة"), 4000, "refrigerator"),
    (("washing machine", "غسالة"), 4000, "washing-machine"),
    (("dishwasher", "غسالة أطباق"), 4500, "dishwasher"),
    (("television", "smart tv", "تلفزيون", "شاشة سمارت"), 1500, "television"),
    (("playstation 5", "ps5", "xbox series"), 5000, "console"),
)


def _semantic_signal(rec, current):
    title = str(rec.get("title") or "").lower()

    if any(x in title for x in ACCESSORY_WORDS):
        return None

    for words, floor, family in SEMANTIC_FLOORS:
        if not any(w in title for w in words):
            continue

        ratio = current / float(floor)

        if ratio <= .20:
            tier = "CRITICAL"
        elif ratio <= .40:
            tier = "ULTRA"
        elif ratio <= .60:
            tier = "HOT"
        else:
            return None

        return {
            "tier": tier,
            "confidence": 0.58,
            "reference": 0,
            "drop": 0,
            "reason": f"semantic cold-start anomaly:{family}",
            "cold_start": True,
            "verified": False,
        }

    return None



def evaluate_shadow(rec, current):
    # UNIVERSAL_VALUE_ANOMALY_V96
    #
    # Universal logic:
    # - no fixed price ceiling
    # - no minimum saving in EGP
    # - not limited to specific product families
    # - works for old products AND cold-start/new products
    #
    # Reference priority:
    # actual observed history / stable anchor
    # exact-model market matches
    # explicit reference price
    # Amazon visible previous/list price
    # semantic floor only as last-resort suspicion

    current = _f(current)

    if current <= 0:
        return None

    from statistics import median

    candidates = []

    def add_ref(
        value,
        basis,
        confidence,
        verified=False,
        historical=False,
    ):
        value = _f(value)

        if value <= current:
            return

        candidates.append({
            "value": value,
            "basis": basis,
            "confidence": int(confidence),
            "verified": bool(verified),
            "historical": bool(historical),
        })

    # ------------------------------------------------------
    # REAL OBSERVED PRICE HISTORY
    # ------------------------------------------------------

    raw_hist = []

    for x in rec.get(
        "price_intel_samples",
        []
    ):
        try:
            if isinstance(x, dict):
                v = _f(
                    x.get("p")
                    or x.get("price")
                )
            else:
                v = _f(x)

            if v > 0:
                raw_hist.append(v)

        except Exception:
            pass

    # record_price_sample() normally stores current last,
    # so exclude one trailing current observation.
    prior_hist = list(raw_hist)

    if prior_hist:
        last = prior_hist[-1]

        if (
            abs(last - current)
            / max(current, 1.0)
            <= 0.01
        ):
            prior_hist = prior_hist[:-1]

    if len(prior_hist) >= 3:
        values = prior_hist[-12:]
        ref = float(median(values))

        add_ref(
            ref,
            "observed_history",
            92,
            verified=True,
            historical=True,
        )

    elif len(prior_hist) == 2:
        ref = float(median(prior_hist))

        add_ref(
            ref,
            "observed_history_2",
            82,
            verified=True,
            historical=True,
        )

    elif len(prior_hist) == 1:
        add_ref(
            prior_hist[0],
            "observed_history_1",
            58,
            verified=False,
            historical=True,
        )

    # ------------------------------------------------------
    # STABLE ANCHOR
    # ------------------------------------------------------

    anchor = max(
        _f(rec.get("stable_anchor")),
        _f(rec.get("intel_anchor_price")),
    )

    anchor_hits = max(
        int(
            rec.get(
                "stable_anchor_hits",
                0
            ) or 0
        ),
        int(
            rec.get(
                "intel_anchor_hits",
                0
            ) or 0
        ),
    )

    if anchor > current:

        if anchor_hits >= 3:
            conf = 92
            verified = True

        elif anchor_hits >= 2:
            conf = 84
            verified = True

        else:
            conf = 58
            verified = False

        add_ref(
            anchor,
            "stable_anchor",
            conf,
            verified=verified,
            historical=True,
        )

    # ------------------------------------------------------
    # EXACT MODEL MARKET REFERENCE
    # Works even when ASIN is completely new to our bot.
    # ------------------------------------------------------

    market_count = 0
    market_ref = 0.0

    try:
        from price_intelligence_bridge import (
            strict_market_match,
        )

        market = strict_market_match(
            rec.get("title", "")
        )

        prices = [
            _f(x)
            for x in (
                market.get("prices")
                or []
            )
            if _f(x) > 0
        ]

        market_count = len(prices)

        if prices:
            market_ref = float(
                median(prices)
            )

            if market_count >= 2:
                add_ref(
                    market_ref,
                    "exact_market_multi",
                    94,
                    verified=True,
                    historical=False,
                )

            else:
                add_ref(
                    market_ref,
                    "exact_market_single",
                    72,
                    verified=False,
                    historical=False,
                )

    except Exception:
        pass

    # ------------------------------------------------------
    # EXPLICIT / AMAZON REFERENCES
    # ------------------------------------------------------

    add_ref(
        rec.get("reference_price"),
        "reference_price",
        68,
        verified=False,
        historical=False,
    )

    amazon_old = _f(
        rec.get("amazon_old_price")
    )

    amazon_old_verified = bool(
        rec.get(
            "amazon_old_price_verified"
        )
    )

    if (
        amazon_old > current
        and amazon_old_verified
    ):
        # Exact Amazon product page confirmed that
        # this reference/old price is currently displayed.
        add_ref(
            amazon_old,
            "amazon_product_page_old_price",
            88,
            verified=True,
            historical=False,
        )

    elif amazon_old > current:
        add_ref(
            amazon_old,
            "amazon_old_price_unverified",
            55,
            verified=False,
            historical=False,
        )

    add_ref(
        rec.get("search_old_price"),
        "amazon_visible_old_price",
        48,
        verified=False,
        historical=False,
    )

    # max_seen is deliberately NOT treated as a strong
    # reference because old/list prices may previously
    # have polluted that field.

    # ------------------------------------------------------
    # NO DYNAMIC REFERENCE:
    # semantic intelligence stays only as LAST fallback.
    # ------------------------------------------------------

    if not candidates:
        # V9.6 CLEAN MODE:
        # Never call a product HOT/ULTRA/CRITICAL from words/category alone.
        # Without a real price reference there is no verified value anomaly.
        return None

    # ------------------------------------------------------
    # CHOOSE BEST REFERENCE BY EVIDENCE QUALITY,
    # NOT SIMPLY THE HIGHEST PRICE.
    # ------------------------------------------------------

    candidates.sort(
        key=lambda x: (
            x["confidence"],
            x["value"],
        ),
        reverse=True,
    )

    best = candidates[0]

    strong = [
        x
        for x in candidates
        if x["confidence"] >= 68
    ]

    reference = best["value"]
    confidence = best["confidence"]
    verified = best["verified"]

    basis = [
        best["basis"]
    ]

    # Multiple good references that agree -> stronger.
    if len(strong) >= 2:
        vals = [
            x["value"]
            for x in strong
        ]

        lo = min(vals)
        hi = max(vals)

        if (
            lo > 0
            and hi / lo <= 1.35
        ):
            reference = float(
                median(vals)
            )

            confidence = min(
                98,
                max(
                    x["confidence"]
                    for x in strong
                ) + 6
            )

            verified = (
                any(
                    x["verified"]
                    for x in strong
                )
                or market_count >= 2
            )

            basis = [
                x["basis"]
                for x in strong
            ]

    if reference <= current:
        return None

    saving = (
        reference
        - current
    )

    drop = (
        saving
        / reference
        * 100.0
    )

    # ------------------------------------------------------
    # UNIVERSAL RELATIVE ANOMALY TIERS
    #
    # NO absolute-EGP saving requirement.
    #
    # 1000 -> 100  = 90% anomaly
    # 150  -> 8    = 94.7% anomaly
    # 22000 -> 131 = 99.4% anomaly
    # ------------------------------------------------------

    tier = None

    if (
        drop >= 85
        and confidence >= 45
    ):
        tier = "CRITICAL"

    elif (
        drop >= 70
        and confidence >= 60
    ):
        tier = "CRITICAL"

    elif (
        drop >= 60
        and confidence >= 45
    ):
        tier = "ULTRA"

    elif (
        drop >= 40
        and confidence >= 45
    ):
        tier = "HOT"

    elif (
        drop >= 30
        and confidence >= 82
    ):
        tier = "HOT"

    if tier is None:
        return None

    has_history = any(
        x.get("historical")
        for x in candidates
        if x["confidence"] >= 58
    )

    cold_start = not has_history

    return {
        "tier": tier,
        "confidence": round(
            confidence / 100.0,
            2
        ),
        "reference": round(
            reference,
            2
        ),
        "drop": round(
            drop,
            1
        ),
        "saving": round(
            saving,
            2
        ),
        "reason": (
            "dynamic value anomaly:"
            + "+".join(basis)
        ),
        "reference_source":
            "+".join(basis),
        "market_count": market_count,
        "market_reference": round(
            market_ref,
            2
        ),
        "cold_start": cold_start,
        "verified": bool(verified),
    }



def _num_field(d, *names):
    for name in names:
        v = _f(d.get(name))
        if v > 0:
            return v
    return 0.0


def compute_promo_stack(live, deal):
    live = _f(live)

    if live <= 0:
        return {
            "authoritative_final_price": 0,
            "best_case_final_price": 0,
            "components": [],
            "stack_verified": False,
            "needs_checkout_confirmation": False,
            "summary": "",
        }

    verified = bool(deal.get("promo_verified", False))
    conditional = bool(deal.get("conditional", False))
    member_only = bool(deal.get("member_only", False))

    explicit_stackable = deal.get("promo_stackable")
    if explicit_stackable is None:
        explicit_stackable = deal.get("stackable")

    components = []
    seen = set()

    def add(kind, mode, value, verified_flag=verified, conditional_flag=False):
        value = _f(value)
        if value <= 0:
            return

        key = (kind, mode, round(value, 2))
        if key in seen:
            return

        seen.add(key)
        components.append({
            "kind": kind,
            "mode": mode,
            "value": round(value, 2),
            "verified": bool(verified_flag),
            "conditional": bool(conditional_flag),
        })

    coupon = _num_field(deal, "coupon_value", "coupon_amount")
    if coupon:
        add("coupon", "fixed", coupon)

    coupon_pct = _num_field(deal, "coupon_percent", "coupon_discount_percent")
    if coupon_pct:
        add("coupon", "percent", coupon_pct)

    task = _num_field(
        deal,
        "task_value",
        "task_discount_value",
        "mission_value",
        "mission_discount_value",
    )
    if task:
        add("task", "fixed", task)

    voucher = _num_field(deal, "voucher_value", "voucher_amount")
    if voucher:
        add("voucher", "fixed", voucher)

    cart = _num_field(deal, "cart_discount_value", "cart_value")
    if cart:
        add("cart", "fixed", cart)

    wallet = _num_field(deal, "wallet_discount_value", "wallet_value")
    if wallet:
        add("wallet", "fixed", wallet)

    bank_pct = _num_field(
        deal,
        "bank_percent",
        "bank_discount_percent",
        "card_discount_percent",
    )
    if bank_pct:
        add("bank", "percent", bank_pct, verified, conditional)

    promo_type = str(deal.get("promo_type") or "").lower()
    promo_percent = _f(deal.get("promo_percent"))

    if promo_type in ("card", "bank_card") and promo_percent:
        add("bank", "percent", promo_percent, verified, conditional)

    elif promo_type == "coupon" and promo_percent:
        add("coupon", "percent", promo_percent)

    elif promo_type == "quantity_discount" and promo_percent:
        add("quantity", "percent", promo_percent, verified, conditional)

    elif promo_type == "member" and promo_percent:
        add("membership", "percent", promo_percent, verified, True)

    # Try to capture explicit task/mission fixed discount in promo text.
    text = str(deal.get("promo_details") or "")

    m = re.search(
        r"(?:task|mission|مهام|مهمة)[^\d]{0,40}(\d+(?:[.,]\d+)?)",
        text,
        re.I,
    )

    if m:
        try:
            add("task", "fixed", float(m.group(1).replace(",", ".")))
        except Exception:
            pass

    usable = [
        x for x in components
        if x["verified"] and not x["conditional"]
    ]

    def apply(price, items):
        price = float(price)

        for x in items:
            if x["mode"] == "fixed":
                price = max(0.0, price - x["value"])
            else:
                pct = max(0.0, min(100.0, x["value"]))
                price = max(0.0, price * (1.0 - pct / 100.0))

        return round(price, 2)

    # If stacking is not explicitly confirmed, never blindly combine them.
    if len(usable) <= 1:
        authoritative = apply(live, usable)
        stack_verified = bool(usable)

    elif explicit_stackable is True:
        authoritative = apply(live, usable)
        stack_verified = True

    else:
        candidates = [apply(live, [x]) for x in usable]
        authoritative = min([live] + candidates)
        stack_verified = False

    # Best-case is informational only.
    best_case = apply(live, usable)

    needs_checkout = (
        len(usable) > 1
        and explicit_stackable is not True
    )

    labels = []

    for x in components:
        value = x["value"]

        if x["mode"] == "percent":
            labels.append(f"{x['kind']} {value:g}%")
        else:
            labels.append(f"{x['kind']} -{value:g} EGP")

    summary = " + ".join(labels)

    if needs_checkout:
        summary += " | stacking needs checkout confirmation"

    return {
        "authoritative_final_price": round(authoritative, 2),
        "best_case_final_price": round(best_case, 2),
        "components": components,
        "stack_verified": stack_verified,
        "needs_checkout_confirmation": needs_checkout,
        "summary": summary,
        "member_only": member_only,
    }
