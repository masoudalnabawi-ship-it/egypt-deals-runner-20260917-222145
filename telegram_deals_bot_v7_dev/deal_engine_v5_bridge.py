from deal_engine_v5 import evaluate_deal

try:
    from market_matcher_v2 import strict_market_match
except Exception:
    strict_market_match = None


def evaluate_record_v5(rec, current_price):
    title = str(rec.get("title", "") or "")

    anchor = float(
        rec.get("intel_anchor_price", 0)
        or 0
    )

    anchor_hits = int(
        rec.get("intel_anchor_hits", 0)
        or 0
    )

    market_median = 0.0
    market_count = 0
    market_stores = []

    if strict_market_match is not None:
        try:
            market = strict_market_match(title) or {}

            market_median = float(
                market.get("median", 0)
                or 0
            )

            stores = market.get("stores", {}) or {}

            if isinstance(stores, dict):
                market_stores = list(stores.keys())
                market_count = len(market_stores)

            elif isinstance(stores, (list, tuple, set)):
                market_stores = list(stores)
                market_count = len(market_stores)

        except Exception:
            pass

    advertised_old = float(
        rec.get("old_price", 0)
        or rec.get("advertised_old_price", 0)
        or 0
    )

    original_promo_type = str(
        rec.get("promo_type", "none")
        or "none"
    ).lower()

    original_promo_percent = float(
        rec.get("promo_percent", 0)
        or 0
    )

    original_promo_verified = bool(
        rec.get("promo_verified", False)
    )

    direct_old = float(
        rec.get("amazon_old_price", 0)
        or 0
    )

    direct_verified = bool(
        rec.get(
            "amazon_old_price_verified",
            False
        )
    )

    direct_percent = 0.0

    if (
        direct_verified
        and current_price > 0
        and direct_old > current_price
        and direct_old <= current_price * 10
    ):
        direct_percent = (
            (direct_old - current_price)
            / direct_old
            * 100.0
        )

    # Amazon's own visible old/current price becomes primary
    # verified evidence when the direct drop is 5%+.
    if direct_percent >= 5.0:
        advertised_old = direct_old

        engine_promo_type = "amazon_price_drop"
        engine_promo_percent = direct_percent
        engine_promo_verified = True
        engine_conditional = False
    else:
        engine_promo_type = original_promo_type
        engine_promo_percent = original_promo_percent
        engine_promo_verified = original_promo_verified
        engine_conditional = bool(
            rec.get("conditional", False)
        )

    result = evaluate_deal(
        current_price=current_price,
        anchor_price=anchor,
        anchor_hits=anchor_hits,
        market_median=market_median,
        market_match_count=market_count,
        advertised_old_price=advertised_old,

        promo_type=engine_promo_type,
        promo_percent=engine_promo_percent,

        coupon_value=rec.get(
            "coupon_value",
            0
        ),

        promo_verified=engine_promo_verified,
        conditional=engine_conditional,
    )

    result["market_stores"] = market_stores
    result["market_median"] = market_median
    result["asin"] = rec.get("asin")
    result["title"] = title

    result["promo_type"] = engine_promo_type
    result["promo_percent"] = engine_promo_percent
    result["coupon_value"] = rec.get("coupon_value", 0)
    result["promo_verified"] = engine_promo_verified
    result["conditional"] = engine_conditional
    result["member_only"] = bool(rec.get("member_only", False))
    result["promo_details"] = rec.get("promo_details", "")
    result["promo_text"] = rec.get("promo_text", "")
    result["promo_source"] = rec.get("promo_source", "")

    result["amazon_old_price"] = (
        direct_old
        if direct_percent >= 5.0
        else 0.0
    )

    result["amazon_direct_discount"] = direct_percent
    result["image_url"] = rec.get("image_url", "")
    result["title_ar"] = rec.get("title_ar", "")

    # Keep bank/Prime/etc. as secondary info when a real
    # Amazon price drop is the reason the deal was admitted.
    if (
        direct_percent >= 5.0
        and original_promo_type
        not in ("none", "amazon_price_drop")
    ):
        result["secondary_promo_type"] = original_promo_type
        result["secondary_promo_percent"] = original_promo_percent
        result["secondary_promo_verified"] = original_promo_verified
    else:
        result["secondary_promo_type"] = "none"
        result["secondary_promo_percent"] = 0.0
        result["secondary_promo_verified"] = False

    return result
