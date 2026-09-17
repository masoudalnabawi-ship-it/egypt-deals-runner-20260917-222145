import re

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬", "0123456789.,")


def _clean(text):
    return " ".join(str(text or "").translate(_ARABIC_DIGITS).lower().split())


def _percent(text):
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    if not m:
        return 0.0
    try:
        value = float(m.group(1))
    except Exception:
        return 0.0
    return value if 0 < value <= 100 else 0.0


def _has_any(text, words):
    return any(word in text for word in words)


def analyze_promo(text):
    t = _clean(text)

    out = {
        "promo_type": "none",
        "promo_verified": False,
        "promo_percent": 0.0,
        "coupon_value": 0.0,
        "conditional": False,
        "member_only": False,
        "account_specific": False,
        "bank_only": False,
        "applies_to_all": False,
        "promo_scope": "none",
        "max_discount_only": False,
        "clear_percent_offer": False,
        "details": "",
    }

    if not t:
        return out

    pct = _percent(t)

    bank_words = (
        "بطاقة", "بطاقتك", "كارت", "فيزا", "ماستركارد",
        "بنك", "visa", "mastercard", "bank", "credit card",
        "debit card", "amex", "american express",
    )
    member_words = (
        "prime", "برايم", "للأعضاء", "اعضاء برايم", "أعضاء برايم",
        "members only", "member only", "member price", "membership",
        "noon one",
    )
    account_words = (
        "لحسابات مؤهلة", "لحسابات محددة", "للحسابات المؤهلة",
        "للحسابات المحددة", "خاص بحسابك", "حسابك المؤهل",
        "selected accounts", "eligible accounts", "personalized",
        "selected customers", "eligible customers", "targeted offer",
    )
    up_to_words = (
        "حتى ", "حتى خصم", "خصم حتى", "وفر حتى", "توفير حتى",
        "up to", "save up to", "discount up to",
    )

    has_bank = _has_any(t, bank_words)
    has_member = _has_any(t, member_words)
    has_account = _has_any(t, account_words)
    max_only = _has_any(t, up_to_words)

    # BUY 1 GET 1 / 2 FOR 1
    bogo_patterns = (
        r"اشتر[ِي]?\s*1.*(?:واحصل|وخد|وخذ).*1.*مجانا",
        r"اشتري\s*واحد.*(?:والثاني|والثانية).*مجانا",
        r"buy\s*1.*get\s*1",
        r"2\s*for\s*1",
        r"2\s*بسعر\s*1",
        r"قطعتين\s*بسعر\s*قطعة",
    )
    if any(re.search(p, t) for p in bogo_patterns):
        restricted = has_bank or has_member or has_account
        out.update({
            "promo_type": "bogo",
            "promo_verified": not restricted,
            "promo_percent": 50.0,
            "conditional": restricted,
            "member_only": has_member,
            "account_specific": has_account,
            "bank_only": has_bank,
            "applies_to_all": not restricted,
            "promo_scope": (
                "bank" if has_bank else
                "member" if has_member else
                "account" if has_account else
                "universal"
            ),
            "details": "Buy 1 Get 1",
        })
        return out

    # Bank/card offers are always kept separate from the public price.
    if has_bank and pct:
        out.update({
            "promo_type": "bank_card",
            "promo_verified": True,
            "promo_percent": pct,
            "conditional": True,
            "bank_only": True,
            "promo_scope": "bank",
            "details": "Bank/card discount",
        })
        return out

    # Account-targeted offers are metadata only; they must never become
    # the public effective price.
    if has_account:
        out.update({
            "promo_type": "percent" if pct else "account_offer",
            "promo_verified": bool(pct),
            "promo_percent": pct,
            "conditional": True,
            "account_specific": True,
            "promo_scope": "account",
            "details": "Selected/eligible account offer",
        })
        return out

    # Prime/member-only offers are also isolated from the public price.
    if has_member:
        out.update({
            "promo_type": "percent" if pct else "member",
            "promo_verified": bool(pct),
            "promo_percent": pct,
            "conditional": True,
            "member_only": True,
            "promo_scope": "member",
            "details": "Member-only offer",
        })
        return out

    # Fixed-value coupon.
    coupon = re.search(
        r"(?:كوبون|قسيمة|coupon|voucher).*?(\d+(?:\.\d+)?)\s*(?:جنيه|ج\.م|egp|le)\b",
        t,
    )
    if coupon:
        out.update({
            "promo_type": "coupon",
            "promo_verified": True,
            "coupon_value": float(coupon.group(1)),
            "conditional": True,
            "applies_to_all": True,
            "promo_scope": "coupon",
            "details": "Coupon",
        })
        return out

    # Percentage coupon.
    if pct and _has_any(t, ("كوبون", "قسيمة", "coupon", "voucher")):
        out.update({
            "promo_type": "coupon",
            "promo_verified": True,
            "promo_percent": pct,
            "conditional": True,
            "applies_to_all": True,
            "promo_scope": "coupon",
            "details": "Percentage coupon",
        })
        return out

    # Quantity discounts.
    quantity_patterns = [
        r"(?:خصم|وفر)\s*(\d+(?:\.\d+)?)\s*%.*?(?:اشتري|اشتر|شراء|تشتري)\s*(\d+)",
        r"(?:اشتري|اشتر|شراء|تشتري)\s*(\d+).*?(?:وفر|خصم)\s*(\d+(?:\.\d+)?)\s*%",
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
        promo_type = "bulk_discount" if minimum_quantity >= 5 else "quantity_discount"
        out.update({
            "promo_type": promo_type,
            "promo_verified": True,
            "promo_percent": discount,
            "conditional": True,
            "minimum_quantity": minimum_quantity,
            "bulk_only": minimum_quantity >= 5,
            "promo_scope": "bulk" if minimum_quantity >= 5 else "quantity",
            "details": f"Buy {minimum_quantity}+ / Save {discount:g}%",
        })
        return out

    # Flash sale signal. Percentage handling below can still outrank this
    # in the dynamic scanner when a clear public percentage is present.
    if _has_any(t, (
        "flash sale", "عرض فلاش", "عرض البرق", "لفترة محدودة",
        "limited time deal", "limited-time deal",
    )) and not pct:
        out.update({
            "promo_type": "flash",
            "promo_verified": True,
            "applies_to_all": True,
            "promo_scope": "universal",
            "details": "Flash sale",
        })
        return out

    # Clear public percentage promotion. The parser identifies it, but the
    # DOM scanner decides whether the surrounding Amazon element is trusted
    # enough to mark it verified. This prevents an ordinary price badge from
    # being mistaken for a second checkout promotion.
    direct_patterns = (
        r"(?:خصم|وفر|توفير)\s*(?:بنسبة\s*)?(\d+(?:\.\d+)?)\s*%",
        r"(\d+(?:\.\d+)?)\s*%\s*(?:خصم|توفير)",
        r"(?:save|discount)\s*(?:up\s*to\s*)?(\d+(?:\.\d+)?)\s*%",
    )
    direct = any(re.search(p, t, re.I) for p in direct_patterns)

    if pct and direct:
        out.update({
            "promo_type": "percent",
            "promo_percent": pct,
            "promo_verified": False,
            "conditional": bool(max_only),
            "applies_to_all": not max_only,
            "promo_scope": "range" if max_only else "universal",
            "max_discount_only": bool(max_only),
            "clear_percent_offer": True,
            "details": (
                f"Public percentage promotion up to {pct:g}%"
                if max_only else
                f"Public percentage promotion {pct:g}%"
            ),
        })
        return out

    # Weak generic percentage. Never affects effective price on its own.
    if pct:
        out.update({
            "promo_type": "percent",
            "promo_percent": pct,
            "promo_verified": False,
            "promo_scope": "unknown",
            "details": "Advertised percentage only",
        })

    return out
