import time

from price_intelligence_v2 import evaluate_price
from market_matcher_v2 import strict_market_match


TIER_SCORE = {
    "WATCH": 0,
    "HOT": 1,
    "ULTRA": 2,
    "CRITICAL": 3,
}


def _better_tier(a, b):
    if TIER_SCORE.get(b, 0) > TIER_SCORE.get(a, 0):
        return b
    return a


def meaningful_history(samples):
    """
    One observation per 30-minute bucket.
    Prevents 20 identical readings in a few minutes
    from looking like 20 independent historical prices.
    """
    buckets = {}

    for item in samples or []:
        try:
            if isinstance(item, dict):
                price = float(item.get("price", 0))
                ts = int(item.get("ts", 0) or 0)
            else:
                price = float(item)
                ts = 0

            if price <= 0:
                continue

            if ts > 0:
                bucket = ts // 1800
            else:
                bucket = len(buckets)

            buckets[bucket] = {
                "price": price,
                "ts": ts,
            }

        except Exception:
            pass

    return list(buckets.values())[-48:]



def update_stable_anchor(rec, price):
    """
    Keep a price baseline that does NOT immediately follow
    a sudden downward price movement.
    """

    try:
        price = float(price)
        if price <= 0:
            return
    except Exception:
        return

    now = int(time.time())

    anchor = float(
        rec.get(
            "intel_anchor_price",
            0
        )
        or 0
    )

    hits = int(
        rec.get(
            "intel_anchor_hits",
            0
        )
        or 0
    )

    last_confirm = int(
        rec.get(
            "intel_anchor_last_confirm",
            0
        )
        or 0
    )

    # First baseline.
    if anchor <= 0:
        rec["intel_anchor_price"] = price
        rec["intel_anchor_hits"] = 1
        rec["intel_anchor_since"] = now
        rec["intel_anchor_last_confirm"] = now
        return

    ratio = price / anchor

    # Normal/stable price region.
    if 0.92 <= ratio <= 1.08:

        if now - last_confirm >= 300:
            hits += 1

            rec["intel_anchor_hits"] = min(
                hits,
                20
            )

            rec[
                "intel_anchor_last_confirm"
            ] = now

        # Very slow adaptation.
        rec["intel_anchor_price"] = round(
            anchor * 0.90
            + price * 0.10,
            2
        )

        return

    # A sudden DROP must NOT lower the anchor.
    if ratio < 0.92:
        rec["intel_drop_candidate_since"] = (
            rec.get(
                "intel_drop_candidate_since"
            )
            or now
        )
        return

    # Upward price movement:
    # adapt slowly rather than treating an old low price
    # as a permanent baseline.
    if ratio > 1.08:

        up_price = float(
            rec.get(
                "intel_up_candidate_price",
                0
            )
            or 0
        )

        up_hits = int(
            rec.get(
                "intel_up_candidate_hits",
                0
            )
            or 0
        )

        if (
            up_price > 0
            and abs(price - up_price)
                / max(up_price, 1)
                <= 0.05
        ):
            if now - last_confirm >= 300:
                up_hits += 1
        else:
            up_price = price
            up_hits = 1

        rec[
            "intel_up_candidate_price"
        ] = up_price

        rec[
            "intel_up_candidate_hits"
        ] = up_hits

        if up_hits >= 3:
            rec["intel_anchor_price"] = price
            rec["intel_anchor_hits"] = 3
            rec["intel_anchor_since"] = now
            rec[
                "intel_anchor_last_confirm"
            ] = now

            rec.pop(
                "intel_up_candidate_price",
                None
            )

            rec.pop(
                "intel_up_candidate_hits",
                None
            )


def record_price_sample(rec, price):
    try:
        price = float(price)
        if price <= 0:
            return
    except Exception:
        return

    now = int(time.time())

    update_stable_anchor(
        rec,
        price
    )

    samples = rec.get("price_samples", [])
    clean = []

    for item in samples:
        try:
            if isinstance(item, dict):
                p = float(item.get("price", 0))
                ts = int(item.get("ts", 0) or 0)
            else:
                p = float(item)
                ts = 0

            if p > 0:
                clean.append({
                    "price": p,
                    "ts": ts,
                })
        except Exception:
            pass

    if clean:
        last = clean[-1]

        old_price = float(last["price"])
        old_ts = int(last.get("ts", 0) or 0)

        change = (
            abs(price - old_price)
            / max(old_price, 1)
        )

        # Same price: one sample every 30 minutes.
        # A meaningful price change is saved immediately.
        if (
            now - old_ts < 1800
            and change < 0.01
        ):
            rec["price_samples"] = clean[-96:]
            return

    clean.append({
        "price": price,
        "ts": now,
    })

    rec["price_samples"] = clean[-96:]


def evaluate_shadow(rec, current):
    try:
        current = float(current)

        market = strict_market_match(
            rec.get("title", "")
        )

        history = meaningful_history(
            rec.get("price_samples", [])
        )

        result = evaluate_price(
            current=current,
            history=history,
            market_prices=market["prices"],
            last_price=(
                rec.get(
                    "intel_anchor_price",
                    0
                )
                if int(
                    rec.get(
                        "intel_anchor_hits",
                        0
                    )
                    or 0
                ) >= 2
                else 0
            ),
            max_seen=rec.get("max_seen_price", 0),
            priority_boost=(
                int(
                    rec.get(
                        "priority_boost_until",
                        0
                    ) or 0
                )
                > int(time.time())
            ),
        )

        market_count = len(
            market["prices"]
        )

        market_median = float(
            market["median"] or 0
        )

        market_gap = 0.0
        market_saving = 0.0

        if (
            market_median > 0
            and current < market_median
        ):
            market_saving = (
                market_median - current
            )

            market_gap = (
                market_saving
                / market_median
                * 100
            )

        market_tier = "WATCH"
        market_confidence = 0

        # -----------------------------------------
        # ONE exact-model competitor
        # Conservative.
        # -----------------------------------------
        if market_count == 1:

            if (
                market_gap >= 65
                and market_saving >= 2000
            ):
                market_tier = "ULTRA"
                market_confidence = 65

            elif (
                market_gap >= 40
                and market_saving >= 1000
            ):
                market_tier = "HOT"
                market_confidence = 55

        # -----------------------------------------
        # TWO OR MORE exact-market confirmations
        # Much stronger evidence.
        # -----------------------------------------
        elif market_count >= 2:

            if (
                market_gap >= 70
                and market_saving >= 1000
            ):
                market_tier = "CRITICAL"
                market_confidence = 90

            elif (
                market_gap >= 50
                and market_saving >= 500
            ):
                market_tier = "ULTRA"
                market_confidence = 85

            elif (
                market_gap >= 30
                and market_saving >= 500
            ):
                market_tier = "HOT"
                market_confidence = 75

        result["tier"] = _better_tier(
            result.get("tier", "WATCH"),
            market_tier
        )

        result["confidence"] = max(
            int(result.get("confidence", 0)),
            market_confidence
        )

        result["history_count"] = len(
            history
        )

        anchor = float(
            rec.get(
                "intel_anchor_price",
                0
            )
            or 0
        )

        anchor_hits = int(
            rec.get(
                "intel_anchor_hits",
                0
            )
            or 0
        )

        anchor_gap = 0.0
        anchor_saving = 0.0

        if (
            anchor > 0
            and current < anchor
        ):
            anchor_saving = (
                anchor - current
            )

            anchor_gap = (
                anchor_saving
                / anchor
                * 100
            )

        result[
            "anchor_price"
        ] = anchor

        result[
            "anchor_hits"
        ] = anchor_hits

        result[
            "anchor_gap"
        ] = round(
            anchor_gap,
            1
        )

        # Stable Amazon baseline alone:
        # useful, but deliberately conservative.
        if anchor_hits >= 3:

            if (
                anchor_gap >= 75
                and anchor_saving >= 1500
            ):
                result["tier"] = _better_tier(
                    result.get(
                        "tier",
                        "WATCH"
                    ),
                    "ULTRA"
                )

                result["confidence"] = max(
                    int(
                        result.get(
                            "confidence",
                            0
                        )
                    ),
                    70
                )

            elif (
                anchor_gap >= 45
                and anchor_saving >= 750
            ):
                result["tier"] = _better_tier(
                    result.get(
                        "tier",
                        "WATCH"
                    ),
                    "HOT"
                )

                result["confidence"] = max(
                    int(
                        result.get(
                            "confidence",
                            0
                        )
                    ),
                    60
                )

        # Stable Amazon baseline + exact market match.
        # Two independent price references.
        if (
            anchor_hits >= 2
            and anchor > 0
            and market_median > 0
        ):
            agreement = (
                market_median
                / anchor
            )

            if 0.80 <= agreement <= 1.25:

                combined_ref = (
                    anchor
                    + market_median
                ) / 2

                combined_saving = (
                    combined_ref
                    - current
                )

                combined_gap = (
                    combined_saving
                    / combined_ref
                    * 100
                    if combined_ref > 0
                    else 0
                )

                if (
                    combined_gap >= 70
                    and combined_saving >= 1000
                ):
                    result["tier"] = (
                        "CRITICAL"
                    )

                    result["confidence"] = max(
                        int(
                            result.get(
                                "confidence",
                                0
                            )
                        ),
                        90
                    )

                elif (
                    combined_gap >= 50
                    and combined_saving >= 750
                ):
                    result["tier"] = _better_tier(
                        result.get(
                            "tier",
                            "WATCH"
                        ),
                        "ULTRA"
                    )

                    result["confidence"] = max(
                        int(
                            result.get(
                                "confidence",
                                0
                            )
                        ),
                        85
                    )

                elif (
                    combined_gap >= 30
                    and combined_saving >= 500
                ):
                    result["tier"] = _better_tier(
                        result.get(
                            "tier",
                            "WATCH"
                        ),
                        "HOT"
                    )

                    result["confidence"] = max(
                        int(
                            result.get(
                                "confidence",
                                0
                            )
                        ),
                        75
                    )

        result["market_count"] = market_count
        result["market_median"] = market_median
        result["market_gap"] = round(
            market_gap,
            1
        )
        result["market_saving"] = round(
            market_saving,
            2
        )
        result["market_stores"] = list(
            market["stores"].keys()
        )

        result["reason"] = (
            str(result.get("reason", ""))
            + "|STRICT_MARKET:"
            + ",".join(result["market_stores"])
            if market_count
            else str(result.get("reason", ""))
        )

        return result

    except Exception as exc:
        return {
            "tier": "ERROR",
            "error": repr(exc),
        }
