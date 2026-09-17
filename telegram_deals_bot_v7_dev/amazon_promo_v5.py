import re
import time

from promo_intelligence_v5 import analyze_promo


PROMO_SELECTORS = [
    "#promotions_feature_div",
    "#promoPriceBlockMessage_feature_div",
    "#couponFeature",
    "#couponText",
    "#instantSavings_feature_div",
    "#primeSavingsMessage",
    "#primeExclusiveMessage",
    "#dealBadge_feature_div",
    ".couponBadge",
    ".promoPriceBlockMessage",
]


PROMO_KEYWORDS = [
    "اشتر",
    "وفر",
    "مجانا",
    "مجاناً",
    "كوبون",
    "بطاقة",
    "بنك",
    "برايم",
    "buy 1",
    "get 1",
    "coupon",
    "save",
    "prime",
    "flash sale",
    "limited time",
]


def asin_from_url(url):
    text = str(url or "")

    patterns = [
        r"/dp/([A-Z0-9]{10})",
        r"/gp/product/([A-Z0-9]{10})",
    ]

    for pattern in patterns:
        m = re.search(
            pattern,
            text,
            re.I
        )

        if m:
            return m.group(1).upper()

    return ""


def _keyword_windows(text):
    low = text.lower()
    windows = []

    for keyword in PROMO_KEYWORDS:
        start = 0

        while True:
            idx = low.find(
                keyword.lower(),
                start
            )

            if idx < 0:
                break

            left = max(0, idx - 90)
            right = min(
                len(text),
                idx + 150
            )

            windows.append(
                text[left:right]
            )

            start = idx + len(keyword)

            if len(windows) >= 10:
                return windows

    return windows


def _promo_rank(result):
    ranks = {
        "bogo": 100,
        "coupon": 95,
        "quantity_discount": 85,
        "card": 80,
        "bank_card": 80,
        "percent": 75,
        "member": 72,
        "flash": 65,
        "bulk_discount": 20,
        "none": 0,
    }

    score = ranks.get(
        result.get("promo_type", "none"),
        0
    )

    if result.get("promo_verified"):
        score += 5

    return score


def _best_promo(pieces, source):
    best = None
    seen = set()

    for piece in pieces:
        piece = " ".join(str(piece).split()).strip()

        if not piece:
            continue

        key = piece.lower()

        if key in seen:
            continue

        seen.add(key)

        result = analyze_promo(piece)

        if result.get("promo_type") == "none":
            continue

        result["promo_text"] = piece[:800]
        result["promo_source"] = source

        if (
            best is None
            or _promo_rank(result) > _promo_rank(best)
        ):
            best = result

    return best


def extract_amazon_promo(soup):
    selector_pieces = []

    for selector in PROMO_SELECTORS:
        try:
            for el in soup.select(selector):
                text = el.get_text(
                    " ",
                    strip=True
                )

                if text:
                    selector_pieces.append(text)
        except Exception:
            pass

    # First trust dedicated Amazon promotion elements.
    result = _best_promo(
        selector_pieces,
        "amazon_selector"
    )

    # Only use page-text fallback when dedicated
    # promotion elements did not produce a promo.
    if result is None:
        fallback_pieces = []

        try:
            page_text = soup.get_text(
                " ",
                strip=True
            )

            fallback_pieces = _keyword_windows(
                page_text
            )
        except Exception:
            pass

        result = _best_promo(
            fallback_pieces,
            "keyword_context"
        )

    if result is None:
        result = {
            "promo_type": "none",
            "promo_verified": False,
            "promo_percent": 0.0,
            "coupon_value": 0.0,
            "conditional": False,
            "member_only": False,
            "details": "",
            "promo_text": "",
            "promo_source": "",
        }

    result["promo_seen_at"] = int(time.time())

    return result

# ===== V7 MULTI-PROMO STACK WRAPPER =====
try:
    from amazon_promo_stack_v7 import legacy_promo_override
    _extract_amazon_promo_v5_base = extract_amazon_promo

    def extract_amazon_promo(soup):
        base = _extract_amazon_promo_v5_base(soup)
        return legacy_promo_override(base, soup)
except Exception:
    pass
