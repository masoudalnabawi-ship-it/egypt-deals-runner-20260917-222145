import re


def _clean(text):
    return " ".join(str(text or "").lower().split())


def analyze_promo(text):
    t = _clean(text)

    out = {
        "promo_type": "none",
        "promo_verified": False,
        "promo_percent": 0.0,
        "coupon_value": 0.0,
        "conditional": False,
        "member_only": False,
        "details": "",
    }

    if not t:
        return out

    # -------------------------
    # BUY 1 GET 1 / 2 FOR 1
    # -------------------------
    bogo_patterns = (
        r"اشتر[ِي]?\s*1.*(?:واحصل|وخد|وخذ).*1.*مجانا",
        r"اشتري\s*واحد.*(?:والثاني|والثانية).*مجانا",
        r"buy\s*1.*get\s*1",
        r"2\s*for\s*1",
        r"2\s*بسعر\s*1",
        r"قطعتين\s*بسعر\s*قطعة",
    )

    if any(re.search(p, t) for p in bogo_patterns):
        out.update({
            "promo_type": "bogo",
            "promo_verified": True,
            "promo_percent": 50.0,
            "details": "Buy 1 Get 1",
        })
        return out

    # -------------------------
    # COUPON VALUE
    # -------------------------
    coupon = re.search(
        r"(?:كوبون|coupon).*?(\d+(?:\.\d+)?)\s*(?:جنيه|egp|le)",
        t
    )

    if coupon:
        out.update({
            "promo_type": "coupon",
            "promo_verified": True,
            "coupon_value": float(coupon.group(1)),
            "conditional": True,
            "details": "Coupon",
        })
        return out

    # -------------------------
    # QUANTITY DISCOUNT
    # -------------------------
    qty = re.search(
        r"(?:اشتر[ِي]?|buy)\s*(\d+).*?"
        r"(?:وفر|save).*?(\d+(?:\.\d+)?)\s*%",
        t
    )

    if qty:
        minimum_quantity = int(qty.group(1))
        discount = float(qty.group(2))

        # Bulk/business quantity offers are real,
        # but should NOT boost the normal consumer deal score.
        if minimum_quantity >= 3:
            promo_type = "bulk_discount"
        else:
            promo_type = "quantity_discount"

        out.update({
            "promo_type": promo_type,
            "promo_verified": True,
            "promo_percent": discount,
            "conditional": True,
            "minimum_quantity": minimum_quantity,
            "bulk_only": minimum_quantity >= 3,
            "details": f"Buy {minimum_quantity}+",
        })
        return out

    # -------------------------
    # BANK / CARD DISCOUNT
    # -------------------------
    card = re.search(
        r"(\d+(?:\.\d+)?)\s*%.*?"
        r"(?:بطاقة|كارت|visa|mastercard|bank|بنك)",
        t
    )

    if card:
        out.update({
            "promo_type": "card",
            "promo_verified": True,
            "promo_percent": float(card.group(1)),
            "conditional": True,
            "details": "Card discount",
        })
        return out

    # -------------------------
    # PRIME / MEMBER ONLY
    # -------------------------
    member_words = (
        "prime",
        "برايم",
        "للأعضاء",
        "members only",
        "member price",
        "noon one",
    )

    if any(x in t for x in member_words):
        pct = re.search(r"(\d+(?:\.\d+)?)\s*%", t)

        out.update({
            "promo_type": "percent" if pct else "member",
            "promo_verified": bool(pct),
            "promo_percent": float(pct.group(1)) if pct else 0.0,
            "conditional": True,
            "member_only": True,
            "details": "Member-only offer",
        })
        return out

    # V7_ARABIC_QUANTITY_PROMOS
    # Handles:
    # خصم 20% لما تشتري 2
    # خصم 20% عند شراء قطعتين أو أكثر
    # اشتر 2 أو أكثر ووفر 5%
    # اشترِ 2 ووفر 20%
    # Buy 2 save/get 15%

    quantity_patterns = [
        # Discount first, quantity second
        r"(?:خصم|وفر)\s*(\d+(?:\.\d+)?)\s*%.*?(?:اشتري|اشتر|شراء|تشتري)\s*(\d+)",

        # Quantity first, discount second
        r"(?:اشتري|اشتر|شراء|تشتري)\s*(\d+).*?(?:وفر|خصم)\s*(\d+(?:\.\d+)?)\s*%",

        # English quantity first
        r"buy\s*(\d+).*?(?:save|get)\s*(\d+(?:\.\d+)?)\s*%",
    ]

    for idx, pattern in enumerate(quantity_patterns):
        m = re.search(pattern, t, re.I)

        if not m:
            continue

        if idx == 0:
            discount = float(m.group(1))
            minimum_quantity = int(m.group(2))
        else:
            minimum_quantity = int(m.group(1))
            discount = float(m.group(2))

        # Buy 2/3/4 = consumer quantity promo.
        # Buy 5+ remains bulk/business style.
        promo_type = (
            "bulk_discount"
            if minimum_quantity >= 5
            else "quantity_discount"
        )

        out.update({
            "promo_type": promo_type,
            "promo_verified": True,
            "promo_percent": discount,
            "conditional": True,
            "minimum_quantity": minimum_quantity,
            "bulk_only": minimum_quantity >= 5,
            "details": (
                f"Buy {minimum_quantity}+ / Save {discount:g}%"
            ),
        })

        return out

    # -------------------------
    # FLASH SALE
    # -------------------------
    if any(x in t for x in (
        "flash sale",
        "عرض فلاش",
        "عرض البرق",
        "لفترة محدودة",
        "limited time deal",
    )):
        out.update({
            "promo_type": "flash",
            "promo_verified": True,
            "details": "Flash sale",
        })
        return out

    # -------------------------
    # GENERIC PERCENT PROMO
    # Weak signal: V5 still needs price/history evidence.
    # -------------------------
    pct = re.search(
        r"(\d+(?:\.\d+)?)\s*%",
        t
    )

    if pct:
        out.update({
            "promo_type": "percent",
            "promo_percent": float(pct.group(1)),
            "promo_verified": False,
            "details": "Advertised percentage only",
        })

    return out
