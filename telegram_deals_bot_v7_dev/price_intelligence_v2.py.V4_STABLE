from statistics import median


def _prices(values):
    out = []

    for x in values or []:
        try:
            if isinstance(x, dict):
                x = x.get("price")

            x = float(x)

            if x > 0:
                out.append(x)

        except Exception:
            pass

    return out


def robust_history_reference(history):
    values = _prices(history)

    if not values:
        return 0.0, 0, 999.0

    med = median(values)

    if len(values) < 4:
        return med, len(values), 1.0

    deviations = [
        abs(x - med)
        for x in values
    ]

    mad = median(deviations)

    # Remove obvious historical outliers.
    tolerance = max(
        med * 0.20,
        mad * 3.5,
    )

    clean = [
        x
        for x in values
        if abs(x - med) <= tolerance
    ]

    if len(clean) >= 3:
        med = median(clean)
        values = clean

    dispersion = (
        median(
            abs(x - med)
            for x in values
        )
        / med
        if med > 0
        else 999
    )

    return (
        float(med),
        len(values),
        float(dispersion),
    )


def evaluate_price(
    *,
    current,
    history=None,
    market_prices=None,
    last_price=0,
    max_seen=0,
    priority_boost=False,
):
    current = float(current or 0)

    if current <= 0:
        return {
            "tier": "WATCH",
            "confidence": 0,
            "reference": 0,
            "drop": 0,
            "saving": 0,
            "reason": "invalid_current",
        }

    hist_ref, hist_count, dispersion = (
        robust_history_reference(
            history or []
        )
    )

    market = _prices(
        market_prices or []
    )

    market_ref = (
        float(median(market))
        if market
        else 0.0
    )

    reference = 0.0
    basis = []
    confidence = 0

    # -------------------------------------------------
    # Strong history
    # -------------------------------------------------

    if hist_count >= 5:
        reference = hist_ref
        basis.append(
            f"history:{hist_count}"
        )

        confidence += 55

        if dispersion <= 0.05:
            confidence += 15
        elif dispersion <= 0.12:
            confidence += 8

    elif hist_count >= 3:
        reference = hist_ref
        basis.append(
            f"history:{hist_count}"
        )

        confidence += 40

    # -------------------------------------------------
    # Market comparison
    # -------------------------------------------------

    if len(market) >= 2:
        confidence += 20

        if reference > 0:
            ratio = (
                market_ref
                / reference
            )

            if 0.65 <= ratio <= 1.55:
                reference = (
                    reference * 0.75
                    + market_ref * 0.25
                )

                basis.append(
                    f"market:{len(market)}"
                )

                confidence += 5

        else:
            reference = market_ref
            basis.append(
                f"market:{len(market)}"
            )

            confidence += 40

    elif len(market) == 1:
        confidence += 5

    # -------------------------------------------------
    # Last known price is safer than max_seen.
    # -------------------------------------------------

    try:
        last_price = float(
            last_price or 0
        )
    except Exception:
        last_price = 0

    if reference <= 0 and last_price > 0:
        reference = last_price
        basis.append(
            "last_price"
        )
        confidence += 25

    # max_seen is intentionally weak.
    try:
        max_seen = float(
            max_seen or 0
        )
    except Exception:
        max_seen = 0

    if reference <= 0 and max_seen > 0:
        reference = max_seen
        basis.append(
            "max_seen_weak"
        )
        confidence += 15

    if priority_boost:
        confidence += 5

    confidence = min(
        int(confidence),
        95
    )

    if reference <= 0:
        return {
            "tier": "WATCH",
            "confidence": confidence,
            "reference": 0,
            "drop": 0,
            "saving": 0,
            "reason": "no_reference",
        }

    saving = (
        reference
        - current
    )

    if saving <= 0:
        return {
            "tier": "WATCH",
            "confidence": confidence,
            "reference": round(reference, 2),
            "drop": 0,
            "saving": 0,
            "reason": "+".join(basis),
        }

    drop = (
        saving
        / reference
        * 100
    )

    tier = "WATCH"

    # -------------------------------------------------
    # Price-error tiers
    # -------------------------------------------------

    if (
        confidence >= 50
        and drop >= 85
        and saving >= 500
    ):
        tier = "CRITICAL"

    elif (
        confidence >= 65
        and drop >= 70
        and saving >= 1000
    ):
        tier = "CRITICAL"

    elif (
        confidence >= 55
        and drop >= 50
        and saving >= 500
    ):
        tier = "ULTRA"

    elif (
        confidence >= 65
        and drop >= 35
        and saving >= 2000
    ):
        tier = "ULTRA"

    elif (
        confidence >= 60
        and drop >= 25
        and saving >= 500
    ):
        tier = "HOT"

    return {
        "tier": tier,
        "confidence": confidence,
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
        "history_count": hist_count,
        "history_median": round(
            hist_ref,
            2
        ),
        "market_reference": round(
            market_ref,
            2
        ),
        "reason": "+".join(
            basis
        ) or "weak_reference",
    }


if __name__ == "__main__":

    tests = [
        {
            "name":
                "REAL PRICE ERROR",

            "current":
                1300,

            "history": [
                9800,
                10000,
                10100,
                9950,
                10200,
                9900,
            ],

            "market_prices": [
                10100,
                9900,
                10300,
            ],

            "max_seen":
                10500,
        },

        {
            "name":
                "FALSE MAX_SEEN PROTECTION",

            "current":
                4500,

            "history": [
                4900,
                5000,
                5100,
                4950,
                5050,
                5000,
            ],

            "market_prices": [
                5100,
                4950,
            ],

            "max_seen":
                20000,
        },

        {
            "name":
                "STRONG REAL DEAL",

            "current":
                1800,

            "history": [
                3000,
                3100,
                3050,
                2999,
                3150,
                3080,
            ],

            "market_prices": [
                3050,
                3200,
            ],

            "max_seen":
                3300,
        },

        {
            "name":
                "INSUFFICIENT HISTORY",

            "current":
                3000,

            "history": [],

            "market_prices": [],

            "max_seen":
                9000,
        },
    ]

    print(
        "===== PRICE INTELLIGENCE V2 TEST ====="
    )

    for test in tests:
        result = evaluate_price(
            current=test["current"],
            history=test["history"],
            market_prices=test[
                "market_prices"
            ],
            max_seen=test[
                "max_seen"
            ],
        )

        print()
        print(test["name"])
        print(
            "TIER       =",
            result["tier"]
        )
        print(
            "CONFIDENCE =",
            result["confidence"]
        )
        print(
            "REFERENCE  =",
            result["reference"]
        )
        print(
            "DROP       =",
            result["drop"],
            "%"
        )
        print(
            "SAVING     =",
            result["saving"]
        )
        print(
            "BASIS      =",
            result["reason"]
        )
