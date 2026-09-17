import json
import re
import sqlite3

from pathlib import Path
from statistics import median


GENERIC = {
    "1080P", "1440P", "2160P", "4K", "8K",
    "HDR10", "HDR10+", "FHD", "UHD", "QHD",
    "100HZ", "120HZ", "144HZ", "165HZ",
    "180HZ", "240HZ",
    "5000MAH", "10000MAH", "20000MAH",
    "22.5W", "25W", "45W", "65W", "100W",
    "128GB", "256GB", "512GB",
    "1TB", "2TB",
    "8GB", "12GB", "16GB",
    "24GB", "32GB",
    "WIFI", "5G", "4G",
}


def normalize(s):
    return str(s or "").upper().replace("–", "-")


def strong_models(text):
    text = normalize(text)

    tokens = re.findall(
        r"[A-Z0-9][A-Z0-9._/#-]{3,}",
        text
    )

    out = []

    for token in tokens:
        token = token.strip("._/#-")

        if len(token) < 5:
            continue

        if token in GENERIC:
            continue

        if token.endswith("MAH"):
            continue

        if re.fullmatch(
            r"\d{3,4}P",
            token
        ):
            continue

        if re.fullmatch(
            r"\d{2,3}HZ",
            token
        ):
            continue

        # CPU/spec patterns, not unique product SKU.
        if re.fullmatch(
            r"\d{1,2}-\d{3,5}[A-Z]{0,3}",
            token
        ):
            continue

        has_letter = any(
            c.isalpha()
            for c in token
        )

        has_digit = any(
            c.isdigit()
            for c in token
        )

        if not (
            has_letter
            and has_digit
        ):
            continue

        out.append(token)

    return sorted(
        set(out),
        key=len,
        reverse=True
    )


def product_key_models(key):
    parts = str(key or "").split("|")

    if len(parts) < 2:
        return []

    # product_key is normally:
    # brand | model(s) | capacities
    model_part = parts[1]

    return strong_models(
        model_part
    )


def capacity_signature(text):
    t = str(text or "").lower()

    vals = []

    for m in re.findall(
        r"\b(64|128|256|512)\s*(?:gb|جيجا|جيجابايت)",
        t
    ):
        vals.append(
            f"{m}GB"
        )

    for m in re.findall(
        r"\b(1|2)\s*(?:tb|تيرا)",
        t
    ):
        vals.append(
            f"{m}TB"
        )

    return set(vals)


def classify(text):
    t = str(text or "").lower()

    # Accessories first.
    accessory_words = (
        "حامل",
        "كفر",
        "جراب",
        "case",
        "cover",
        "stand",
        "متوافق مع",
        "compatible with",
        "screen protector",
        "واقي شاشة",
    )

    if any(x in t for x in accessory_words):
        return "accessory"

    if any(x in t for x in (
        "طابعة",
        "printer",
        "pixma",
        "smart tank",
    )):
        return "printer"

    if any(x in t for x in (
        "لاب توب",
        "لابتوب",
        "laptop",
        "notebook",
        "omnibook",
        "legion",
        "vivobook",
    )):
        return "laptop"

    if any(x in t for x in (
        "شاشة ألعاب",
        "monitor",
        "شاشة كمبيوتر",
    )):
        return "monitor"

    if any(x in t for x in (
        "تلفزيون",
        "tv",
        "television",
    )):
        return "tv"

    if any(x in t for x in (
        "بروجيكتور",
        "projector",
    )):
        return "projector"

    if any(x in t for x in (
        "ماوس",
        "mouse",
    )):
        return "mouse"

    if any(x in t for x in (
        "كيبورد",
        "keyboard",
    )):
        return "keyboard"

    if any(x in t for x in (
        "باور بانك",
        "power bank",
        "powerbank",
    )):
        return "powerbank"

    if any(x in t for x in (
        "سماعة",
        "سماعات",
        "earbuds",
        "headphones",
        "headset",
    )):
        return "audio"

    if (
        "iphone" in t
        or "ايفون" in t
        or "آيفون" in t
    ):
        return "phone"

    if any(x in t for x in (
        "موبايل",
        "هاتف",
        "galaxy",
        "reno",
        "redmi",
        "smartphone",
    )):
        return "phone"

    return "unknown"


def iphone_signature(text):
    t = str(text or "").lower()

    # Never match accessories to phones.
    if classify(text) != "phone":
        return None

    if not (
        "iphone" in t
        or "ايفون" in t
        or "آيفون" in t
    ):
        return None

    m = re.search(
        r"(?:iphone|ايفون|آيفون)\s*(\d{1,2})",
        t
    )

    if not m:
        return None

    generation = m.group(1)

    if (
        "pro max" in t
        or "برو ماكس" in t
    ):
        variant = "PRO_MAX"
    elif (
        "pro" in t
        or "برو" in t
    ):
        variant = "PRO"
    elif (
        "air" in t
        or "اير" in t
        or "إير" in t
    ):
        variant = "AIR"
    else:
        variant = "BASE"

    caps = capacity_signature(t)

    capacity = (
        sorted(caps)[0]
        if len(caps) == 1
        else ""
    )

    return (
        generation,
        variant,
        capacity,
    )


def looks_like_bundle(title, key):
    t = str(title or "")

    key_models = product_key_models(
        key
    )

    bundle_markers = (
        " + ",
        " +",
        "+ ",
        "مع هدية",
        "bundle",
        "باكدج",
    )

    return (
        any(x in t.lower() for x in bundle_markers)
        or len(key_models) >= 2
    )


watch = json.loads(
    Path(
        ".amazon_radar_watch.json"
    ).read_text(
        encoding="utf-8"
    )
)

products = (
    list(watch.values())
    if isinstance(watch, dict)
    else watch
)

con = sqlite3.connect(
    "deals.db"
)

con.row_factory = sqlite3.Row

rows = con.execute("""
    SELECT
        product_key,
        store,
        title,
        current_price,
        seen_at
    FROM market_observations
    WHERE lower(store) IN ('jumia','2b')
    ORDER BY seen_at DESC
""").fetchall()

con.close()

matches = []

for rec in products:

    amazon_title = str(
        rec.get("title", "")
    )

    amazon_price = float(
        rec.get("last_price", 0)
        or 0
    )

    asin = rec.get(
        "asin",
        ""
    )

    amazon_type = classify(
        amazon_title
    )

    amazon_models = set(
        strong_models(
            amazon_title
        )
    )

    amazon_caps = capacity_signature(
        amazon_title
    )

    amazon_iphone = iphone_signature(
        amazon_title
    )

    by_store = {}

    for row in rows:

        store = str(
            row["store"]
        ).lower()

        comp_title = str(
            row["title"]
            or ""
        )

        comp_key = str(
            row["product_key"]
            or ""
        )

        price = float(
            row["current_price"]
            or 0
        )

        if price <= 0:
            continue

        comp_type = classify(
            comp_title
        )

        # Product class must agree whenever both are known.
        if (
            amazon_type != "unknown"
            and comp_type != "unknown"
            and amazon_type != comp_type
        ):
            continue

        # Bundles are not comparable to single products.
        if looks_like_bundle(
            comp_title,
            comp_key
        ):
            continue

        matched = False
        reason = ""

        # ==========================================
        # Exact structured SKU/model match
        # ==========================================

        comp_models = set(
            product_key_models(
                comp_key
            )
        )

        shared = (
            amazon_models
            & comp_models
        )

        if shared:
            model = max(
                shared,
                key=len
            )

            matched = True
            reason = (
                "STRICT_MODEL:"
                + model
            )

        # ==========================================
        # iPhone exact generation+variant+capacity
        # ==========================================

        if not matched:
            comp_iphone = iphone_signature(
                comp_title
            )

            if (
                amazon_iphone
                and comp_iphone
                and amazon_iphone
                    == comp_iphone
                and amazon_iphone[2]
            ):
                matched = True

                reason = (
                    "STRICT_IPHONE:"
                    + "/".join(
                        amazon_iphone
                    )
                )

        if not matched:
            continue

        # If both expose capacity, it must agree.
        comp_caps = capacity_signature(
            comp_title
            + " "
            + comp_key
        )

        if (
            amazon_caps
            and comp_caps
            and not (
                amazon_caps
                & comp_caps
            )
        ):
            continue

        if store not in by_store:
            by_store[store] = {
                "price": price,
                "title": comp_title,
                "reason": reason,
            }

    if not by_store:
        continue

    prices = [
        x["price"]
        for x in by_store.values()
    ]

    market_med = float(
        median(prices)
    )

    gap = 0.0

    if (
        amazon_price > 0
        and amazon_price < market_med
    ):
        gap = (
            (
                market_med
                - amazon_price
            )
            / market_med
            * 100
        )

    matches.append({
        "asin": asin,
        "title": amazon_title,
        "amazon": amazon_price,
        "market": market_med,
        "gap": gap,
        "stores": by_store,
    })


matches.sort(
    key=lambda x: x["gap"],
    reverse=True
)

print(
    "===== STRICT MARKET MATCH V2 ====="
)

print(
    "WATCH =",
    len(products)
)

print(
    "STRICT MATCHES =",
    len(matches)
)

print()

for rec in matches[:30]:

    print(
        rec["asin"],
        "| AMAZON =",
        rec["amazon"],
        "| MARKET =",
        round(
            rec["market"],
            2
        ),
        "| GAP =",
        round(
            rec["gap"],
            1
        ),
        "%"
    )

    print(
        " AMAZON:",
        rec["title"][:90]
    )

    for store, info in rec[
        "stores"
    ].items():

        print(
            " ",
            store.upper(),
            "=",
            info["price"],
            "|",
            info["reason"],
            "|",
            info["title"][:75]
        )

    print(
        "-" * 78
    )
