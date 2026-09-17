"""Dynamic Amazon promotion discovery.

The scanner uses the already-downloaded product page. It does not make an
extra Amazon request. It deliberately separates public promotions from bank,
member and account-targeted offers so restricted discounts never become the
public effective price.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from promo_intelligence_v5 import analyze_promo


PROMO_TERMS = (
    "خصم", "وفر", "توفير", "عرض", "اشتر", "شراء", "كوبون", "قسيمة",
    "مجانا", "مجاناً", "بطاقة", "بنك", "برايم", "discount", "save",
    "offer", "promotion", "promo", "coupon", "voucher", "buy", "prime",
    "bank", "visa", "mastercard", "member", "checkout",
)

TRUSTED_SELECTORS = (
    "#promotions_feature_div",
    "#promoPriceBlockMessage_feature_div",
    "#quickPromoBucketContent",
    "#couponFeature",
    "#couponText",
    "#instantSavings_feature_div",
    "#primeSavingsMessage",
    "#primeExclusiveMessage",
    "#dealBadge_feature_div",
    ".couponBadge",
    ".promoPriceBlockMessage",
)

SEMANTIC_SELECTORS = (
    "[id*='promo']", "[class*='promo']", "[id*='coupon']", "[class*='coupon']",
    "[id*='offer']", "[class*='offer']", "[id*='deal']", "[class*='deal']",
    "[data-csa-c-content-id*='promo']", "[data-csa-c-content-id*='coupon']",
)

PRICE_BADGE_MARKERS = (
    "savingspercentage", "priceblocksavings", "basisprice", "a-text-price",
    "priceblockstrike", "discount-badge", "savingbadge",
)


@dataclass
class Candidate:
    text: str
    source: str
    trust: int
    attrs: str = ""


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _contains_promo_term(text: str) -> bool:
    low = text.lower()
    return any(term in low for term in PROMO_TERMS)


def _candidate_key(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()[:600]


def _node_attrs(node) -> str:
    try:
        classes = " ".join(node.get("class", []) or [])
        return f"{node.get('id', '')} {classes}".lower()
    except Exception:
        return ""


def _is_price_badge(attrs: str) -> bool:
    attrs = str(attrs or "").lower()
    return any(marker in attrs for marker in PRICE_BADGE_MARKERS)


def _add(candidates, seen, text, source, trust, attrs=""):
    text = _clean(text)
    if not (3 <= len(text) <= 1500):
        return
    if not _contains_promo_term(text) and "%" not in text:
        return
    key = _candidate_key(text)
    if key in seen:
        return
    seen.add(key)
    candidates.append(Candidate(text=text, source=source, trust=trust, attrs=attrs))


def _rank(result):
    scope = result.get("promo_scope", "none")
    ptype = result.get("promo_type", "none")
    verified = bool(result.get("promo_verified"))
    percent = float(result.get("promo_percent", 0) or 0)

    base = {
        "bogo": 95,
        "percent": 82,
        "coupon": 78,
        "quantity_discount": 68,
        "flash": 60,
        "bank_card": 45,
        "member": 40,
        "account_offer": 38,
        "bulk_discount": 20,
        "none": 0,
    }.get(ptype, 25)

    if scope == "universal":
        base += 20
    if verified:
        base += 12
    if result.get("conditional"):
        base -= 8
    if result.get("member_only") or result.get("account_specific") or result.get("bank_only"):
        base -= 8
    base += min(10, int(percent // 10))
    return base


def _promote(candidate: Candidate, parsed: dict) -> dict:
    result = dict(parsed)
    result["promo_text"] = candidate.text[:800]
    result["promo_source"] = candidate.source
    result["promo_dom_trust"] = candidate.trust

    # Trusted dedicated/semantic promo containers can verify a clear public
    # percentage. Plain page text is kept as a signal only.
    if (
        result.get("promo_type") == "percent"
        and result.get("promo_scope") == "universal"
        and result.get("clear_percent_offer")
        and not result.get("max_discount_only")
        and candidate.trust >= 2
        and not _is_price_badge(candidate.attrs)
    ):
        result["promo_verified"] = True
        result["conditional"] = False
        result["applies_to_all"] = True
        result["verification_source"] = candidate.source

    # A price badge may describe the ordinary Amazon price drop. Keep it as
    # metadata but never turn it into an additional checkout promotion.
    if _is_price_badge(candidate.attrs) and result.get("promo_type") == "percent":
        result["promo_verified"] = False
        result["price_badge_only"] = True

    scope = str(result.get("promo_scope") or "")
    public_coupon = (
        result.get("promo_type") == "coupon"
        and scope == "coupon"
        and not result.get("member_only")
        and not result.get("account_specific")
        and not result.get("bank_only")
    )
    result["general_50_plus"] = bool(
        result.get("promo_verified")
        and scope in ("universal", "coupon")
        and (not result.get("conditional") or public_coupon)
        and (
            float(result.get("promo_percent", 0) or 0) >= 50
            or result.get("promo_type") == "bogo"
        )
    )
    result["public_coupon_50_plus"] = bool(
        public_coupon
        and float(result.get("promo_percent", 0) or 0) >= 50
        and result.get("promo_verified")
    )
    return result


def _unique_offers(items, limit=8):
    out = []
    seen = set()
    for item in sorted(items, key=_rank, reverse=True):
        sig = (
            item.get("promo_scope"), item.get("promo_type"),
            round(float(item.get("promo_percent", 0) or 0), 2),
            round(float(item.get("coupon_value", 0) or 0), 2),
            item.get("promo_text", "")[:120].lower(),
        )
        if sig in seen:
            continue
        seen.add(sig)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def scan_dynamic_promos(soup):
    candidates = []
    seen = set()

    # High-trust Amazon promotion containers.
    for selector in TRUSTED_SELECTORS:
        try:
            for node in soup.select(selector):
                attrs = _node_attrs(node)
                pieces = list(node.stripped_strings)
                _add(
                    candidates, seen,
                    " ".join(pieces),
                    f"trusted:{selector}", 3, attrs,
                )
                for piece in pieces[:20]:
                    _add(
                        candidates, seen, piece,
                        f"trusted-child:{selector}", 3, attrs,
                    )
        except Exception:
            pass

    # Dynamic Amazon experiments often rename containers while preserving
    # semantic id/class fragments such as promo/coupon/offer/deal.
    for selector in SEMANTIC_SELECTORS:
        try:
            for node in soup.select(selector)[:80]:
                attrs = _node_attrs(node)
                pieces = list(node.stripped_strings)
                _add(
                    candidates, seen,
                    " ".join(pieces),
                    f"semantic:{selector}", 2, attrs,
                )
                for piece in pieces[:20]:
                    _add(
                        candidates, seen, piece,
                        f"semantic-child:{selector}", 2, attrs,
                    )
        except Exception:
            pass

    # Last-resort short lines. These are never enough by themselves to verify
    # a public percentage; they help retain restricted-offer metadata.
    try:
        for line in soup.get_text("\n", strip=True).splitlines():
            text = _clean(line)
            if len(text) <= 500 and _contains_promo_term(text):
                _add(candidates, seen, text, "page_line", 1, "")
            if len(candidates) >= 180:
                break
    except Exception:
        pass

    offers = []
    for candidate in candidates:
        parsed = analyze_promo(candidate.text)
        if parsed.get("promo_type") == "none":
            continue
        offers.append(_promote(candidate, parsed))

    offers = _unique_offers(offers, limit=24)
    public = _unique_offers([
        x for x in offers
        if x.get("promo_scope") in ("universal", "coupon", "quantity")
        and not x.get("member_only")
        and not x.get("account_specific")
        and not x.get("bank_only")
    ])
    bank = _unique_offers([x for x in offers if x.get("promo_scope") == "bank"])
    member = _unique_offers([x for x in offers if x.get("promo_scope") == "member"])
    account = _unique_offers([x for x in offers if x.get("promo_scope") == "account"])
    bulk = _unique_offers([x for x in offers if x.get("promo_scope") == "bulk"])

    best_public = max(public, key=_rank) if public else None
    restricted = bank + member + account
    best_restricted = max(restricted, key=_rank) if restricted else None
    primary = best_public or best_restricted or (max(offers, key=_rank) if offers else None)

    result = dict(primary or {
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
    })

    result.update({
        "dynamic_scan_version": "V1",
        "public_offers": public,
        "bank_offers": bank,
        "member_offers": member,
        "account_offers": account,
        "bulk_offers": bulk,
        "restricted_offers": restricted,
        "general_50_plus": bool(best_public and best_public.get("general_50_plus")),
    })

    # Best public offer should be the primary deal signal even if a restricted
    # offer advertises a larger percentage.
    if best_public:
        for key, value in best_public.items():
            result[key] = value
        result["public_offers"] = public
        result["bank_offers"] = bank
        result["member_offers"] = member
        result["account_offers"] = account
        result["bulk_offers"] = bulk
        result["restricted_offers"] = restricted
        result["general_50_plus"] = bool(best_public.get("general_50_plus"))
        result["dynamic_scan_version"] = "V1"

    return result
