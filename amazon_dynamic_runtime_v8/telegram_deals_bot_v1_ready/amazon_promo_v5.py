import re
import time

from dynamic_promo_scanner import scan_dynamic_promos
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
    "اشتر", "خصم", "وفر", "توفير", "مجانا", "مجاناً", "كوبون", "قسيمة",
    "بطاقة", "بنك", "برايم", "buy 1", "get 1", "coupon", "save",
    "discount", "prime", "flash sale", "limited time", "member", "offer",
]


def asin_from_url(url):
    text = str(url or "")
    for pattern in (r"/dp/([A-Z0-9]{10})", r"/gp/product/([A-Z0-9]{10})"):
        m = re.search(pattern, text, re.I)
        if m:
            return m.group(1).upper()
    return ""


def _keyword_windows(text):
    low = text.lower()
    windows = []
    for keyword in PROMO_KEYWORDS:
        start = 0
        while True:
            idx = low.find(keyword.lower(), start)
            if idx < 0:
                break
            left = max(0, idx - 100)
            right = min(len(text), idx + 220)
            windows.append(text[left:right])
            start = idx + len(keyword)
            if len(windows) >= 16:
                return windows
    return windows


def _promo_rank(result):
    ranks = {
        "bogo": 100,
        "coupon": 95,
        "quantity_discount": 85,
        "percent": 82,
        "bank_card": 70,
        "card": 70,
        "member": 62,
        "account_offer": 60,
        "flash": 55,
        "bulk_discount": 20,
        "none": 0,
    }
    score = ranks.get(result.get("promo_type", "none"), 0)
    if result.get("promo_scope") == "universal":
        score += 15
    if result.get("promo_verified"):
        score += 5
    if result.get("conditional"):
        score -= 5
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
        if best is None or _promo_rank(result) > _promo_rank(best):
            best = result
    return best


def extract_amazon_promo(soup):
    # Primary path: dynamic scanner discovers Amazon's current promo containers
    # and keeps public vs bank/member/account offers separated.
    try:
        result = scan_dynamic_promos(soup)
        if result.get("promo_type") != "none" or result.get("restricted_offers"):
            result["promo_seen_at"] = int(time.time())
            return result
    except Exception:
        result = None

    # Safe legacy fallback.
    selector_pieces = []
    for selector in PROMO_SELECTORS:
        try:
            for el in soup.select(selector):
                text = el.get_text(" ", strip=True)
                if text:
                    selector_pieces.append(text)
        except Exception:
            pass

    result = _best_promo(selector_pieces, "amazon_selector")

    if result is None:
        fallback_pieces = []
        try:
            fallback_pieces = _keyword_windows(soup.get_text(" ", strip=True))
        except Exception:
            pass
        result = _best_promo(fallback_pieces, "keyword_context")

    if result is None:
        result = {
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
            "details": "",
            "promo_text": "",
            "promo_source": "",
            "public_offers": [],
            "bank_offers": [],
            "member_offers": [],
            "account_offers": [],
            "restricted_offers": [],
            "general_50_plus": False,
        }

    result["promo_seen_at"] = int(time.time())
    return result
