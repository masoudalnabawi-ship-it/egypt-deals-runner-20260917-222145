import re
import sqlite3
import time

from statistics import median


DB_PATH = "deals.db"

_CACHE_ROWS = []
_CACHE_AT = 0.0
CACHE_SECONDS = 300


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


def strong_models(text):
    text = str(text or "").upper()

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

        if re.fullmatch(r"\d{3,4}P", token):
            continue

        if re.fullmatch(r"\d{2,3}HZ", token):
            continue

        if re.fullmatch(
            r"\d{1,2}-\d{3,5}[A-Z]{0,3}",
            token
        ):
            continue

        if not any(c.isalpha() for c in token):
            continue

        if not any(c.isdigit() for c in token):
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

    return strong_models(parts[1])


def capacities(text):
    t = str(text or "").lower()

    out = set()

    for x in re.findall(
        r"\b(64|128|256|512)\s*(?:gb|جيجا|جيجابايت)",
        t
    ):
        out.add(x + "GB")

    for x in re.findall(
        r"\b(1|2)\s*(?:tb|تيرا)",
        t
    ):
        out.add(x + "TB")

    return out


def classify(text):
    t = str(text or "").lower()

    if any(x in t for x in (
        "حامل", "كفر", "جراب",
        "case", "cover", "stand",
        "متوافق مع", "compatible with",
        "واقي شاشة", "screen protector",
    )):
        return "accessory"

    if any(x in t for x in (
        "طابعة", "printer",
        "pixma", "smart tank",
    )):
        return "printer"

    if any(x in t for x in (
        "لابتوب", "لاب توب",
        "laptop", "notebook",
        "omnibook", "legion",
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
        "television",
        " tv ",
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

    if any(x in t for x in (
        "iphone",
        "ايفون",
        "آيفون",
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
    if classify(text) != "phone":
        return None

    t = str(text or "").lower()

    m = re.search(
        r"(?:iphone|ايفون|آيفون)\s*(\d{1,2})",
        t
    )

    if not m:
        return None

    generation = m.group(1)

    if "pro max" in t or "برو ماكس" in t:
        variant = "PRO_MAX"
    elif "pro" in t or "برو" in t:
        variant = "PRO"
    elif "air" in t or "اير" in t or "إير" in t:
        variant = "AIR"
    else:
        variant = "BASE"

    caps = capacities(t)

    cap = (
        next(iter(caps))
        if len(caps) == 1
        else ""
    )

    return generation, variant, cap


def is_bundle(title, key):
    t = str(title or "").lower()

    return (
        any(x in t for x in (
            " + ",
            "مع هدية",
            "bundle",
            "باكدج",
        ))
        or len(product_key_models(key)) >= 2
    )


def _load_rows():
    global _CACHE_ROWS
    global _CACHE_AT

    now = time.time()

    if (
        _CACHE_ROWS
        and now - _CACHE_AT < CACHE_SECONDS
    ):
        return _CACHE_ROWS

    con = sqlite3.connect(DB_PATH)
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

    _CACHE_ROWS = rows
    _CACHE_AT = now

    return rows


def strict_market_match(title):
    amazon_title = str(title or "")

    amazon_type = classify(amazon_title)
    amazon_models = set(
        strong_models(amazon_title)
    )
    amazon_caps = capacities(
        amazon_title
    )
    amazon_iphone = iphone_signature(
        amazon_title
    )

    by_store = {}

    for row in _load_rows():

        store = str(row["store"]).lower()
        comp_title = str(row["title"] or "")
        comp_key = str(row["product_key"] or "")

        try:
            price = float(
                row["current_price"] or 0
            )
        except Exception:
            continue

        if price <= 0:
            continue

        comp_type = classify(comp_title)

        if (
            amazon_type != "unknown"
            and comp_type != "unknown"
            and amazon_type != comp_type
        ):
            continue

        if is_bundle(
            comp_title,
            comp_key
        ):
            continue

        matched = False
        reason = ""

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
            reason = "MODEL:" + model

        if not matched:
            comp_iphone = iphone_signature(
                comp_title
            )

            if (
                amazon_iphone
                and comp_iphone
                and amazon_iphone == comp_iphone
                and amazon_iphone[2]
            ):
                matched = True
                reason = (
                    "IPHONE:"
                    + "/".join(
                        amazon_iphone
                    )
                )

        if not matched:
            continue

        comp_caps = capacities(
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
                "reason": reason,
                "title": comp_title,
            }

    prices = [
        x["price"]
        for x in by_store.values()
    ]

    return {
        "prices": prices,
        "stores": by_store,
        "median": (
            float(median(prices))
            if prices
            else 0.0
        ),
    }
