from __future__ import annotations


DEAL_THRESHOLD = 55


def _num(value):
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _price_score(percent):
    if percent >= 70:
        return 70
    if percent >= 50:
        return 60
    if percent >= 40:
        return 52
    if percent >= 30:
        return 44
    if percent >= 25:
        return 38
    if percent >= 20:
        return 32
    if percent >= 15:
        return 25
    if percent >= 10:
        return 18
    if percent >= 5:
        return 10
    if percent >= 3:
        return 5
    return 0

def _saving_score(saving):
    if saving >= 15000:
        return 28
    if saving >= 10000:
        return 25
    if saving >= 5000:
        return 20
    if saving >= 2500:
        return 16
    if saving >= 1000:
        return 12
    if saving >= 500:
        return 8
    if saving >= 250:
        return 5
    if saving >= 100:
        return 3
    return 0

def _tier(score, critical=False):
    if critical:
        return "CRITICAL"

    if score >= 92:
        return "ULTRA"

    if score >= 82:
        return "VERY_STRONG"

    if score >= 70:
        return "STRONG"

    if score >= DEAL_THRESHOLD:
        return "DEAL"

    return "WATCH"


def evaluate_deal(
    *,
    current_price,
    anchor_price=0,
    anchor_hits=0,
    history_reference=0,
    history_points=0,
    market_median=0,
    market_match_count=0,
    advertised_old_price=0,
    promo_type="none",
    promo_percent=0,
    coupon_value=0,
    promo_verified=False,
    conditional=False,
):
    current = _num(current_price)

    if current <= 0:
        return {
            "tier": "WATCH",
            "score": 0,
            "send": False,
            "reason": ["invalid current price"],
        }

    anchor = _num(anchor_price)
    history = _num(history_reference)
    market = _num(market_median)
    old = _num(advertised_old_price)

    trusted_refs = []
    evidence = []
    evidence_score = 0

    if (
        anchor > current
        and int(anchor_hits or 0) >= 3
    ):
        trusted_refs.append(anchor)
        evidence.append("stable_anchor")
        evidence_score += 8

    if (
        history > current
        and int(history_points or 0) >= 3
    ):
        trusted_refs.append(history)
        evidence.append("price_history")
        evidence_score += 8

    if (
        market > current
        and int(market_match_count or 0) >= 1
    ):
        trusted_refs.append(market)
        evidence.append("market_match")
        evidence_score += 10

        if int(market_match_count or 0) >= 2:
            evidence_score += 4

    weak_old_only = False

    if trusted_refs:
        # Conservative reference:
        # use the lowest trustworthy reference.
        reference = min(trusted_refs)
    elif old > current:
        reference = old
        weak_old_only = True
        evidence.append("advertised_old_price_only")
    else:
        reference = current

    saving = max(0.0, reference - current)

    price_drop = (
        saving / reference * 100
        if reference > current
        else 0.0
    )

    score = (
        _price_score(price_drop)
        + _saving_score(saving)
        + evidence_score
    )

    # A small percentage on an expensive product can still
    # be a genuinely valuable deal.
    if (
        trusted_refs
        and price_drop >= 5
        and saving >= 3000
    ):
        score = max(score, 58)

    if (
        trusted_refs
        and price_drop >= 5
        and saving >= 7500
    ):
        score = max(score, 65)

    promo_type = str(
        promo_type or "none"
    ).strip().lower()

    # Bulk offers such as Buy 5+ are useful metadata,
    # but must NOT change the normal consumer deal score.
    if promo_type == "bulk_discount":
        promo_verified = False
        conditional = False

    promo_equivalent = 0.0
    promo_note = None

    if promo_verified:
        if promo_type in (
            "bogo",
            "buy_1_get_1",
            "buy1get1",
        ):
            promo_equivalent = 50.0
            promo_note = "buy_1_get_1"
            score = max(score, 74)

        elif promo_type in (
            "percent",
            "quantity_discount",
            "buy_more_save",
        ):
            promo_equivalent = max(
                0.0,
                _num(promo_percent)
            )

            promo_note = (
                f"verified_percent_{promo_equivalent:.1f}"
            )

            score += int(
                _price_score(
                    promo_equivalent
                ) * 0.70
            )

        elif promo_type == "coupon":
            coupon = min(
                current,
                _num(coupon_value)
            )

            promo_equivalent = (
                coupon / current * 100
                if current > 0
                else 0
            )

            promo_note = (
                f"verified_coupon_{coupon:.2f}"
            )

            score += int(
                _price_score(
                    promo_equivalent
                ) * 0.75
            )

            score += _saving_score(
                coupon
            )

        elif promo_type in (
            "card",
            "bank_card",
        ):
            promo_equivalent = max(
                0.0,
                _num(promo_percent)
            )

            promo_note = (
                f"conditional_card_{promo_equivalent:.1f}"
            )

            score += int(
                _price_score(
                    promo_equivalent
                ) * 0.55
            )

        elif promo_type == "flash":
            promo_note = "verified_flash_sale"
            score += 5

    if conditional:
        # Still useful, but not available to everybody.
        score -= 6

    deal_percent = max(
        price_drop,
        promo_equivalent
    )

    # Never trust a huge "old price" printed by the store
    # if we have no independent evidence.
    if weak_old_only and not promo_verified:
        score = min(score, 49)

    score = max(
        0,
        min(100, int(round(score)))
    )

    critical = (
        len(trusted_refs) >= 2
        and price_drop >= 65
        and saving >= 1000
    )

    tier = _tier(
        score,
        critical=critical
    )

    verified = (
        bool(trusted_refs)
        or bool(promo_verified)
    )

    promo_type_norm = str(
        promo_type or "none"
    ).strip().lower()

    coupon_percent = (
        (coupon_value / current_price) * 100.0
        if current_price > 0 and coupon_value > 0
        else 0.0
    )

    # USER POLICY:
    # Any REAL verified discount of 5%+ must reach admin review,
    # even when the normal Deal Score is below 55.
    verified_price_5 = (
        bool(trusted_refs)
        and price_drop >= 5.0
    )

    verified_promo_5 = (
        bool(promo_verified)
        and promo_type_norm != "bulk_discount"
        and (
            promo_percent >= 5.0
            or coupon_percent >= 5.0
            or promo_type_norm in ("bogo", "flash")
        )
    )

    five_percent_gate = (
        verified_price_5
        or verified_promo_5
    )

    if five_percent_gate and tier == "WATCH":
        tier = "DEAL"

    bank_only = (
        promo_type_norm in ("card", "bank_card")
        and price_drop < 5.0
    )

    should_send = (
        verified
        and not bank_only
        and (
            five_percent_gate
            or (
                score >= DEAL_THRESHOLD
                and tier != "WATCH"
            )
        )
    )

    reasons = list(evidence)

    if promo_note:
        reasons.append(promo_note)

    if saving > 0:
        reasons.append(
            f"saving={saving:.2f}"
        )

    if deal_percent > 0:
        reasons.append(
            f"deal_percent={deal_percent:.1f}"
        )

    return {
        "tier": tier,
        "score": score,
        "send": should_send,
        "verified": verified,
        "current_price": round(
            current,
            2
        ),
        "reference_price": round(
            reference,
            2
        ),
        "saving": round(
            saving,
            2
        ),
        "price_drop_percent": round(
            price_drop,
            1
        ),
        "promo_equivalent_percent": round(
            promo_equivalent,
            1
        ),
        "deal_percent": round(
            deal_percent,
            1
        ),
        "evidence": evidence,
        "reason": reasons,
        "conditional": bool(
            conditional
        ),
    }
