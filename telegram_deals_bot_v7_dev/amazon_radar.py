
try:
    from price_intelligence_bridge import (
        record_price_sample,
        evaluate_shadow,
    )
except Exception:
    def record_price_sample(rec, price):
        return None

    def evaluate_shadow(rec, current):
        return None

import asyncio
import hashlib
import json
import os
import re
import time
import random
from types import SimpleNamespace
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup
from amazon_campaign_v7 import extract_campaign_targets
from rapidfuzz import fuzz
from dotenv import load_dotenv

from engine import run_store
from db import connect
from stores.base import parse_price
from ultra_speed_v7 import semantic_price_sanity
from flash_review_v9 import build_flash_review_card

load_dotenv(".env")

ROOT = Path(__file__).resolve().parent
WATCH_FILE = ROOT / ".amazon_radar_watch.json"
STATE_FILE = ROOT / ".amazon_radar_state.json"

RAW_BASE = os.environ["CLOUD_API_URL"].rstrip("/")
if RAW_BASE.endswith("/api/deals"):
    CLOUD_BASE = RAW_BASE[:-len("/api/deals")]
else:
    CLOUD_BASE = RAW_BASE

API_KEY = os.environ["CLOUD_API_KEY"]

AMAZON = "https://www.amazon.eg"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 14) "
        "AppleWebKit/537.36 "
        "Chrome/140.0 Mobile Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "ar-EG,ar;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
}

API_HEADERS = {
    "x-api-key": API_KEY,
    "Accept": "application/json",
    "User-Agent": "EgyptDealsAmazonRadar/1.0",
}

# مصادر بحث مستقلة عن الـscanner العادي
KEYWORDS = [
    "iphone",
    "samsung galaxy",
    "xiaomi",
    "redmi",
    "oppo",
    "realme",
    "laptop",
    "gaming laptop",
    "ssd",
    "nvme",
    "headphones",
    "earbuds",
    "power bank",
    "smart watch",
    "playstation",
    "air fryer",
    "smart tv",
    "monitor",
]

CATEGORIES = [
    "electronics",
    "computers",
    "mobile-phones",
    "videogames",
    "appliances",
]

SOURCE_URLS = (
    [
        f"{AMAZON}/s?i={x}"
        for x in CATEGORIES
    ]
    +
    [
        f"{AMAZON}/s?k={quote_plus(x)}"
        for x in KEYWORDS
    ]
)

HIGH_VALUE_WORDS = (
    "iphone",
    "galaxy",
    "samsung",
    "xiaomi",
    "redmi",
    "oppo",
    "realme",
    "laptop",
    "ssd",
    "nvme",
    "playstation",
    "television",
    "smart tv",
    "monitor",
    "air fryer",
    "ساعة",
    "موبايل",
    "هاتف",
    "لاب توب",
    "تلفزيون",
    "شاشة",
)

lock = asyncio.Lock()


def load_json(path, default):
    try:
        return json.loads(
            path.read_text(encoding="utf-8")
        )
    except Exception:
        return default


watch = load_json(WATCH_FILE, {})
state = load_json(
    STATE_FILE,
    {
        "source_index": 0,
        "hot_index": 0,
    }
)


def save_files():
    WATCH_FILE.write_text(
        json.dumps(
            watch,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    STATE_FILE.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )


def to_float(v):
    try:
        return float(v)
    except Exception:
        return 0.0


def asin_url(asin):
    return f"{AMAZON}/dp/{asin}"


def parse_search(html):
    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    found = []

    for card in soup.select(
        "div.s-result-item[data-asin]"
    ):
        asin = (
            card.get("data-asin")
            or ""
        ).strip()

        if not asin:
            continue

        title_el = card.select_one(
            "h2 span"
        )

        price_el = card.select_one(
            ".a-price .a-offscreen"
        )

        if not title_el or not price_el:
            continue

        current = parse_price(
            price_el.get_text(
                " ",
                strip=True
            )
        )

        if not current or current <= 0:
            continue

        old = None

        old_el = card.select_one(
            ".a-price.a-text-price .a-offscreen"
        )

        if old_el:
            old = parse_price(
                old_el.get_text(
                    " ",
                    strip=True
                )
            )

        if old is not None and old <= current:
            old = None

        found.append({
            "asin": asin,
            "title": title_el.get_text(
                " ",
                strip=True
            ),
            "url": asin_url(asin),
            "current_price": float(current),
            "old_price": (
                float(old)
                if old
                else None
            ),
        })

    return found


def add_product(item, source):
    asin = item.get("asin")

    if not asin:
        url = item.get("url", "")
        parts = url.split("/dp/")

        if len(parts) > 1:
            asin = (
                parts[1]
                .split("/")[0]
                .split("?")[0]
            )

    if not asin:
        return

    current = to_float(
        item.get("current_price")
    )

    if current <= 0:
        return

    old = to_float(
        item.get("old_price")
    )

    now = int(time.time())

    rec = watch.get(
        asin,
        {
            "asin": asin,
            "title": item.get(
                "title",
                ""
            ),
            "url": asin_url(asin),
            "first_seen": now,
            "last_seen": now,
            "last_price": current,
            "max_seen_price": current,
            "min_seen_price": current,
            "sources": [],
        }
    )

    rec["title"] = (
        item.get("title")
        or rec.get("title")
        or ""
    )

    rec["url"] = asin_url(asin)
    rec["last_seen"] = now
    rec["last_price"] = current

    rec["max_seen_price"] = max(
        to_float(
            rec.get("max_seen_price")
        ),
        current,
        old,
    )

    previous_min = to_float(
        rec.get("min_seen_price")
    )

    if previous_min <= 0:
        previous_min = current

    rec["min_seen_price"] = min(
        previous_min,
        current
    )

    sources = set(
        rec.get("sources", [])
    )

    sources.add(source)

    rec["sources"] = sorted(sources)

    watch[asin] = rec




# =========================================================
# AMAZON SMART NETWORK GOVERNOR
# =========================================================

AMAZON_FETCH_CACHE = {}
AMAZON_FAIL_UNTIL = {}
AMAZON_INFLIGHT = {}

AMAZON_GATE = asyncio.Lock()

AMAZON_LAST_REQUEST_AT = 0.0
AMAZON_503_EVENTS = []

AMAZON_CIRCUIT_UNTIL = 0.0
AMAZON_RECOVERY_UNTIL = 0.0

AMAZON_CIRCUIT_SECONDS = 150
AMAZON_RECOVERY_SECONDS = 75

# Normal traffic:
# one Amazon request approximately every 1.15 sec.
AMAZON_NORMAL_GAP = 1.15

# After a block has just recovered:
# intentionally slower for a short period.
AMAZON_RECOVERY_GAP = 2.25

# Smart progressive recovery
AMAZON_SUCCESS_STREAK = 0
AMAZON_LAST_503_AT = 0.0

# Amazon Search has a much stricter limit than product pages.
# Keep its health completely separate from Ultra/Hot.
AMAZON_SEARCH_503_STREAK = 0
AMAZON_SEARCH_BACKOFF_UNTIL = 0.0

# Hard protection: 403/429/CAPTCHA means STOP, not bypass.
AMAZON_HARD_BLOCK_UNTIL = 0.0
AMAZON_HARD_BLOCK_SECONDS = 300
AMAZON_HARD_BLOCK_EVENTS = []

# Persist hard cooldown across process restarts so Ctrl+C/restart
# can never bypass Amazon's safety pause.
AMAZON_HARD_BLOCK_STATE_FILE = ROOT / ".amazon_hard_block_state.json"

def _load_persisted_hard_block_until():
    try:
        data = json.loads(
            AMAZON_HARD_BLOCK_STATE_FILE.read_text(
                encoding="utf-8"
            )
        )
        return float(data.get("until_epoch", 0) or 0)
    except Exception:
        return 0.0

AMAZON_HARD_BLOCK_UNTIL_EPOCH = (
    _load_persisted_hard_block_until()
)

def _persist_amazon_hard_block():
    try:
        AMAZON_HARD_BLOCK_STATE_FILE.write_text(
            json.dumps(
                {
                    "until_epoch":
                        AMAZON_HARD_BLOCK_UNTIL_EPOCH,
                    "updated_at":
                        int(time.time()),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass

CAPTCHA_MARKERS = (
    "robot check",
    "enter the characters you see below",
    "type the characters you see in this image",
    "/errors/validatecaptcha",
    "api-services-support@amazon.com",
)


def amazon_captcha_page(text):
    low = str(text or "").lower()
    return any(marker in low for marker in CAPTCHA_MARKERS)


def note_amazon_hard_block(reason, status=None):
    global AMAZON_HARD_BLOCK_UNTIL
    global AMAZON_HARD_BLOCK_UNTIL_EPOCH
    global AMAZON_HARD_BLOCK_EVENTS
    global AMAZON_SUCCESS_STREAK

    now = time.monotonic()
    now_epoch = time.time()

    AMAZON_SUCCESS_STREAK = 0

    AMAZON_HARD_BLOCK_UNTIL = max(
        AMAZON_HARD_BLOCK_UNTIL,
        now + AMAZON_HARD_BLOCK_SECONDS,
    )

    AMAZON_HARD_BLOCK_UNTIL_EPOCH = max(
        AMAZON_HARD_BLOCK_UNTIL_EPOCH,
        now_epoch + AMAZON_HARD_BLOCK_SECONDS,
    )

    _persist_amazon_hard_block()

    AMAZON_HARD_BLOCK_EVENTS = [
        x
        for x in AMAZON_HARD_BLOCK_EVENTS
        if now - x.get("at", now) <= 3600
    ]

    AMAZON_HARD_BLOCK_EVENTS.append(
        {
            "at": now,
            "reason": str(reason),
            "status": status,
        }
    )

    print(
        "🛑 AMAZON HARD BLOCK PROTECTION",
        "| REASON =",
        reason,
        "| STATUS =",
        status,
        "| PAUSE =",
        AMAZON_HARD_BLOCK_SECONDS,
        "sec",
        flush=True,
    )




def amazon_priority_url(url):
    """
    Priority traffic means:
      - exact product URL
      - exact ASIN search

    Ultra/Hot price checks use these URLs.
    Discovery/model searches are secondary.
    """
    u = str(url or "")

    if "amazon.eg" not in u:
        return True

    if "/dp/" in u:
        return True

    if "/s?k=" in u:
        q = (
            u.split("/s?k=", 1)[1]
            .split("&", 1)[0]
            .strip()
            .upper()
        )

        if re.fullmatch(r"[A-Z0-9]{10}", q):
            return True

    return False


def amazon_hard_block_active():
    return (
        time.monotonic() < AMAZON_HARD_BLOCK_UNTIL
        or time.time() < AMAZON_HARD_BLOCK_UNTIL_EPOCH
    )


def amazon_circuit_open():
    now = time.monotonic()
    return (
        now < AMAZON_CIRCUIT_UNTIL
        or amazon_hard_block_active()
    )


def amazon_circuit_remaining():
    # Includes legacy circuit + in-memory hard block + persisted hard block.
    now = time.monotonic()
    now_epoch = time.time()

    remaining = max(
        0.0,
        AMAZON_CIRCUIT_UNTIL - now,
        AMAZON_HARD_BLOCK_UNTIL - now,
        AMAZON_HARD_BLOCK_UNTIL_EPOCH - now_epoch,
    )

    if remaining <= 0:
        return 0

    return max(1, int(remaining + 0.999))


def amazon_recovering():
    now = time.monotonic()

    return (
        not amazon_circuit_open()
        and now < AMAZON_RECOVERY_UNTIL
    )



def amazon_secondary_allowed():
    # Ultra gets absolute priority during startup/recovery.
    # Discovery/competitors resume after clean successes.
    return (
        not amazon_circuit_open()
        and AMAZON_SUCCESS_STREAK >= 8
        and not amazon_recovering()
    )



def amazon_search_backoff_active():
    return (
        time.monotonic()
        < AMAZON_SEARCH_BACKOFF_UNTIL
    )


def note_amazon_search_503(url):
    global AMAZON_SEARCH_503_STREAK
    global AMAZON_SEARCH_BACKOFF_UNTIL

    AMAZON_SEARCH_503_STREAK += 1

    # Independent exponential backoff for Search only.
    delays = (90, 300, 600, 900)

    delay = delays[
        min(
            AMAZON_SEARCH_503_STREAK - 1,
            len(delays) - 1
        )
    ]

    now = time.monotonic()

    AMAZON_SEARCH_BACKOFF_UNTIL = max(
        AMAZON_SEARCH_BACKOFF_UNTIL,
        now + delay
    )

    # Quarantine this exact query for 10 minutes.
    AMAZON_FAIL_UNTIL[str(url)] = max(
        AMAZON_FAIL_UNTIL.get(
            str(url),
            0
        ),
        now + 600
    )

    print(
        "🔎 AMAZON SEARCH BACKOFF",
        "| STREAK =",
        AMAZON_SEARCH_503_STREAK,
        "| PAUSE =",
        delay,
        "sec",
        flush=True
    )


def note_amazon_search_200():
    global AMAZON_SEARCH_503_STREAK
    global AMAZON_SEARCH_BACKOFF_UNTIL

    if AMAZON_SEARCH_503_STREAK:
        print(
            "✅ AMAZON SEARCH HEALTHY AGAIN",
            flush=True
        )

    AMAZON_SEARCH_503_STREAK = 0
    AMAZON_SEARCH_BACKOFF_UNTIL = 0.0


def note_amazon_503():
    global AMAZON_503_EVENTS
    global AMAZON_CIRCUIT_UNTIL
    global AMAZON_RECOVERY_UNTIL
    global AMAZON_SUCCESS_STREAK
    global AMAZON_LAST_503_AT

    now = time.monotonic()

    AMAZON_SUCCESS_STREAK = 0
    AMAZON_LAST_503_AT = now

    AMAZON_503_EVENTS = [
        t
        for t in AMAZON_503_EVENTS
        if now - t <= 25
    ]

    AMAZON_503_EVENTS.append(now)

    count = len(AMAZON_503_EVENTS)

    if count >= 3:
        AMAZON_CIRCUIT_UNTIL = max(
            AMAZON_CIRCUIT_UNTIL,
            now + AMAZON_CIRCUIT_SECONDS
        )

        AMAZON_RECOVERY_UNTIL = (
            AMAZON_CIRCUIT_UNTIL
            + AMAZON_RECOVERY_SECONDS
        )

        print(
            "⛔ AMAZON CIRCUIT OPEN",
            "| 503/25s =",
            count,
            "| PAUSE =",
            AMAZON_CIRCUIT_SECONDS,
            "sec",
            flush=True
        )

    else:
        print(
            "🛡️ AMAZON 503 WARNING",
            "| COUNT =",
            count,
            flush=True
        )


def note_amazon_200():
    global AMAZON_503_EVENTS
    global AMAZON_SUCCESS_STREAK
    global AMAZON_RECOVERY_UNTIL

    now = time.monotonic()

    AMAZON_503_EVENTS = [
        t
        for t in AMAZON_503_EVENTS
        if now - t <= 25
    ]

    AMAZON_SUCCESS_STREAK += 1

    if AMAZON_SUCCESS_STREAK in (1, 3, 8, 12):
        print(
            "🚦 AMAZON HEALTH",
            "| SUCCESS STREAK =",
            AMAZON_SUCCESS_STREAK,
            flush=True
        )

    # 12 clean requests = return to full speed early
    if AMAZON_SUCCESS_STREAK >= 12:
        if AMAZON_RECOVERY_UNTIL > now:
            print(
                "✅ AMAZON RECOVERED -> FULL SPEED",
                flush=True
            )

        AMAZON_RECOVERY_UNTIL = 0.0


def invalidate_amazon_cache(asin):
    asin = str(asin or "").upper()

    if not asin:
        return

    for mapping in (
        AMAZON_FETCH_CACHE,
        AMAZON_FAIL_UNTIL,
    ):
        for key in list(mapping.keys()):
            if asin in str(key).upper():
                mapping.pop(key, None)



async def amazon_wait_for_slot():
    global AMAZON_LAST_REQUEST_AT

    now = time.monotonic()

    # First clean requests after startup/block:
    # 0-2 successes  -> 2.8 sec
    # 3-7 successes  -> 1.8 sec
    # 8+ successes   -> 1.15 sec
    #
    # This quickly reaches maximum speed without a burst.
    if AMAZON_SUCCESS_STREAK < 3:
        gap = 2.80
    elif AMAZON_SUCCESS_STREAK < 8:
        gap = 1.80
    else:
        gap = AMAZON_NORMAL_GAP

    wait = (
        AMAZON_LAST_REQUEST_AT
        + gap
        - now
    )

    # Positive jitter only slows request timing; it never makes it faster.
    wait = max(0.0, wait) + random.uniform(0.05, 0.22)
    if wait > 0:
        await asyncio.sleep(wait)

    AMAZON_LAST_REQUEST_AT = time.monotonic()


async def _raw_fetch(client, url):
    is_amazon = (
        "amazon.eg"
        in str(url)
    )

    if not is_amazon:
        try:
            r = await client.get(
                url,
                headers=HEADERS,
                timeout=8,
                follow_redirects=True
            )

            if r.status_code == 200:
                return r.text

            print(
                "SOURCE HTTP",
                r.status_code,
                url[:80],
                flush=True
            )

        except Exception as e:
            print(
                "SOURCE ERROR",
                url[:80],
                "|",
                repr(e),
                flush=True
            )

        return ""

    # Absolutely no Amazon request while circuit is open.
    if amazon_circuit_open():
        return ""

    async with AMAZON_GATE:

        # Circuit may have opened while waiting.
        if amazon_circuit_open():
            return ""

        await amazon_wait_for_slot()

        try:
            r = await client.get(
                url,
                headers=HEADERS,
                timeout=25,
                follow_redirects=True
            )

            if r.status_code == 200:
                if amazon_captcha_page(r.text):
                    note_amazon_hard_block(
                        "captcha_or_robot_check",
                        status=200,
                    )
                    return ""

                # AMAZON_UNEXPECTED_STATUS_GUARD
                # A normal Amazon HTML page is not empty/tiny.
                # Treat an empty or tiny 200 as a protection signal,
                # never as a clean success.
                body = str(r.text or "")
                if len(body.strip()) < 500:
                    note_amazon_hard_block(
                        "empty_or_tiny_http_200",
                        status=200,
                    )
                    return ""

                if amazon_priority_url(url):
                    note_amazon_200()
                else:
                    note_amazon_search_200()

                return body

            # V8.3 one-request canary observed HTTP 202 with an
            # empty body from amazon.eg. Treat it as a protection
            # warning: stop and cool down, never retry immediately.
            if r.status_code == 202:
                note_amazon_hard_block(
                    "unexpected_http_202",
                    status=202,
                )
                return ""

            if r.status_code in (403, 429):
                note_amazon_hard_block("http_block_or_rate_limit", status=r.status_code)

            if r.status_code == 503:
                if amazon_priority_url(url):
                    # Product/ASIN monitoring problem:
                    # this can affect the global circuit.
                    note_amazon_503()
                else:
                    # Discovery/model-search problem only.
                    # NEVER slow Ultra because of this.
                    note_amazon_search_503(url)

            print(
                "SOURCE HTTP",
                r.status_code,
                url[:80],
                flush=True
            )

        except Exception as e:
            print(
                "SOURCE ERROR",
                repr(e),
                flush=True
            )

    return ""


async def fetch_html(client, url):
    url = str(url)
    now = time.monotonic()

    is_amazon = (
        "amazon.eg"
        in url
    )

    if not is_amazon:
        return await _raw_fetch(
            client,
            url
        )

    priority = amazon_priority_url(
        url
    )

    # No requests during hard circuit.
    if amazon_circuit_open():
        return ""

    # During recovery Ultra/ASIN checks only.
    if (
        not amazon_secondary_allowed()
        and not priority
    ):
        return ""


    fail_until = AMAZON_FAIL_UNTIL.get(
        url,
        0
    )

    if now < fail_until:
        return ""

    cached = AMAZON_FETCH_CACHE.get(
        url
    )

    if cached:
        cached_at, html = cached

        if now - cached_at <= 7:
            return html

    existing = AMAZON_INFLIGHT.get(
        url
    )

    if existing is not None:
        try:
            return await existing
        except Exception:
            return ""

    async def work():
        html = await _raw_fetch(
            client,
            url
        )

        if html:
            AMAZON_FETCH_CACHE[url] = (
                time.monotonic(),
                html
            )

            if len(AMAZON_FETCH_CACHE) > 1200:
                oldest = sorted(
                    AMAZON_FETCH_CACHE.items(),
                    key=lambda x: x[1][0]
                )[:250]

                for key, _ in oldest:
                    AMAZON_FETCH_CACHE.pop(
                        key,
                        None
                    )

        else:
            AMAZON_FAIL_UNTIL[url] = max(
                AMAZON_FAIL_UNTIL.get(
                    url,
                    0
                ),
                time.monotonic() + 15
            )

        return html

    task = asyncio.create_task(
        work()
    )

    AMAZON_INFLIGHT[url] = task

    try:
        return await task
    finally:
        if AMAZON_INFLIGHT.get(url) is task:
            AMAZON_INFLIGHT.pop(
                url,
                None
            )





# =========================================================
# AMAZON PROMO INTELLIGENCE V5
# Uses the SAME downloaded product page.
# NO extra Amazon request.
# =========================================================

try:
    from amazon_promo_v5 import (
        asin_from_url,
        extract_amazon_promo,
    )
except Exception:
    asin_from_url = None
    extract_amazon_promo = None


AMAZON_PROMO_CACHE = {}
AMAZON_PRODUCT_META_CACHE = {}



def cache_amazon_product_meta(url, soup):
    if asin_from_url is None:
        return

    try:
        asin = asin_from_url(url)

        if not asin:
            return

        old_price = 0.0

        old_selectors = [
            ".basisPrice .a-offscreen",
            ".a-text-price .a-offscreen",
            "#priceBlockStrikePriceString",
            "#corePriceDisplay_desktop_feature_div .basisPrice .a-offscreen",
            "span[data-a-strike='true'] .a-offscreen",
        ]

        for selector in old_selectors:
            try:
                for el in soup.select(selector):
                    v = parse_price(
                        el.get_text(" ", strip=True)
                    )

                    if v and v > old_price:
                        old_price = float(v)
            except Exception:
                pass

        image_url = ""

        image = soup.select_one(
            "#landingImage, #imgBlkFront"
        )

        if image:
            for attr in (
                "data-old-hires",
                "src",
            ):
                value = str(
                    image.get(attr, "") or ""
                ).strip()

                if value.startswith("http"):
                    image_url = value
                    break

            if not image_url:
                try:
                    dynamic = json.loads(
                        image.get(
                            "data-a-dynamic-image",
                            "{}"
                        )
                    )

                    if isinstance(dynamic, dict) and dynamic:
                        image_url = next(
                            iter(dynamic.keys())
                        )
                except Exception:
                    pass

        if not image_url:
            og = soup.select_one(
                'meta[property="og:image"]'
            )

            if og:
                image_url = str(
                    og.get("content", "") or ""
                ).strip()

        page_title = ""

        title_el = soup.select_one("#productTitle")

        if title_el:
            page_title = title_el.get_text(
                " ",
                strip=True
            )

        # Only call it Arabic when actual Arabic letters exist.
        title_ar = (
            page_title
            if re.search(r"[\u0600-\u06FF]", page_title)
            else ""
        )

        AMAZON_PRODUCT_META_CACHE[asin] = {
            "old_price": old_price,
            "image_url": image_url,
            "title_ar": title_ar,
            "page_title": page_title,
            "seen_at": int(time.time()),
        }

        if len(AMAZON_PRODUCT_META_CACHE) > 1500:
            oldest = sorted(
                AMAZON_PRODUCT_META_CACHE.items(),
                key=lambda x: int(
                    x[1].get("seen_at", 0) or 0
                )
            )[:300]

            for key, _ in oldest:
                AMAZON_PRODUCT_META_CACHE.pop(
                    key,
                    None
                )

    except Exception as exc:
        print(
            "AMAZON META ERROR:",
            repr(exc),
            flush=True
        )


# ===== V7 CONDITION META WRAPPER =====
_cache_amazon_product_meta_base = cache_amazon_product_meta

def cache_amazon_product_meta(url, soup):
    _cache_amazon_product_meta_base(url, soup)

    try:
        from amazon_promo_stack_v7 import condition_from_soup, offer_key

        asin = asin_from_url(url) if asin_from_url else None
        if not asin:
            return

        meta = AMAZON_PRODUCT_META_CACHE.get(asin)
        if not isinstance(meta, dict):
            return

        condition = condition_from_soup(soup)
        meta["condition"] = condition
        meta["offer_key"] = offer_key(asin, condition)
        meta["is_resale"] = condition != "new"
    except Exception:
        pass

def cache_amazon_promo(url, soup):
    if (
        asin_from_url is None
        or extract_amazon_promo is None
    ):
        return

    try:
        asin = asin_from_url(url)

        if not asin:
            return

        promo = extract_amazon_promo(
            soup
        )

        AMAZON_PROMO_CACHE[
            asin
        ] = promo

        if promo.get("promo_type") != "none":
            print(
                "🎁 PROMO DETECTED",
                asin,
                "| TYPE =", promo.get("promo_type"),
                "| VERIFIED =", promo.get("promo_verified"),
                "| PERCENT =", promo.get("promo_percent"),
                "| COUPON =", promo.get("coupon_value"),
                "| CONDITIONAL =", promo.get("conditional"),
                "| DETAILS =", promo.get("details"),
                flush=True
            )

        # Keep cache bounded.
        if len(AMAZON_PROMO_CACHE) > 1500:
            oldest = sorted(
                AMAZON_PROMO_CACHE.items(),
                key=lambda x: int(
                    x[1].get(
                        "promo_seen_at",
                        0
                    )
                    or 0
                )
            )[:300]

            for key, _ in oldest:
                AMAZON_PROMO_CACHE.pop(
                    key,
                    None
                )

    except Exception:
        pass


def v5_record_with_promo(rec):
    work = dict(rec)

    asin = str(
        rec.get("asin", "")
        or ""
    ).upper()

    promo = AMAZON_PROMO_CACHE.get(
        asin
    )

    if promo:
        work.update({
            "promo_type":
                promo.get("promo_type", "none"),

            "promo_verified":
                bool(
                    promo.get(
                        "promo_verified",
                        False
                    )
                ),

            "promo_percent":
                promo.get(
                    "promo_percent",
                    0
                ),

            "coupon_value":
                promo.get(
                    "coupon_value",
                    0
                ),

            "conditional":
                bool(
                    promo.get(
                        "conditional",
                        False
                    )
                ),

            "promo_details":
                promo.get(
                    "details",
                    ""
                ),

            "promo_text":
                promo.get(
                    "promo_text",
                    ""
                ),
        })


    meta = AMAZON_PRODUCT_META_CACHE.get(
        asin
    )

    if meta:
        old_price = to_float(
            meta.get("old_price")
        )

        if old_price > 0:
            work["amazon_old_price"] = old_price
            work["amazon_old_price_verified"] = True

        image_url = str(
            meta.get("image_url", "") or ""
        )

        if image_url:
            work["image_url"] = image_url

        title_ar = str(
            meta.get("title_ar", "") or ""
        )

        if title_ar:
            work["title_ar"] = title_ar

    return work


# ===== V7 RECORD STACK WRAPPER =====
_v5_record_with_promo_base = v5_record_with_promo

def v5_record_with_promo(rec):
    work = _v5_record_with_promo_base(rec)

    try:
        asin = str(rec.get("asin", "") or "").upper()
        promo = AMAZON_PROMO_CACHE.get(asin) or {}

        for key in (
            "promo_stack",
            "task_discount_value",
            "stack_effective_price",
            "stack_effective_discount_percent",
            "stack_ultra",
            "stack_super_ultra",
            "stack_bank_only",
            "stack_lines",
        ):
            if key in promo:
                work[key] = promo.get(key)

        if promo.get("stack_lines"):
            work["promo_details"] = " | ".join(
                str(x) for x in promo.get("stack_lines") or []
            )

        meta = AMAZON_PRODUCT_META_CACHE.get(asin) or {}
        condition = str(meta.get("condition") or "new")
        work["condition"] = condition
        work["offer_key"] = str(
            meta.get("offer_key")
            or (asin + "|" + condition)
        )
        work["is_resale"] = bool(meta.get("is_resale", False))

    except Exception:
        pass

    return work

async def live_price(client, url):
    """
    DIRECT PRODUCT PAGE ONLY.

    Important:
    Continuous monitoring NEVER uses Amazon Search.
    This avoids /s?k=ASIN rate limits and keeps latency low.
    """

    html = await fetch_html(
        client,
        url
    )

    if not html:
        return None

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    # Extract promotions from THIS SAME PAGE.
    # No additional HTTP request.
    cache_amazon_promo(
        url,
        soup
    )

    cache_amazon_product_meta(
        url,
        soup
    )

    selectors = [
        ".priceToPay .a-offscreen",
        ".apexPriceToPay .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
        "#corePrice_feature_div .a-price .a-offscreen",
        "#apex_desktop .a-price .a-offscreen",
        "#desktop_unifiedPrice .a-price .a-offscreen",
        "#tp_price_block_total_price_ww .a-offscreen",
        "#newAccordionRow_1 .a-price .a-offscreen",
        "#price .a-price .a-offscreen",
        "#price_inside_buybox",
        "#newBuyBoxPrice",
        "#priceblock_dealprice",
        "#priceblock_ourprice",
        "span.a-price[data-a-size='xl'] .a-offscreen",
        "span.a-price[data-a-size='l'] .a-offscreen",
    ]

    for selector in selectors:
        el = soup.select_one(selector)

        if not el:
            continue

        price = parse_price(
            el.get_text(
                " ",
                strip=True
            )
        )

        if price and price > 0:
            return float(price)

    # JSON-LD fallback from the SAME product page.
    # No second Amazon request.
    for script in soup.select(
        'script[type="application/ld+json"]'
    ):
        text = script.string or script.get_text()

        if not text:
            continue

        try:
            data = json.loads(text)
        except Exception:
            continue

        objects = (
            data
            if isinstance(data, list)
            else [data]
        )

        for obj in objects:
            if not isinstance(obj, dict):
                continue

            offers = obj.get("offers")

            if isinstance(offers, dict):
                price = to_float(
                    offers.get("price")
                )

                if price > 0:
                    return float(price)

            elif isinstance(offers, list):
                for offer in offers:
                    if not isinstance(offer, dict):
                        continue

                    price = to_float(
                        offer.get("price")
                    )

                    if price > 0:
                        return float(price)

    # Embedded Amazon data fallback.
    # Still from the SAME downloaded page.
    patterns = [
        r'"priceAmount"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
        r'"price"\s*:\s*"([0-9]+(?:\.[0-9]+)?)"',
    ]

    for pattern in patterns:
        m = re.search(
            pattern,
            html
        )

        if not m:
            continue

        price = to_float(
            m.group(1)
        )

        if price > 0:
            return float(price)

    return None


async def cloud_observations(items):
    if not items:
        return

    payload = []

    for item in items:
        payload.append({
            "product_key":
                "amazon-asin:"
                + item["asin"]
                + "|condition:"
                + str(item.get("condition") or "new"),

            "store": "amazon",
            "title": item["title"],
            "current_price":
                item["current_price"],

            "old_price":
                item.get("old_price"),

            "url": item["url"],
        })

    async with httpx.AsyncClient() as client:
        for i in range(
            0,
            len(payload),
            50
        ):
            chunk = payload[i:i + 50]

            try:
                await client.post(
                    CLOUD_BASE
                    + "/api/observations",
                    json={
                        "observations": chunk
                    },
                    headers=API_HEADERS,
                    timeout=30
                )
            except Exception:
                pass


def is_hot(rec):
    title = (
        rec.get("title")
        or ""
    ).lower()

    high_word = any(
        w in title
        for w in HIGH_VALUE_WORDS
    )

    return (
        high_word
        or to_float(
            rec.get("max_seen_price")
        ) >= 800
    )


async def send_alert(
    client,
    rec,
    live,
    reference,
    drop,
    saving
):
    # Legacy max_seen alerting is intentionally disabled.
    # Intelligence V4 is now the only Amazon review path.
    print(
        "🛡️ LEGACY ALERT SUPPRESSED",
        rec.get("asin"),
        "|",
        round(live, 2),
        flush=True
    )
    return False

    now_ts = int(time.time())

    last_alert_price = to_float(
        rec.get("last_alert_price")
    )
    last_alert_at = int(
        rec.get("last_alert_at", 0)
        or 0
    )

    # Same price already alerted recently.
    # A genuinely new lower price bypasses the cooldown.
    same_price = (
        last_alert_price > 0
        and abs(live - last_alert_price)
            <= max(1.0, last_alert_price * 0.005)
    )

    if same_price and now_ts - last_alert_at < 1800:
        return False

    reason = (
        "🚨 هبوط سعر Amazon مؤكد\n"
        f"ASIN: {rec['asin']}\n"
        f"السعر الحالي: {live:,.2f} جنيه\n"
        f"أعلى سعر رصدناه سابقًا: "
        f"{reference:,.2f} جنيه\n"
        f"التوفير: {saving:,.2f} جنيه\n"
        f"الهبوط المحسوب: {drop:.1f}%\n"
        "✅ تم فتح نفس صفحة المنتج "
        "مرتين والتحقق من السعر قبل الإرسال."
    )

    fp_raw = (
        rec["asin"]
        + "|"
        + str(round(live, 2))
    )

    fp = (
        "amazon-radar-"
        + hashlib.sha256(
            fp_raw.encode()
        ).hexdigest()[:32]
    )

    payload = {
        "fingerprint": fp,
        "store": "Amazon Egypt",
        "title": rec["title"],
        "url": rec["url"],
        "current_price": live,
        "old_price": deal.get("amazon_old_price") or None,
        "discount_percent":
            round(drop, 1),

        "saving":
            round(saving, 2),

        "verified": True,
        "verification_reason": reason,
        "comparison_report": reason,
        "reason": reason,
        "priority": "critical",
        "live_rechecked": True,
        "asin": rec["asin"],
    }

    r = await client.post(
        CLOUD_BASE + "/api/deals",
        json=payload,
        headers=API_HEADERS,
        timeout=30
    )

    if r.status_code == 200:
        rec["last_alert_price"] = live
        rec["last_alert_at"] = now_ts

        if rec["asin"] in watch:
            watch[rec["asin"]]["last_alert_price"] = live
            watch[rec["asin"]]["last_alert_at"] = now_ts

        async with lock:
            save_files()

    print(
        "🚨 AMAZON ALERT",
        rec["asin"],
        "|",
        round(reference, 2),
        "->",
        round(live, 2),
        "|",
        round(drop, 1),
        "%",
        "| API",
        r.status_code,
        flush=True
    )



# =========================================================
# UNIFIED AMAZON DISCOVERY QUEUE
# =========================================================

UNIFIED_AMAZON_QUEUE = []
UNIFIED_QUEUE_SEEN = {}

UNIFIED_QUEUE_LIMIT = 240
UNIFIED_QUEUE_DEDUPE = 1800

# One new Amazon discovery/search request at a time.
# Ultra/Hot product-page monitoring keeps priority.
UNIFIED_QUEUE_GAP = 12


EXTRA_AMAZON_SEARCH_TERMS = [
    "amazon now",
    "amazon now grocery",
    "amazon now everyday essentials",
    "30 minute delivery groceries",
    "amazon resale",
    "used like new",
    "used very good",
    "used good",
    "used acceptable",

    # Electronics
    "electronics deals",
    "mobile phones",
    "smartphones",
    "tablets",
    "computers laptops",
    "computer accessories",
    "monitors",
    "televisions",
    "audio headphones",
    "speakers",
    "smart watches",
    "cameras",
    "gaming",
    "video games",

    # Home & Appliances
    "home appliances",
    "small appliances",
    "air conditioners",
    "fans",
    "refrigerators",
    "washing machines",
    "vacuum cleaners",
    "kitchen appliances",
    "home kitchen",
    "cookware",
    "furniture",
    "home storage",

    # Grocery
    "grocery",
    "grocery deals",
    "food beverages",
    "snacks",
    "chocolate",
    "coffee tea",
    "soft drinks",
    "water beverages",
    "breakfast cereals",
    "cooking oil",
    "rice pasta",
    "canned food",
    "sauces condiments",
    "cleaning supplies",
    "laundry detergent",
    "dishwashing",
    "tissues paper products",

    # Beauty / Personal care
    "beauty",
    "personal care",
    "skin care",
    "hair care",
    "oral care",
    "deodorant",
    "shampoo",
    "perfume",

    # Baby
    "baby",
    "diapers",
    "baby care",
    "baby food",

    # Fashion
    "men fashion",
    "women fashion",
    "kids fashion",
    "shoes",
    "bags",
    "watches",

    # Other categories
    "health",
    "toys",
    "sports fitness",
    "automotive",
    "tools home improvement",
    "pet supplies",
    "books",
    "office stationery",
    "garden outdoor",
    "smart home",

    # Used / Open Box / Renewed
    "used",
    "used electronics",
    "used phones",
    "used laptops",
    "open box",
    "open box electronics",
    "renewed",
    "renewed phones",
    "renewed laptops",
    "refurbished",
    "amazon warehouse",

    # Amazon deal discovery
    "todays deals",
    "limited time deals",
    "amazon deals",
    "discount offers",
    "prime deals",

    # Arabic / Egypt discovery
    "عروض بقالة",
    "مناديل",
    "منظفات",
    "مسحوق غسيل",
    "حفاضات",
    "شامبو",
    "عناية شخصية",
    "عطور",
    "قهوة",
    "شوكولاتة",
    "مشروبات",
    "أجهزة منزلية",
    "موبايلات",
    "لابتوب",
]


def amazon_discovery_group(term):
    t = str(term or "").lower()

    groups = {
        "grocery": (
            "grocery","food","snack","chocolate","coffee","tea",
            "drink","beverage","rice","pasta","oil","cleaning",
            "detergent","dishwashing","tissue",
            "بقالة","مناديل","منظفات","مسحوق","قهوة",
            "شوكولاتة","مشروبات"
        ),
        "fashion": (
            "fashion","shoes","bags","watch",
            "ملابس","أحذية"
        ),
        "beauty_baby": (
            "beauty","personal care","skin care","hair care",
            "shampoo","perfume","baby","diapers",
            "عناية","عطور","حفاضات","شامبو"
        ),
        "home": (
            "home appliances","small appliances","air conditioner",
            "refrigerator","washing machine","vacuum","kitchen",
            "cookware","furniture","garden","tools",
            "أجهزة منزلية"
        ),
        "electronics": (
            "electronics","mobile","smartphone","tablet","computer",
            "laptop","monitor","television","headphone","speaker",
            "camera","gaming","موبايلات","لابتوب"
        ),
        "used": (
            "used","open box","renewed","refurbished","warehouse"
        ),
    }

    for group, words in groups.items():
        if any(word in t for word in words):
            return group

    return "other"


AMAZON_V7_SOURCES = []

for _term in EXTRA_AMAZON_SEARCH_TERMS:
    _base = AMAZON + "/s?k=" + quote_plus(_term)
    _group = amazon_discovery_group(_term)

    for _page in range(1, 4):
        _url = (
            _base
            if _page == 1
            else _base + "&page=" + str(_page)
        )

        AMAZON_V7_SOURCES.append({
            "group": _group,
            "term": _term,
            "page": _page,
            "url": _url,
        })


for term in EXTRA_AMAZON_SEARCH_TERMS:
    base_u = (
        AMAZON
        + "/s?k="
        + quote_plus(term)
    )

    for discovery_page in range(1, 4):
        u = (
            base_u
            if discovery_page == 1
            else base_u + "&page=" + str(discovery_page)
        )

        if u not in SOURCE_URLS:
            SOURCE_URLS.append(u)


def strong_model_tokens(title):
    tokens = model_tokens(title)

    good = []

    generic_patterns = [
        r"^\d+(GB|TB|MB)$",
        r"^\d+(MAH|W|HZ)$",
        r"^\d+G$",
        r"^[245]G$",
        r"^\d+[./]\d+(GB|TB)?$",
        r"^\d+INCH$",
    ]

    for token in tokens:
        t = token.upper().strip()

        if any(
            re.fullmatch(pattern, t)
            for pattern in generic_patterns
        ):
            continue

        good.append(t)

    return good[:3]



def build_trigger_query(title):
    """
    Build the smallest possible Amazon search query.

    Prefer exact model numbers only.
    Avoid Arabic brand words and generic specs because
    long search queries were causing unnecessary 503s.
    """

    title = " ".join(
        str(title or "").split()
    )

    if not title:
        return ""

    tokens = strong_model_tokens(
        title
    )

    if tokens:
        # Prefer the strongest/longest model-looking token.
        tokens = sorted(
            set(tokens),
            key=lambda x: (
                len(x),
                sum(c.isdigit() for c in x)
            ),
            reverse=True
        )

        primary = tokens[0]

        # Use a second token only if it is clearly useful.
        if len(tokens) > 1:
            second = tokens[1]

            if (
                len(second) >= 6
                and second not in primary
                and primary not in second
            ):
                return primary + " " + second

        return primary

    # Product families without classic model IDs.
    low = title.lower()

    families = (
        "iphone",
        "ipad",
        "macbook",
        "playstation",
        "xbox",
    )

    for family in families:
        if family in low:
            words = title.split()
            return " ".join(words[:5])

    return ""



def enqueue_amazon_candidate(
    source,
    title="",
    query="",
    url="",
    priority=50,
    boost_seconds=900,
    kind="trigger",
):
    """
    Latest/priority aware queue.

    The SAME model coming from 2B, Jumia, Kenzz, BTECH etc.
    becomes one queue item instead of many Amazon searches.
    """

    now = time.time()

    if not query and not url:
        return False

    # -------------------------------------------------
    # Canonical identity
    # -------------------------------------------------

    if kind == "discovery" and url:
        canonical = (
            "DISCOVERY|"
            + str(url).lower().strip()
        )

    else:
        model_source = (
            str(query or "")
            + " "
            + str(title or "")
        )

        models = strong_model_tokens(
            model_source
        )

        if models:
            # The strongest model is enough for dedupe.
            model = sorted(
                set(models),
                key=lambda x: (
                    len(x),
                    sum(
                        c.isdigit()
                        for c in x
                    )
                ),
                reverse=True
            )[0]

            canonical = (
                "MODEL|"
                + model.upper()
            )

        else:
            normalized = re.sub(
                r"\s+",
                " ",
                str(query).strip().lower()
            )

            canonical = (
                "QUERY|"
                + normalized
            )

    # -------------------------------------------------
    # Remove stale queue work
    # -------------------------------------------------

    fresh = []

    for old in UNIFIED_AMAZON_QUEUE:
        age = (
            now
            - old.get(
                "queued_at",
                now
            )
        )

        ttl = (
            3600
            if old.get("priority", 0) >= 85
            else 1200
        )

        if age <= ttl:
            fresh.append(old)

    UNIFIED_AMAZON_QUEUE[:] = fresh

    # -------------------------------------------------
    # Merge duplicate model/query across ALL sources
    # -------------------------------------------------

    for old in UNIFIED_AMAZON_QUEUE:

        if old.get("key") != canonical:
            continue

        old_priority = old.get(
            "priority",
            0
        )

        if priority > old_priority:
            old["priority"] = int(
                priority
            )

            old["source"] = source

            if title:
                old["title"] = title

            if query:
                old["query"] = query

        old["boost_seconds"] = max(
            int(
                old.get(
                    "boost_seconds",
                    0
                )
            ),
            int(boost_seconds)
        )

        sources = set(
            old.get(
                "sources",
                []
            )
        )

        sources.add(
            str(source)
        )

        old["sources"] = sorted(
            sources
        )

        return False

    # -------------------------------------------------
    # New queue item
    # -------------------------------------------------

    item = {
        "key": canonical,
        "source": source,
        "sources": [str(source)],
        "title": title,
        "query": query,
        "url": url,
        "priority": int(priority),
        "boost_seconds": int(
            boost_seconds
        ),
        "kind": kind,
        "queued_at": now,
        "ready_at": now,
        "attempts": 0,
    }

    UNIFIED_AMAZON_QUEUE.append(
        item
    )

    # Highest priority first.
    # Within same priority, oldest first.
    UNIFIED_AMAZON_QUEUE.sort(
        key=lambda x: (
            -x.get(
                "priority",
                0
            ),
            x.get(
                "queued_at",
                now
            ),
        )
    )

    # If crowded:
    # preserve important triggers and throw away
    # lowest priority discovery work first.
    if len(UNIFIED_AMAZON_QUEUE) > UNIFIED_QUEUE_LIMIT:

        UNIFIED_AMAZON_QUEUE[:] = (
            UNIFIED_AMAZON_QUEUE[
                :UNIFIED_QUEUE_LIMIT
            ]
        )

    UNIFIED_QUEUE_SEEN[
        canonical
    ] = now

    return True



def pop_amazon_candidate():
    now = time.time()

    # Remove stale entries.
    keep = []

    for item in UNIFIED_AMAZON_QUEUE:

        age = (
            now
            - item.get(
                "queued_at",
                now
            )
        )

        ttl = (
            3600
            if item.get(
                "priority",
                0
            ) >= 85
            else 1200
        )

        if age <= ttl:
            keep.append(item)

    UNIFIED_AMAZON_QUEUE[:] = keep

    # Keep priority ordering fresh.
    UNIFIED_AMAZON_QUEUE.sort(
        key=lambda x: (
            -x.get(
                "priority",
                0
            ),
            x.get(
                "queued_at",
                now
            ),
        )
    )

    for i, item in enumerate(
        UNIFIED_AMAZON_QUEUE
    ):
        if item.get(
            "ready_at",
            0
        ) <= now:
            return UNIFIED_AMAZON_QUEUE.pop(
                i
            )

    return None


def radar_fast_signal(title, current, old_price=0.0):
    title = str(title or "").strip()
    current = to_float(current)
    old_price = to_float(old_price)
    direct = 0.0
    if old_price > current > 0:
        direct = (old_price - current) / old_price * 100.0

    semantic = semantic_price_sanity(
        SimpleNamespace(
            title=title,
            current_price=current,
            old_price=old_price or None,
            category="",
            description="",
            brand="",
            model="",
            variant="",
        )
    )
    severe_semantic = bool(
        semantic.get("candidate")
        and semantic.get("identity_guard")
        and int(semantic.get("score", 0)) >= 92
    )
    if severe_semantic:
        return {"candidate": True, "kind": "cold_start_semantic", "route": "PRIVATE_URGENT", "class": "COLD_START_ANOMALY", "score": int(semantic.get("score", 99)), "direct_discount": direct, "semantic": semantic}
    if direct >= 40.0:
        return {"candidate": True, "kind": "discount_40_plus", "route": "PRIVATE_REVIEW", "class": "FAST_40_REVIEW", "score": max(72, min(95, int(direct))), "direct_discount": direct, "semantic": semantic}
    return {"candidate": False, "kind": None, "route": None, "class": None, "score": 0, "direct_discount": direct, "semantic": semantic}


def queue_fast_signal_from_search_item(item, source="amazon_search_surface"):
    signal = radar_fast_signal(item.get("title"), item.get("current_price"), item.get("old_price"))
    if not signal.get("candidate"):
        return False
    asin = str(item.get("asin") or "").upper().strip()
    if not asin:
        return False
    return enqueue_amazon_candidate(source=source, title=item.get("title") or "", url=asin_url(asin), priority=100, boost_seconds=1800, kind="fast_exact")


async def send_radar_flash_review(client, rec, live, signal, source="amazon_radar"):
    if not signal or not signal.get("candidate"):
        return False
    asin = str(rec.get("asin") or "").upper().strip()
    if not asin:
        return False
    now_ts = int(time.time())
    last_price = to_float(rec.get("last_flash_price"))
    last_at = int(rec.get("last_flash_at", 0) or 0)
    if last_price > 0 and abs(live - last_price) <= max(1.0, last_price * 0.005) and now_ts - last_at < 900:
        return False

    meta = AMAZON_PRODUCT_META_CACHE.get(asin) or {}
    old_price = to_float(meta.get("old_price"))
    direct = to_float(signal.get("direct_discount"))
    truth = {
        "class": signal.get("class"),
        "route": signal.get("route"),
        "score": int(signal.get("score", 0) or 0),
        "reason": "exact Amazon product page price confirmed once; full/history/market verification continues in background",
    }
    fp_raw = asin + "|" + str(round(live, 2))
    fp = "amazon-radar-" + hashlib.sha256(fp_raw.encode()).hexdigest()[:32]
    image_url = str(meta.get("image_url") or rec.get("image_url") or "")
    title = str(meta.get("page_title") or rec.get("title") or "")

    flash_card = build_flash_review_card(
        store="Amazon Egypt",
        title=title,
        current_price=float(live),
        old_price=old_price or None,
        discount_percent=round(direct, 1),
        url=rec.get("url") or asin_url(asin),
        image_url=image_url,
        route=truth["route"],
        priority_class=truth["class"],
        price_verified=True,
        reference_verified=False,
        reference_price=None,
        asin=asin,
    )

    payload = {
        "fingerprint": fp,
        "store": "Amazon Egypt",
        "title": title,
        "url": rec.get("url") or asin_url(asin),
        "current_price": float(live),
        "old_price": old_price or None,
        "discount_percent": round(direct, 1),
        "saving": round(max(0.0, old_price - live), 2) if old_price > live else 0.0,
        "verified": False,
        "preliminary": True,
        "review_mode": "flash",
        "price_verified": True,
        "reference_verified": False,
        "live_rechecked": True,
        "priority": "super_ultra" if truth["route"] == "PRIVATE_URGENT" else "ultra",
        "priority_truth": truth,
        "detection_kind": signal.get("kind"),
        "asin": asin,
        "image_url": image_url,
        "source": source,
        "verification_reason": truth["reason"],
        "reason": truth["reason"],

        # V9 presentation contract. Existing Cloud endpoints may ignore
        # these extra fields safely until moderation buttons are upgraded.
        "review_card": flash_card,
        "review_text": flash_card["review_text"],
        "urgent_post_text": flash_card["urgent_post_text"],
        "normal_post_text": flash_card["normal_post_text"],
        "publish_actions": flash_card["publish_actions"],
        "hype_label": flash_card["hype_label"],
    }
    r = await client.post(CLOUD_BASE + "/api/deals", json=payload, headers=API_HEADERS, timeout=20)
    print("⚡ RADAR FLASH REVIEW", asin, "|", truth["class"], "|", round(live, 2), "| API", r.status_code, flush=True)
    if r.status_code == 200:
        rec["last_flash_price"] = float(live)
        rec["last_flash_at"] = now_ts
        async with lock:
            if asin in watch:
                watch[asin]["last_flash_price"] = float(live)
                watch[asin]["last_flash_at"] = now_ts
            save_files()
        return True
    return False


async def process_queue_item(client, job):
    if job["url"]:
        amazon_url = job["url"]
    else:
        amazon_url = (
            AMAZON
            + "/s?k="
            + quote_plus(job["query"])
        )

    exact_asin = asin_from_url(amazon_url) if asin_from_url is not None else None

    if exact_asin and "/dp/" in amazon_url:
        live = await live_price(client, amazon_url)
        if live is None:
            return False
        meta = AMAZON_PRODUCT_META_CACHE.get(exact_asin) or {}
        title = str(meta.get("page_title") or job.get("title") or exact_asin)
        old_price = to_float(meta.get("old_price"))
        item = {"asin": exact_asin, "title": title, "url": asin_url(exact_asin), "current_price": float(live), "old_price": old_price or None}
        add_product(item, "queue:" + job["source"])
        if exact_asin in watch:
            watch[exact_asin]["priority_boost_until"] = int(time.time()) + job["boost_seconds"]
            watch[exact_asin]["trigger_source"] = job["source"]
            watch[exact_asin]["trigger_title"] = title
        async with lock:
            save_files()
        rec = watch.get(exact_asin) or item
        signal = radar_fast_signal(title, live, old_price)
        if signal.get("candidate"):
            await send_radar_flash_review(client, rec, float(live), signal, source=job.get("source") or "amazon_exact_queue")
        print("⚡ EXACT QUEUE -> AMAZON", job.get("source"), "| ASIN:", exact_asin, "| FAST:", bool(signal.get("candidate")), "| PRICE:", round(float(live), 2), flush=True)
        return True

    html = await fetch_html(client, amazon_url)
    if not html:
        return False
    items = parse_search(html)
    if not items:
        return True

    # Broad Amazon discovery page.
    if job["kind"] == "discovery":
        added = 0

        fast_queued = 0

        for item in items[:24]:
            before = len(watch)
            add_product(item, "queue:" + job["source"])
            if len(watch) > before:
                added += 1
            if queue_fast_signal_from_search_item(item, source="fast_signal:" + job["source"]):
                fast_queued += 1

        async with lock:
            save_files()

        print(
            "🔎 QUEUE DISCOVERY",
            job["source"],
            "| FOUND =",
            len(items),
            "| NEW =",
            added,
            "| FAST QUEUED =",
            fast_queued,
            "| QUEUE =",
            len(UNIFIED_AMAZON_QUEUE),
            flush=True
        )

        return True

    original_title = (
        job["title"]
        or job["query"]
    )

    original_tokens = strong_model_tokens(
        original_title
    )

    best = None
    best_score = 0

    for item in items[:12]:
        amazon_title = (
            item.get("title")
            or ""
        )

        score = fuzz.token_set_ratio(
            original_title.lower(),
            amazon_title.lower()
        )

        token_match = (
            bool(original_tokens)
            and any(
                token.upper()
                in amazon_title.upper()
                for token in original_tokens
            )
        )

        if token_match:
            score += 20

        if (
            original_tokens
            and not token_match
            and score < 82
        ):
            continue

        if score > best_score:
            best_score = score
            best = item

    if best is None:
        return True

    if best_score < 58:
        return True

    asin = best["asin"]
    existed = asin in watch

    add_product(
        best,
        "queue:" + job["source"]
    )

    if asin in watch:
        watch[asin][
            "priority_boost_until"
        ] = int(time.time()) + job[
            "boost_seconds"
        ]

        watch[asin][
            "trigger_source"
        ] = job["source"]

        watch[asin][
            "trigger_title"
        ] = original_title

        watch[asin][
            "trigger_match_score"
        ] = round(best_score, 1)

    async with lock:
        save_files()

    print(
        "🎯 QUEUE -> AMAZON",
        job["source"],
        "| ASIN:",
        asin,
        "| SCORE:",
        round(best_score, 1),
        "|",
        "KNOWN" if existed else "NEW",
        flush=True
    )

    return True


async def unified_queue_loop():
    await asyncio.sleep(4)

    async with httpx.AsyncClient() as client:

        while True:
            job = pop_amazon_candidate()
            if not job:
                await asyncio.sleep(1.5)
                continue
            job_url = str(job.get("url") or "")
            exact_priority = bool(job.get("priority", 0) >= 95 and "/dp/" in job_url)
            if amazon_search_backoff_active() and not exact_priority:
                job["ready_at"] = time.time() + 30
                UNIFIED_AMAZON_QUEUE.append(job)
                await asyncio.sleep(3)
                continue
            if not amazon_secondary_allowed() and not exact_priority:
                job["ready_at"] = time.time() + 20
                UNIFIED_AMAZON_QUEUE.append(job)
                await asyncio.sleep(3)
                continue

            try:
                ok = await process_queue_item(
                    client,
                    job
                )

                if not ok:
                    job["attempts"] += 1

                    if (
                        job["attempts"] <= 2
                        and not amazon_circuit_open()
                    ):
                        job["ready_at"] = (
                            time.time() + 60
                        )

                        UNIFIED_AMAZON_QUEUE.append(
                            job
                        )

                        UNIFIED_AMAZON_QUEUE.sort(
                            key=lambda x: (
                                -x["priority"],
                                x["ready_at"],
                            )
                        )

            except Exception as e:
                print(
                    "QUEUE WORKER ERROR:",
                    repr(e),
                    flush=True
                )

            # Searches are intentionally sparse.
            # Ultra/Hot direct product checks stay fastest.
            # Important external/competitor models are
            # processed faster than broad Amazon discovery.
            if job.get("priority", 0) >= 95:
                queue_delay = 0.35
            elif job.get("priority", 0) >= 85:
                queue_delay = 2.0
            elif job.get("priority", 0) >= 50:
                queue_delay = 20
            else:
                queue_delay = 60

            await asyncio.sleep(
                queue_delay
            )




async def deep_discovery():
    # DISCOVERY V7 BALANCED
    # Every major category receives guaranteed search slots.
    # Network request speed remains governed elsewhere.

    soft_limit = int(UNIFIED_QUEUE_LIMIT * 0.75)

    if len(UNIFIED_AMAZON_QUEUE) >= soft_limit:
        print(
            "⏳ DISCOVERY V7 SKIPPED",
            "| QUEUE =", len(UNIFIED_AMAZON_QUEUE),
            flush=True
        )
        return

    group_order = [
        "grocery",
        "fashion",
        "beauty_baby",
        "home",
        "electronics",
        "used",
        "other",
    ]

    stats = {}
    total = 0

    for group in group_order:
        sources = [
            x for x in AMAZON_V7_SOURCES
            if x["group"] == group
        ]

        if not sources:
            stats[group] = 0
            continue

        state_key = "v7_idx_" + group
        pos = int(state.get(state_key, 0) or 0)

        added = 0
        checked = 0

        while added < 3 and checked < len(sources):
            src = sources[(pos + checked) % len(sources)]

            if enqueue_amazon_candidate(
                source="amazon_v7:" + group,
                url=src["url"],
                priority=30,
                boost_seconds=600,
                kind="discovery",
            ):
                added += 1
                total += 1

            checked += 1

        state[state_key] = (
            pos + checked
        ) % len(sources)

        stats[group] = added

    async with lock:
        save_files()

    print(
        "🧠 DISCOVERY V7 BALANCED",
        "| TOTAL =", total,
        "| grocery =", stats.get("grocery", 0),
        "| fashion =", stats.get("fashion", 0),
        "| beauty_baby =", stats.get("beauty_baby", 0),
        "| home =", stats.get("home", 0),
        "| electronics =", stats.get("electronics", 0),
        "| used =", stats.get("used", 0),
        "| other =", stats.get("other", 0),
        "| QUEUE =", len(UNIFIED_AMAZON_QUEUE),
        "| WATCHLIST =", len(watch),
        flush=True
    )


async def hot_watch_once():
    async with lock:
        hot = [
            dict(x)
            for x in watch.values()
            if is_hot(x)
        ]

    hot.sort(
        key=lambda x: (
            to_float(
                x.get(
                    "max_seen_price"
                )
            ),
            x.get(
                "last_seen",
                0
            ),
        ),
        reverse=True
    )

    # أسرع 45 منتج عالي الأهمية
    hot = hot[:45]

    if not hot:
        print(
            "⚡ HOT WATCH: empty",
            flush=True
        )
        return

    start = int(
        state.get(
            "hot_index",
            0
        )
    )

    batch = [
        hot[
            (start + i)
            % len(hot)
        ]
        for i in range(
            min(5, len(hot))
        )
    ]

    state["hot_index"] = (
        start + len(batch)
    ) % len(hot)

    semaphore = asyncio.Semaphore(2)

    async with httpx.AsyncClient() as client:

        async def check(rec):
            async with semaphore:
                current = await live_price(
                    client,
                    rec["url"]
                )

                if current is None:
                    return None

                reference = to_float(
                    rec.get(
                        "max_seen_price"
                    )
                )

                result = {
                    "asin":
                        rec["asin"],
                    "title":
                        rec["title"],
                    "url":
                        rec["url"],
                    "current_price":
                        current,
                    "old_price":
                        None,
                }

                # V5 LIVE CHECK - HOT
                v5_sent, v5_live = (
                    await maybe_send_v5_review(
                        client,
                        rec,
                        current
                    )
                )

                if v5_live is not None:
                    result[
                        "current_price"
                    ] = v5_live

                if v5_sent:
                    return result

                if reference <= 0:
                    return result

                saving = (
                    reference - current
                )

                if saving <= 0:
                    return result

                drop = (
                    saving
                    / reference
                    * 100
                )

                # هبوط مهم
                suspicious = (
                    (
                        current
                        <= reference * 0.50
                        and saving >= 300
                    )
                    or
                    (
                        current
                        <= reference * 0.70
                        and saving >= 1000
                    )
                )

                if not suspicious:
                    return result

                # إعادة تحقق فورية ثانية
                await asyncio.sleep(1)

                second = await live_price(
                    client,
                    rec["url"]
                )

                if second is None:
                    return result

                if second > current * 1.05:
                    print(
                        "PRICE CHANGED - CANCELLED:",
                        rec["asin"],
                        current,
                        "->",
                        second,
                        flush=True
                    )
                    return result

                saving2 = (
                    reference - second
                )

                if saving2 <= 0:
                    return result

                drop2 = (
                    saving2
                    / reference
                    * 100
                )

                await send_alert(
                    client,
                    rec,
                    second,
                    reference,
                    drop2,
                    saving2
                )

                result[
                    "current_price"
                ] = second

                return result

        results = await asyncio.gather(
            *[
                check(x)
                for x in batch
            ]
        )

    valid = [
        x
        for x in results
        if x
    ]

    async with lock:
        now = int(time.time())

        for item in valid:
            asin = item["asin"]

            if asin not in watch:
                continue

            p = to_float(
                item["current_price"]
            )

            watch[asin][
                "last_price"
            ] = p

            # Build Intelligence V4 Stable Anchor from HOT
            # Uses the price already fetched — NO extra Amazon request.
            record_price_sample(
                watch[asin],
                p
            )

            watch[asin][
                "last_checked"
            ] = now

            if p > to_float(
                watch[asin].get(
                    "max_seen_price"
                )
            ):
                watch[asin][
                    "max_seen_price"
                ] = p

            old_min = to_float(
                watch[asin].get(
                    "min_seen_price"
                )
            )

            if (
                old_min <= 0
                or p < old_min
            ):
                watch[asin][
                    "min_seen_price"
                ] = p

        save_files()

    await cloud_observations(
        valid
    )

    print(
        "⚡ HOT WATCH",
        datetime.now().strftime(
            "%H:%M:%S"
        ),
        "| CHECKED =",
        len(batch),
        "| LIVE =",
        len(valid),
        flush=True
    )



def intel_review_eligible(intel):
    if not intel:
        return False

    tier = str(
        intel.get("tier", "WATCH")
    ).upper()

    confidence = int(
        intel.get("confidence", 0)
        or 0
    )

    thresholds = {
        "HOT": 75,
        "ULTRA": 70,
        "CRITICAL": 85,
    }

    return (
        tier in thresholds
        and confidence >= thresholds[tier]
    )

async def send_intel_review(
    client,
    rec,
    live,
    intel,
):
    """
    Sends ONLY to the existing /api/deals moderation flow.
    It does not publish directly to the Telegram channel.
    """

    now_ts = int(time.time())

    last_alert_price = to_float(
        rec.get("last_alert_price")
    )

    last_alert_at = int(
        rec.get("last_alert_at", 0)
        or 0
    )

    same_price = (
        last_alert_price > 0
        and abs(live - last_alert_price)
        <= max(
            1.0,
            last_alert_price * 0.005
        )
    )

    if (
        same_price
        and now_ts - last_alert_at < 1800
    ):
        return False

    tier = str(
        intel.get("tier", "HOT")
    ).upper()

    confidence = int(
        intel.get("confidence", 0)
        or 0
    )

    anchor = to_float(
        intel.get("anchor_price")
    )

    market = to_float(
        intel.get("market_median")
    )

    history_ref = to_float(
        intel.get("reference")
    )

    refs = [
        x for x in (
            anchor,
            market,
            history_ref,
        )
        if x > live
    ]

    # Conservative reference:
    # never exaggerate the calculated saving.
    reference = (
        min(refs)
        if refs
        else history_ref
    )

    if reference <= live:
        return False

    saving = (
        reference - live
    )

    drop = (
        saving
        / reference
        * 100
    )

    stores = intel.get(
        "market_stores",
        []
    ) or []

    market_text = (
        f"{market:,.2f} جنيه"
        if market > 0
        else "غير متاح"
    )

    anchor_text = (
        f"{anchor:,.2f} جنيه"
        if anchor > 0
        else "غير متاح"
    )

    reason = (
        f"🧠 تصنيف الذكاء: {tier}\n"
        f"🎯 الثقة: {confidence}%\n"
        f"📦 ASIN: {rec['asin']}\n"
        f"💰 السعر المؤكد الآن: {live:,.2f} جنيه\n"
        f"📊 Stable Anchor: {anchor_text}\n"
        f"🏪 مرجع السوق المطابق: {market_text}\n"
        f"🔎 متاجر المطابقة: "
        f"{', '.join(stores) if stores else '—'}\n"
        f"💵 التوفير المحافظ: {saving:,.2f} جنيه\n"
        f"📉 الهبوط المحافظ: {drop:.1f}%\n"
        f"✅ تم فتح صفحة المنتج مباشرة مرتين "
        f"وإعادة تشغيل Intelligence V4 قبل الإرسال.\n"
        f"⚠️ للمراجعة فقط — لا نشر تلقائي."
    )

    fp_raw = (
        rec["asin"]
        + "|"
        + str(round(live, 2))
    )

    fp = (
        "amazon-radar-"
        + hashlib.sha256(
            fp_raw.encode()
        ).hexdigest()[:32]
    )

    payload = {
        "fingerprint": fp,
        "store": "Amazon Egypt",
        "title": rec["title"],
        "url": rec["url"],
        "current_price": live,
        "old_price": None,
        "discount_percent": round(
            drop,
            1
        ),
        "saving": round(
            saving,
            2
        ),
        "verified": True,
        "verification_reason": reason,
        "comparison_report": reason,
        "reason": reason,
        "priority": (
            "super_ultra"
            if float(drop or 0) > 60.0
            else tier.lower()
        ),
        "live_rechecked": True,
        "asin": rec["asin"],

        # Intelligence metadata
        "intelligence_tier": tier,
        "super_ultra": bool(float(drop or 0) > 60.0),
        "confidence": confidence,
        "reference_price": round(
            reference,
            2
        ),
        "anchor_price": (
            round(anchor, 2)
            if anchor > 0
            else None
        ),
        "market_reference": (
            round(market, 2)
            if market > 0
            else None
        ),
        "market_stores": stores,
    }

    r = await client.post(
        CLOUD_BASE + "/api/deals",
        json=payload,
        headers=API_HEADERS,
        timeout=30
    )

    print(
        "📩 INTEL REVIEW",
        rec["asin"],
        "|",
        tier,
        "| CONF",
        confidence,
        "|",
        round(reference, 2),
        "->",
        round(live, 2),
        "| API",
        r.status_code,
        flush=True
    )

    if r.status_code == 200:

        rec["last_alert_price"] = live
        rec["last_alert_at"] = now_ts

        async with lock:
            if rec["asin"] in watch:
                watch[
                    rec["asin"]
                ][
                    "last_alert_price"
                ] = live

                watch[
                    rec["asin"]
                ][
                    "last_alert_at"
                ] = now_ts

            save_files()

        return True

    return False


async def maybe_send_intel_review(
    client,
    rec,
    current,
):
    """
    Fail-safe:
    any intelligence problem returns to normal radar operation.
    """

    try:
        first = evaluate_shadow(
            rec,
            current
        )

        if not intel_review_eligible(
            first
        ):
            return False, current

        print(
            "🧠 INTEL CANDIDATE",
            rec["asin"],
            "|",
            first.get("tier"),
            "| CONF =",
            first.get("confidence"),
            "| PRICE =",
            round(current, 2),
            flush=True
        )

        # Force a real Amazon product-page request.
        invalidate_amazon_cache(
            rec["asin"]
        )

        await asyncio.sleep(0.8)

        second = await live_price(
            client,
            rec["url"]
        )

        if second is None:
            print(
                "⚠️ INTEL RECHECK FAILED",
                rec["asin"],
                flush=True
            )
            return False, current

        # Two independent reads should agree closely.
        difference = (
            abs(second - current)
            / max(current, 1)
        )

        if difference > 0.02:
            print(
                "⚠️ INTEL PRICE MISMATCH",
                rec["asin"],
                "|",
                round(current, 2),
                "->",
                round(second, 2),
                flush=True
            )
            return False, second

        second_intel = evaluate_shadow(
            rec,
            second
        )

        if not intel_review_eligible(
            second_intel
        ):
            print(
                "⚠️ INTEL DOWNGRADED",
                rec["asin"],
                "|",
                second_intel.get("tier"),
                "| CONF =",
                second_intel.get(
                    "confidence"
                ),
                flush=True
            )
            return False, second

        sent = await send_intel_review(
            client,
            rec,
            second,
            second_intel,
        )

        return sent, second

    except Exception as exc:
        print(
            "INTEL REVIEW ERROR:",
            rec.get("asin"),
            repr(exc),
            flush=True
        )

        # Intelligence must NEVER kill Ultra.
        return False, current




# =========================================================
# DEAL ENGINE V5.1 - VERIFIED DEAL REVIEW
# =========================================================

try:
    from deal_engine_v5_bridge import evaluate_record_v5
except Exception as _v5_error:
    evaluate_record_v5 = None


async def send_v5_review(
    client,
    rec,
    live,
    deal,
):
    now_ts = int(time.time())

    last_price = to_float(
        rec.get("last_alert_price")
    )

    last_at = int(
        rec.get("last_alert_at", 0)
        or 0
    )

    same_price = (
        last_price > 0
        and abs(live - last_price)
        <= max(1.0, last_price * 0.005)
    )

    if (
        same_price
        and now_ts - last_at < 1800
    ):
        return False

    tier = str(
        deal.get("tier", "DEAL")
    )

    score = int(
        deal.get("score", 0)
        or 0
    )

    reference = to_float(
        deal.get("reference_price")
    )

    saving = to_float(
        deal.get("saving")
    )

    discount = to_float(
        deal.get("price_drop_percent")
    )

    promo_deal_percent = to_float(
        deal.get("deal_percent")
    )

    market = to_float(
        deal.get("market_median")
    )

    stores = deal.get(
        "market_stores",
        []
    ) or []

    evidence = deal.get(
        "evidence",
        []
    ) or []

    promo_type = str(
        deal.get("promo_type", "none")
        or "none"
    ).lower()

    promo_percent = to_float(
        deal.get("promo_percent")
    )

    coupon_value = to_float(
        deal.get("coupon_value")
    )

    promo_details = str(
        deal.get("promo_details", "")
        or ""
    )

    conditional = bool(
        deal.get("conditional", False)
    )

    member_only = bool(
        deal.get("member_only", False)
    )

    promo_lines = []

    promo_saving = 0.0
    promo_final_price = live

    if (
        promo_type in (
            "card",
            "bank_card",
            "percent",
            "quantity_discount",
            "member",
        )
        and promo_percent > 0
    ):
        promo_saving = live * promo_percent / 100.0
        promo_final_price = max(
            0.0,
            live - promo_saving
        )

    elif promo_type == "coupon" and coupon_value > 0:
        promo_saving = min(
            live,
            coupon_value
        )
        promo_final_price = max(
            0.0,
            live - promo_saving
        )

    if promo_type == "amazon_price_drop":
        promo_lines.append(
            f"🏷️ خصم Amazon مباشر: {promo_percent:.1f}%"
        )

    elif promo_type == "bogo":
        promo_lines.append(
            "🎁 العرض: اشترِ واحدة واحصل على واحدة مجانًا"
        )

    elif promo_type == "coupon":
        if coupon_value > 0:
            promo_lines.append(
                f"🎟️ كوبون: توفير {coupon_value:,.2f} جنيه"
            )
        else:
            promo_lines.append("🎟️ كوبون خصم متاح")

    elif promo_type in ("card", "bank_card"):
        if promo_percent > 0:
            promo_lines.append(
                f"💳 خصم بطاقة/بنك: {promo_percent:.1f}%"
            )
        else:
            promo_lines.append(
                "💳 خصم بطاقة/بنك متاح"
            )

    elif member_only:
        if promo_percent > 0:
            promo_lines.append(
                f"👑 Prime/Member: خصم {promo_percent:.1f}%"
            )
        else:
            promo_lines.append(
                "👑 عرض Prime/Member"
            )

    elif promo_type == "flash":
        promo_lines.append("⚡ Flash Sale")

    elif promo_type == "quantity_discount":
        if promo_percent > 0:
            promo_lines.append(
                f"📦 خصم كمية: {promo_percent:.1f}%"
            )
        else:
            promo_lines.append("📦 خصم كمية")

    elif promo_type == "bulk_discount":
        promo_lines.append(
            "📦 خصم شراء كميات — لا يؤثر على Deal Score"
        )

    elif promo_type == "percent" and promo_percent > 0:
        promo_lines.append(
            f"🏷️ خصم إضافي: {promo_percent:.1f}%"
        )

    if promo_saving > 0:
        promo_lines.append(
            f"💵 التوفير المحتمل من العرض: {promo_saving:,.2f} جنيه"
        )
        promo_lines.append(
            f"💰 السعر المتوقع بعد العرض: {promo_final_price:,.2f} جنيه"
        )

    secondary_promo_type = str(
        deal.get("secondary_promo_type", "none")
        or "none"
    ).lower()

    secondary_promo_percent = to_float(
        deal.get("secondary_promo_percent")
    )

    if (
        secondary_promo_type in ("card", "bank_card")
        and secondary_promo_percent > 0
    ):
        promo_lines.append(
            f"💳 عرض بنك إضافي: {secondary_promo_percent:.1f}%"
        )

    elif (
        secondary_promo_type in ("member", "percent")
        and secondary_promo_percent > 0
        and bool(deal.get("member_only", False))
    ):
        promo_lines.append(
            f"👑 عرض Prime إضافي: {secondary_promo_percent:.1f}%"
        )

    if promo_details:
        promo_lines.append(
            f"📝 تفاصيل العرض: {promo_details}"
        )

    if (
        conditional
        and promo_type not in ("none", "bulk_discount")
    ):
        promo_lines.append(
            "⚠️ العرض الإضافي له شروط"
        )

    promo_section = (
        "\n".join(promo_lines) + "\n"
        if promo_lines
        else ""
    )

    reason = (
        f"🔥 Deal Engine V5.1: {tier}\n"
        f"🎯 Deal Score: {score}/100\n"
        f"📦 ASIN: {rec['asin']}\n"
        f"💰 السعر الحالي المؤكد: {live:,.2f} جنيه\n"
        f"📊 السعر المرجعي: {reference:,.2f} جنيه\n"
        f"💵 التوفير: {saving:,.2f} جنيه\n"
        f"📉 قيمة العرض: {discount:.1f}%\n"
        f"🏪 سعر السوق المطابق: "
        f"{market:,.2f} جنيه\n"
        f"🔎 المتاجر: "
        f"{', '.join(stores) if stores else '—'}\n"
        f"🧠 الأدلة: "
        f"{', '.join(evidence) if evidence else '—'}\n"
        f"{promo_section}"
        f"✅ تم إعادة فتح صفحة Amazon والتحقق "
        f"من السعر قبل الإرسال.\n"
        f"⚠️ مراجعة أدمن فقط — لا نشر تلقائي."
    )

    fp_raw = (
        rec["asin"]
        + "|"
        + str(round(live, 2))
    )

    fp = (
        "amazon-radar-"
        + hashlib.sha256(
            fp_raw.encode()
        ).hexdigest()[:32]
    )

    payload = {
        "fingerprint": fp,
        "store": "Amazon Egypt",
        "title": rec["title"],
        "url": rec["url"],
        "current_price": live,
        "old_price": None,
        "discount_percent": round(discount, 1),
        "promo_discount_percent": round(promo_percent, 1),
        "promo_saving": round(promo_saving, 2),
        "promo_final_price": round(promo_final_price, 2),
        "saving": round(saving, 2),
        "verified": True,
        "verification_reason": reason,
        "comparison_report": reason,
        "reason": reason,
        "priority": (
            "super_ultra"
            if max(
                float(discount or 0),
                to_float(deal.get("stack_effective_discount_percent", 0)),
            ) > 60.0
            else tier.lower()
        ),
        "live_rechecked": True,
        "asin": rec["asin"],
        "deal_engine": "V5.1",
        "super_ultra": bool(
            max(
                float(discount or 0),
                to_float(deal.get("stack_effective_discount_percent", 0)),
            ) > 60.0
        ),
        "deal_score": score,
        "reference_price": reference,
        "market_reference": market or None,
        "market_stores": stores,
        "evidence": evidence,
        "image_url": deal.get("image_url", ""),
        "title_ar": deal.get("title_ar", ""),
        "amazon_direct_discount": deal.get("amazon_direct_discount", 0),
        "promo_type": promo_type,
        "promo_percent": promo_percent,
        "coupon_value": coupon_value,
        "promo_verified": bool(deal.get("promo_verified", False)),
        "promo_details": promo_details,
        "conditional": conditional,
        "member_only": member_only,
        "promo_label": promo_lines[0] if promo_lines else "",

        "promo_stack": deal.get("promo_stack", []),
        "task_discount_value": deal.get("task_discount_value", 0),
        "stack_effective_price": deal.get("stack_effective_price", 0),
        "stack_effective_discount_percent": deal.get("stack_effective_discount_percent", 0),
        "stack_ultra": bool(deal.get("stack_ultra", False)),
        "stack_super_ultra": bool(deal.get("stack_super_ultra", False)),
        "condition": deal.get("condition", "new"),
        "offer_key": deal.get("offer_key", rec.get("asin", "") + "|new"),
        "is_resale": bool(deal.get("is_resale", False)),
    }

    r = await client.post(
        CLOUD_BASE + "/api/deals",
        json=payload,
        headers=API_HEADERS,
        timeout=30
    )

    print(
        "🔥 V5 REVIEW",
        rec["asin"],
        "|",
        tier,
        "| SCORE",
        score,
        "|",
        round(reference, 2),
        "->",
        round(live, 2),
        "| API",
        r.status_code,
        flush=True
    )

    if r.status_code == 200:
        rec["last_alert_price"] = live
        rec["last_alert_at"] = now_ts

        async with lock:
            if rec["asin"] in watch:
                watch[rec["asin"]][
                    "last_alert_price"
                ] = live

                watch[rec["asin"]][
                    "last_alert_at"
                ] = now_ts

            save_files()

        return True

    return False


async def maybe_send_v5_review(
    client,
    rec,
    current,
):
    if evaluate_record_v5 is None:
        return False, current

    try:
        first = evaluate_record_v5(
            v5_record_with_promo(rec),
            current
        )

        if not first.get("send"):
            return False, current

        print(
            "🔥 V5 CANDIDATE",
            rec["asin"],
            "|",
            first.get("tier"),
            "| SCORE",
            first.get("score"),
            "| PRICE",
            round(current, 2),
            flush=True
        )

        # Force a genuinely new Amazon page request.
        invalidate_amazon_cache(
            rec["asin"]
        )

        await asyncio.sleep(0.8)

        second = await live_price(
            client,
            rec["url"]
        )

        if second is None:
            print(
                "⚠️ V5 RECHECK FAILED",
                rec["asin"],
                flush=True
            )
            return False, current

        difference = (
            abs(second - current)
            / max(current, 1)
        )

        if difference > 0.02:
            print(
                "⚠️ V5 PRICE MISMATCH",
                rec["asin"],
                "|",
                round(current, 2),
                "->",
                round(second, 2),
                flush=True
            )
            return False, second

        final = evaluate_record_v5(
            v5_record_with_promo(rec),
            second
        )

        if not final.get("send"):
            print(
                "⚠️ V5 DEAL DOWNGRADED",
                rec["asin"],
                "| SCORE",
                final.get("score"),
                flush=True
            )
            return False, second

        sent = await send_v5_review(
            client,
            rec,
            second,
            final
        )

        return sent, second

    except Exception as exc:
        print(
            "V5 REVIEW ERROR:",
            rec.get("asin"),
            repr(exc),
            flush=True
        )

        return False, current



async def ultra_hot_once():
    async with lock:
        items = [
            dict(x)
            for x in watch.values()
            if is_hot(x)
        ]

    now_ts = int(time.time())

    items.sort(
        key=lambda x: (
            1 if int(x.get("priority_boost_until", 0)) > now_ts else 0,
            to_float(x.get("max_seen_price")),
            x.get("last_seen", 0),
        ),
        reverse=True
    )

    # أسرع 20 منتج
    items = items[:20]

    if not items:
        print("🚨 ULTRA HOT: empty", flush=True)
        return

    ultra_index = int(
        state.get("ultra_index", 0)
    )

    # 10 منتجات كل دورة
    batch = [
        items[
            (ultra_index + i)
            % len(items)
        ]
        for i in range(
            min(6, len(items))
        )
    ]

    state["ultra_index"] = (
        ultra_index + len(batch)
    ) % len(items)

    semaphore = asyncio.Semaphore(8)

    async with httpx.AsyncClient() as client:

        async def check(rec):
            async with semaphore:
                current = await live_price(
                    client,
                    rec["url"]
                )

                if current is None:
                    return None

                reference = to_float(
                    rec.get("max_seen_price")
                )

                result = {
                    "asin": rec["asin"],
                    "title": rec["title"],
                    "url": rec["url"],
                    "current_price": current,
                }

                # Intelligence V4 review path.
                # Runs while AsyncClient is still open.
                intel_sent, intel_live = (
                    await maybe_send_intel_review(
                        client,
                        rec,
                        current
                    )
                )

                if intel_live is not None:
                    result[
                        "current_price"
                    ] = intel_live

                if intel_sent:
                    return result

                # V5 LIVE CHECK - ULTRA
                v5_price = to_float(
                    result.get("current_price")
                ) or current

                v5_sent, v5_live = (
                    await maybe_send_v5_review(
                        client,
                        rec,
                        v5_price
                    )
                )

                if v5_live is not None:
                    result[
                        "current_price"
                    ] = v5_live

                if v5_sent:
                    return result

                if reference <= 0:
                    return result

                saving = reference - current

                if saving <= 0:
                    return result

                drop = (
                    saving
                    / reference
                    * 100
                )

                suspicious = (
                    (
                        current <= reference * 0.50
                        and saving >= 300
                    )
                    or
                    (
                        current <= reference * 0.70
                        and saving >= 1000
                    )
                )

                if not suspicious:
                    return result

                # إعادة تحقق فورية حقيقية
                # Clear the 7-second product-page cache first.
                invalidate_amazon_cache(
                    rec["asin"]
                )

                await asyncio.sleep(0.7)

                second = await live_price(
                    client,
                    rec["url"]
                )

                if second is None:
                    return result

                if second > current * 1.05:
                    print(
                        "🚨 ULTRA CANCELLED:",
                        rec["asin"],
                        current,
                        "->",
                        second,
                        flush=True
                    )
                    return result

                saving2 = reference - second

                if saving2 <= 0:
                    return result

                drop2 = (
                    saving2
                    / reference
                    * 100
                )

                await send_alert(
                    client,
                    rec,
                    second,
                    reference,
                    drop2,
                    saving2
                )

                result["current_price"] = second

                return result

        results = await asyncio.gather(
            *[
                check(x)
                for x in batch
            ]
        )

    valid = [
        x for x in results
        if x
    ]

    async with lock:
        now = int(time.time())

        for item in valid:
            asin = item["asin"]

            if asin not in watch:
                continue

            price = to_float(
                item["current_price"]
            )

            watch[asin]["last_price"] = price

            # PRICE INTELLIGENCE SHADOW MODE
            # Does NOT control alerts.
            record_price_sample(
                watch[asin],
                price
            )

            intel_shadow = evaluate_shadow(
                watch[asin],
                price
            )

            if (
                intel_shadow
                and intel_shadow.get("tier")
                in ("HOT", "ULTRA", "CRITICAL")
            ):
                print(
                    "🧠 INTEL SHADOW",
                    asin,
                    "|",
                    intel_shadow.get("tier"),
                    "| CONF =",
                    intel_shadow.get("confidence"),
                    "| REF =",
                    intel_shadow.get("reference"),
                    "| NOW =",
                    round(price, 2),
                    "| DROP =",
                    intel_shadow.get("drop"),
                    "%",
                    "| BASIS =",
                    intel_shadow.get("reason"),
                    flush=True
                )
            watch[asin]["ultra_checked"] = now

            if price > to_float(
                watch[asin].get(
                    "max_seen_price"
                )
            ):
                watch[asin][
                    "max_seen_price"
                ] = price

        save_files()

    print(
        "🚨 ULTRA HOT",
        datetime.now().strftime("%H:%M:%S"),
        "| CHECKED =",
        len(batch),
        "| LIVE =",
        len(valid),
        flush=True
    )


async def ultra_hot_loop():
    await asyncio.sleep(5)

    while True:
        if amazon_circuit_open():
            remaining = amazon_circuit_remaining()
            mode = (
                "HARD CIRCUIT"
                if amazon_hard_block_active()
                else "CIRCUIT"
            )

            print(
                "⏸️ ULTRA WAITING FOR AMAZON",
                "|",
                mode,
                "=",
                remaining,
                "sec remaining",
                flush=True,
            )

            await asyncio.sleep(
                min(
                    15,
                    max(1, remaining),
                )
            )
            continue

        started = time.monotonic()

        try:
            await ultra_hot_once()
        except Exception as e:
            print(
                "ULTRA HOT ERROR:",
                repr(e),
                flush=True
            )

        elapsed = (
            time.monotonic()
            - started
        )

        # الدورة التالية تبدأ كل 7 ثوانٍ تقريبًا
        await asyncio.sleep(
            max(
                1,
                (
                    10
                    if amazon_recovering()
                    else 7
                ) - elapsed
            )
        )


def model_tokens(title):
    title = str(title or "").upper()

    tokens = re.findall(
        r"\b[A-Z0-9][A-Z0-9._/-]{3,}\b",
        title
    )

    good = []

    for token in tokens:
        has_letter = any(c.isalpha() for c in token)
        has_digit = any(c.isdigit() for c in token)

        if has_letter and has_digit:
            good.append(token)

    return good[:4]



async def competitor_trigger_once():
    try:
        con = connect()

        rows = con.execute(
            """
            SELECT store,title,current_price,seen_at
            FROM market_observations
            WHERE lower(store) IN ('jumia','2b')
            ORDER BY seen_at DESC
            LIMIT 80
            """
        ).fetchall()

        con.close()

    except Exception as e:
        print(
            "COMPETITOR DB ERROR:",
            repr(e),
            flush=True
        )
        return

    seen = set()
    queued = 0

    for row in rows:
        title = row["title"]

        query = build_trigger_query(
            title
        )

        if not query:
            continue

        key = query.lower()

        if key in seen:
            continue

        seen.add(key)

        store = str(
            row["store"]
        ).lower()

        if enqueue_amazon_candidate(
            source=store,
            title=title,
            query=query,
            priority=100,
            boost_seconds=1200,
            kind="competitor",
        ):
            queued += 1

        if queued >= 8:
            break

    print(
        "🎯 COMPETITOR QUEUE",
        "| QUEUED =",
        queued,
        "| TOTAL QUEUE =",
        len(UNIFIED_AMAZON_QUEUE),
        flush=True
    )

async def competitor_loop():
    # خلي أول Discovery يشتغل الأول
    await asyncio.sleep(20)

    while True:
        try:
            await competitor_trigger_once()
        except Exception as e:
            print(
                "COMPETITOR LOOP ERROR:",
                repr(e),
                flush=True
            )

        # تحديث كل دقيقة
        await asyncio.sleep(60)


EXTERNAL_SOURCES = [
    {
        "name": "kenzz",
        "url": "https://kenzz.com/en",
        "interval": 60,
    },
    {
        "name": "yaoota",
        "url": "https://yaoota.online/eg/en/trending",
        "interval": 45,
    },
    {
        "name": "dealhunter",
        "url": "https://dealhunters.org/",
        "interval": 180,
    },
]

external_last_run = {}


def external_product_titles(html):
    soup = BeautifulSoup(html, "html.parser")

    ignore = {
        "home", "deals", "trending", "categories",
        "stores", "search", "about", "amazon",
        "jumia", "noon", "buy now", "get deal",
        "check price", "shop all",
    }

    titles = []
    seen = set()

    for el in soup.select("h2, h3, h4, a"):
        text = " ".join(
            el.get_text(" ", strip=True).split()
        )

        if not (12 <= len(text) <= 180):
            continue

        low = text.lower()

        if low in ignore:
            continue

        if low in seen:
            continue

        # Product-like text only.
        useful = (
            any(c.isdigit() for c in text)
            or len(text.split()) >= 4
        )

        if not useful:
            continue

        seen.add(low)
        titles.append(text)

    return titles[:20]



async def external_source_scan(source):
    name = source["name"]

    async with httpx.AsyncClient() as client:
        html = await fetch_html(
            client,
            source["url"]
        )

    if not html:
        print(
            "🌐 EXTERNAL SOURCE FAILED:",
            name,
            flush=True
        )
        return

    titles = external_product_titles(
        html
    )

    priority_map = {
        "kenzz": 95,
        "yaoota": 85,
        "dealhunter": 85,
    }

    queued = 0

    for title in titles:
        query = build_trigger_query(
            title
        )

        if not query:
            continue

        if enqueue_amazon_candidate(
            source=name,
            title=title,
            query=query,
            priority=priority_map.get(
                name,
                75
            ),
            boost_seconds=1200,
            kind="external",
        ):
            queued += 1

        if queued >= 8:
            break

    print(
        "🌐 EXTERNAL QUEUE",
        name,
        "| TITLES =",
        len(titles),
        "| QUEUED =",
        queued,
        "| TOTAL QUEUE =",
        len(UNIFIED_AMAZON_QUEUE),
        flush=True
    )

async def external_sources_loop():
    await asyncio.sleep(25)

    while True:
        now = time.monotonic()

        for source in EXTERNAL_SOURCES:
            name = source["name"]
            interval = source["interval"]

            last = external_last_run.get(
                name,
                0
            )

            if now - last < interval:
                continue

            external_last_run[name] = now

            try:
                await external_source_scan(
                    source
                )
            except Exception as e:
                print(
                    "EXTERNAL LOOP ERROR:",
                    name,
                    repr(e),
                    flush=True
                )

        await asyncio.sleep(10)


MANUAL_FILE = ROOT / ".amazon_manual_watch.txt"

EXTRA_STORE_INTERVALS = {
    "raya": 120,
    "btech": 180,
    "dream2000": 300,
}

extra_store_last = {}
manual_processed = set()



async def trigger_store_to_amazon(store):
    try:
        deals = await run_store(
            store
        )

    except Exception as e:
        print(
            "🎯 EXTRA STORE ERROR:",
            store,
            repr(e),
            flush=True
        )
        return

    deals = sorted(
        deals,
        key=lambda d: getattr(
            d,
            "discount_percent",
            0
        ),
        reverse=True
    )[:40]

    queued = 0
    seen = set()

    for d in deals:
        query = build_trigger_query(
            d.title
        )

        if not query:
            continue

        key = query.lower()

        if key in seen:
            continue

        seen.add(key)

        if enqueue_amazon_candidate(
            source=store,
            title=d.title,
            query=query,
            priority=90,
            boost_seconds=1200,
            kind="store",
        ):
            queued += 1

        if queued >= 10:
            break

    print(
        "🎯 EXTRA STORE QUEUE",
        store,
        "| DEALS =",
        len(deals),
        "| QUEUED =",
        queued,
        "| TOTAL QUEUE =",
        len(UNIFIED_AMAZON_QUEUE),
        flush=True
    )

async def extra_store_trigger_loop():
    await asyncio.sleep(30)

    while True:
        now = time.monotonic()

        for store, interval in EXTRA_STORE_INTERVALS.items():

            if (
                now
                - extra_store_last.get(store, 0)
                < interval
            ):
                continue

            extra_store_last[store] = now

            try:
                await trigger_store_to_amazon(
                    store
                )
            except Exception as e:
                print(
                    "EXTRA STORE LOOP ERROR:",
                    store,
                    repr(e),
                    flush=True
                )

        await asyncio.sleep(15)


async def manual_watch_loop():
    MANUAL_FILE.touch(exist_ok=True)

    await asyncio.sleep(8)

    while True:
        try:
            lines = MANUAL_FILE.read_text(
                encoding="utf-8"
            ).splitlines()

            for raw in lines:
                raw = raw.strip()

                if not raw or raw.startswith("#"):
                    continue

                m = re.search(
                    r"\b([A-Z0-9]{10})\b",
                    raw.upper()
                )

                if not m:
                    continue

                asin = m.group(1)

                if asin in manual_processed:
                    continue

                manual_processed.add(asin)

                if asin in watch:
                    watch[asin][
                        "priority_boost_until"
                    ] = int(time.time()) + 86400

                    watch[asin][
                        "manual_watch"
                    ] = True

                    print(
                        "📌 MANUAL -> ULTRA HOT:",
                        asin,
                        flush=True
                    )

                    continue

                async with httpx.AsyncClient() as client:
                    html = await fetch_html(
                        client,
                        AMAZON
                        + "/s?k="
                        + quote_plus(asin)
                    )

                items = parse_search(html)

                exact = next(
                    (
                        x for x in items
                        if x["asin"] == asin
                    ),
                    None
                )

                if not exact:
                    print(
                        "📌 MANUAL ASIN NOT FOUND:",
                        asin,
                        flush=True
                    )
                    continue

                add_product(
                    exact,
                    "manual"
                )

                watch[asin][
                    "priority_boost_until"
                ] = int(time.time()) + 86400

                watch[asin][
                    "manual_watch"
                ] = True

                async with lock:
                    save_files()

                print(
                    "📌 MANUAL NEW -> ULTRA HOT:",
                    asin,
                    flush=True
                )

        except Exception as e:
            print(
                "MANUAL WATCH ERROR:",
                repr(e),
                flush=True
            )

        await asyncio.sleep(5)


async def discovery_loop():
    while True:
        if not amazon_secondary_allowed():
            await asyncio.sleep(20)
            continue

        try:
            await deep_discovery()
        except Exception as e:
            print(
                "DISCOVERY LOOP ERROR:",
                repr(e),
                flush=True
            )

        # اكتشاف شامل كل 3 دقائق
        await asyncio.sleep(180)


async def hot_loop():
    # اسمح لأول discovery يبدأ
    await asyncio.sleep(12)

    while True:
        if not amazon_secondary_allowed():
            await asyncio.sleep(15)
            continue

        try:
            await hot_watch_once()
        except Exception as e:
            print(
                "HOT LOOP ERROR:",
                repr(e),
                flush=True
            )

        await asyncio.sleep(15)



# =========================================================
# V5 FULL WATCHLIST SWEEP
# Reviews ALL discovered Amazon products progressively.
# Hot/Ultra products keep their faster dedicated monitoring.
# =========================================================

FULL_V5_BATCH = 4


async def full_v5_watchlist_once():
    async with lock:
        items = [
            dict(x)
            for x in watch.values()
            if x.get("asin")
            and x.get("url")
        ]

    if not items:
        return

    # Products already checked recently by HOT/ULTRA naturally
    # move toward the back. Cold/unvisited products come first.
    def check_age(rec):
        return max(
            int(rec.get("v5_full_checked", 0) or 0),
            int(rec.get("last_checked", 0) or 0),
            int(rec.get("ultra_checked", 0) or 0),
        )

    items.sort(
        key=check_age
    )

    batch = items[:FULL_V5_BATCH]

    semaphore = asyncio.Semaphore(2)

    async with httpx.AsyncClient() as client:

        async def check(rec):
            async with semaphore:
                current = await live_price(
                    client,
                    rec["url"]
                )

                if current is None:
                    return {
                        "asin": rec["asin"],
                        "price": None,
                    }

                # V5 evaluates EVERY successfully fetched price.
                sent, verified_price = (
                    await maybe_send_v5_review(
                        client,
                        rec,
                        current
                    )
                )

                final_price = (
                    to_float(verified_price)
                    or to_float(current)
                )

                return {
                    "asin": rec["asin"],
                    "price": final_price,
                    "sent": bool(sent),
                }

        results = await asyncio.gather(
            *[
                check(rec)
                for rec in batch
            ]
        )

    now = int(time.time())

    async with lock:
        live_count = 0
        sent_count = 0

        for result in results:
            asin = result.get("asin")

            if asin not in watch:
                continue

            # Mark it checked even if Amazon temporarily
            # did not return a usable price.
            watch[asin]["v5_full_checked"] = now

            price = to_float(
                result.get("price")
            )

            if price <= 0:
                continue

            live_count += 1

            if result.get("sent"):
                sent_count += 1

            watch[asin]["last_price"] = price

            # Build stable anchors using the same price
            # already fetched. No extra request here.
            record_price_sample(
                watch[asin],
                price
            )

            if price > to_float(
                watch[asin].get(
                    "max_seen_price"
                )
            ):
                watch[asin][
                    "max_seen_price"
                ] = price

            old_min = to_float(
                watch[asin].get(
                    "min_seen_price"
                )
            )

            if (
                old_min <= 0
                or price < old_min
            ):
                watch[asin][
                    "min_seen_price"
                ] = price

        save_files()

    print(
        "🌍 V5 FULL SWEEP",
        datetime.now().strftime("%H:%M:%S"),
        "| CHECKED =",
        len(batch),
        "| LIVE =",
        live_count,
        "| REVIEWS =",
        sent_count,
        "| WATCHLIST =",
        len(items),
        flush=True
    )


async def full_v5_watchlist_loop():
    # Let Ultra/Hot warm up first.
    await asyncio.sleep(15)

    while True:
        try:
            if amazon_circuit_open():
                await asyncio.sleep(20)
                continue

            await full_v5_watchlist_once()

        except Exception as exc:
            print(
                "V5 FULL SWEEP ERROR:",
                repr(exc),
                flush=True
            )

        # Adaptive speed.
        # Healthy Amazon -> faster complete catalogue rotation.
        if AMAZON_SUCCESS_STREAK >= 8:
            delay = 8
        elif AMAZON_SUCCESS_STREAK >= 3:
            delay = 12
        else:
            delay = 20

        await asyncio.sleep(delay)




# =========================================================
# V7 AMAZON PROMO CAMPAIGN CRAWLER
# Campaign page -> child campaign pages -> exact product ASINs.
# Every ASIN is handed to the existing exact-product queue,
# so live-price / promo-stack verification remains authoritative.
# =========================================================

PROMO_CAMPAIGN_SEEDS = [
    AMAZON + "/deals",
    AMAZON + "/gp/goldbox",
]

PROMO_CAMPAIGN_QUEUE = list(PROMO_CAMPAIGN_SEEDS)
PROMO_CAMPAIGN_SEEN = {}
PROMO_CAMPAIGN_MAX_QUEUE = 240
PROMO_CAMPAIGN_TTL = 3600


def queue_promo_campaign(url):
    url = str(url or "").strip()

    if not url or "amazon.eg" not in url.lower():
        return False

    now = time.time()
    last = float(
        PROMO_CAMPAIGN_SEEN.get(url, 0) or 0
    )

    if now - last < PROMO_CAMPAIGN_TTL:
        return False

    if url in PROMO_CAMPAIGN_QUEUE:
        return False

    PROMO_CAMPAIGN_QUEUE.append(url)

    if (
        len(PROMO_CAMPAIGN_QUEUE)
        > PROMO_CAMPAIGN_MAX_QUEUE
    ):
        del PROMO_CAMPAIGN_QUEUE[
            :-PROMO_CAMPAIGN_MAX_QUEUE
        ]

    return True


async def promo_campaign_once():
    if not amazon_secondary_allowed():
        return {
            "page": None,
            "products": 0,
            "links": 0,
            "queued_products": 0,
        }

    now = time.time()

    for seed in PROMO_CAMPAIGN_SEEDS:
        last = float(
            PROMO_CAMPAIGN_SEEN.get(seed, 0) or 0
        )

        if (
            now - last >= PROMO_CAMPAIGN_TTL
            and seed not in PROMO_CAMPAIGN_QUEUE
        ):
            PROMO_CAMPAIGN_QUEUE.append(seed)

    if not PROMO_CAMPAIGN_QUEUE:
        return {
            "page": None,
            "products": 0,
            "links": 0,
            "queued_products": 0,
        }

    url = PROMO_CAMPAIGN_QUEUE.pop(0)
    PROMO_CAMPAIGN_SEEN[url] = now

    async with httpx.AsyncClient() as client:
        html = await fetch_html(client, url)

    if not html:
        print(
            "🎯 PROMO CAMPAIGN FETCH FAILED:",
            url,
            flush=True,
        )
        return {
            "page": url,
            "products": 0,
            "links": 0,
            "queued_products": 0,
        }

    targets = extract_campaign_targets(
        html,
        url,
    )

    products = (
        targets.get("products")
        or []
    )
    links = (
        targets.get("campaign_links")
        or []
    )

    added_links = 0

    for child in links:
        if queue_promo_campaign(child):
            added_links += 1

    queued_products = 0

    for item in products:
        asin = str(
            item.get("asin")
            or ""
        ).upper().strip()

        if not asin:
            continue

        if enqueue_amazon_candidate(
            source="amazon_promo_campaign",
            title=item.get("title") or "",
            url=asin_url(asin),
            priority=98,
            boost_seconds=1800,
            kind="campaign",
        ):
            queued_products += 1

    print(
        "🎯 PROMO CAMPAIGN",
        "| PAGE =", url,
        "| PRODUCTS =", len(products),
        "| QUEUED =", queued_products,
        "| CHILD LINKS =", added_links,
        "| CAMPAIGN QUEUE =",
        len(PROMO_CAMPAIGN_QUEUE),
        flush=True,
    )

    return {
        "page": url,
        "products": len(products),
        "links": added_links,
        "queued_products": queued_products,
    }


async def promo_campaign_loop():
    await asyncio.sleep(12)

    while True:
        try:
            await promo_campaign_once()
        except Exception as exc:
            print(
                "PROMO CAMPAIGN LOOP ERROR:",
                type(exc).__name__,
                str(exc)[:180],
                flush=True,
            )

        await asyncio.sleep(45)



async def main():
    print(
        "🚀 AMAZON MULTI-SOURCE RADAR STARTED",
        flush=True
    )

    await asyncio.gather(
        discovery_loop(),
        hot_loop(),
        ultra_hot_loop(),
        competitor_loop(),
        external_sources_loop(),
        extra_store_trigger_loop(),
        manual_watch_loop(),
        unified_queue_loop(),
        promo_campaign_loop(),
        full_v5_watchlist_loop()
    )


if __name__ == "__main__":
    asyncio.run(main())
