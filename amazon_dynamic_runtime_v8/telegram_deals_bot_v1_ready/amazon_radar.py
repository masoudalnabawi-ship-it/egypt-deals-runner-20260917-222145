
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
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup
from rapidfuzz import fuzz
from dotenv import load_dotenv

from engine import run_store
from db import connect
from stores.base import parse_price

# AMAZON_INDEPENDENT_REVIEW_BRIDGE_FINAL
AMAZON_REVIEW_INBOX = ""  # resolved after ROOT is initialized

class _AmazonLocalReviewResponse:
    status_code = 200
    text = "queued_to_independent_amazon_bot"

    def json(self):
        return {
            "ok": True,
            "local_queue": True
        }


async def _capture_amazon_page_screenshot(url):
    """Capture a real Amazon product-page viewport for Telegram review."""
    import base64

    if not str(url or "").startswith("http"):
        return None

    try:
        from playwright.async_api import async_playwright
    except Exception as exc:
        print("📸 AMAZON SCREENSHOT SKIP playwright:", repr(exc), flush=True)
        return None

    browser = None

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                channel="chrome",
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )

            context = await browser.new_context(
                viewport={"width": 720, "height": 1280},
                device_scale_factor=1,
                is_mobile=True,
                has_touch=True,
                locale="ar-EG",
                user_agent=(
                    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/152.0.0.0 Mobile Safari/537.36"
                ),
                extra_http_headers={
                    "Accept-Language": "ar-EG,ar;q=0.9,en;q=0.8"
                },
            )

            page = await context.new_page()

            await page.goto(
                str(url),
                wait_until="domcontentloaded",
                timeout=15000,
            )

            await page.wait_for_timeout(1500)

            title = (await page.title()).lower()

            try:
                body = (await page.locator("body").inner_text(timeout=3000)).lower()
            except Exception:
                body = ""

            blocked = (
                "robot check" in title
                or "enter the characters you see below" in body
                or "أدخل الأحرف التي تراها" in body
                or "captcha" in title
            )

            if blocked:
                print("📸 AMAZON SCREENSHOT SKIP captcha/robot-check", flush=True)
                await context.close()
                await browser.close()
                return None

            markers = await page.locator(
                "#productTitle, #title, #title_feature_div, input#ASIN, #dp"
            ).count()

            if markers <= 0:
                print("📸 AMAZON SCREENSHOT SKIP product-page marker missing", flush=True)
                await context.close()
                await browser.close()
                return None

            shot = await page.screenshot(
                type="jpeg",
                quality=62,
                full_page=False,
            )

            await context.close()
            await browser.close()

            if not shot or len(shot) > 7_000_000:
                print("📸 AMAZON SCREENSHOT SKIP invalid size", flush=True)
                return None

            print(
                "📸 AMAZON PAGE SCREENSHOT OK",
                len(shot),
                "bytes",
                flush=True,
            )

            return {
                "page_screenshot_b64": base64.b64encode(shot).decode("ascii"),
                "page_screenshot_mime": "image/jpeg",
            }

    except Exception as exc:
        print("📸 AMAZON SCREENSHOT ERROR", repr(exc), flush=True)
        try:
            if browser:
                await browser.close()
        except Exception:
            pass
        return None


async def _send_amazon_independent_review(payload):
    try:
        shot = await asyncio.wait_for(
            _capture_amazon_page_screenshot(payload.get("url")),
            timeout=20,
        )
        if shot:
            payload.update(shot)
    except Exception as exc:
        print("📸 AMAZON SCREENSHOT TIMEOUT/ERROR", repr(exc), flush=True)

    """Send Amazon review candidates to the existing Cloudflare moderation API.

    GitHub Actions has no always-on local review bot, so Cloudflare owns
    Telegram delivery, approval/rejection callbacks, and D1 persistence.
    """
    import json as _json
    import urllib.request as _urlreq

    class _CloudReviewResponse:
        def __init__(self, status_code, text, data=None):
            self.status_code = status_code
            self.text = text
            self._data = data or {}
        def json(self):
            return self._data

    url = CLOUD_BASE.rstrip("/") + "/api/deals"
    body = _json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")

    def _post():
        req = _urlreq.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "EgyptDealsAmazonRadar-GitHub/1.0",
                "x-api-key": API_KEY,
            },
            method="POST",
        )
        with _urlreq.urlopen(req, timeout=35) as resp:
            text = resp.read().decode("utf-8", "replace")
            try:
                data = _json.loads(text)
            except Exception:
                data = {}
            results = data.get("results") or []
            accepted = bool(data.get("ok")) and all(
                bool(x.get("ok"))
                for x in results
                if isinstance(x, dict)
            )
            code = int(resp.status) if accepted else 422
            return _CloudReviewResponse(code, text, data)

    try:
        r = await asyncio.to_thread(_post)
        print("☁️ AMAZON CLOUD REVIEW", payload.get("asin") or payload.get("fingerprint"), "|", r.status_code, flush=True)
        return r
    except Exception as exc:
        print("❌ AMAZON CLOUD REVIEW ERROR", repr(exc), flush=True)
        raise

load_dotenv(".env")

ROOT = Path(__file__).resolve().parent
# Termux-safe review queue. The installer may override this with an env value.
AMAZON_REVIEW_INBOX = os.getenv(
    "AMAZON_REVIEW_INBOX",
    str(ROOT.parent / "amazon_deals_bot_ready" / "inbox.jsonl"),
)
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



# AMAZON_INTEL_V2_ACTIVE
from amazon_intel_v2 import (
    record_price_sample,
    evaluate_shadow,
    compute_promo_stack,
)

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


        # AMAZON_IMAGE_PIPELINE_V1
        # Reuse image already present on Amazon result card.
        # NO extra HTTP request.
        image_url = ""

        try:
            img = card.select_one(
                "img.s-image, img[data-image-latency], img"
            )

            if img:
                for attr in (
                    "src",
                    "data-src",
                    "data-old-hires"
                ):
                    value = str(
                        img.get(attr) or ""
                    ).strip()

                    if value.startswith("http"):
                        image_url = value
                        break

                if not image_url:
                    srcset = str(
                        img.get("srcset") or ""
                    ).strip()

                    if srcset:
                        candidates = [
                            x.strip().split(" ")[0]
                            for x in srcset.split(",")
                            if x.strip()
                        ]

                        candidates = [
                            x for x in candidates
                            if x.startswith("http")
                        ]

                        if candidates:
                            image_url = candidates[-1]

        except Exception:
            image_url = ""

        listing_promo_text = ""
        listing_coupon_percent = 0.0
        try:
            promo_parts = []
            for el in card.select(
                "[class*='coupon'], [id*='coupon'], "
                "[class*='promo'], [id*='promo'], "
                ".s-coupon-unclipped, .a-color-success"
            )[:20]:
                txt = el.get_text(" ", strip=True)
                if txt and any(k in txt.lower() for k in (
                    "coupon", "discount", "save", "promo",
                    "كوبون", "قسيمة", "خصم", "وفر", "توفير"
                )):
                    promo_parts.append(txt)
            listing_promo_text = " | ".join(dict.fromkeys(promo_parts))[:800]
            m = re.search(r"(\d+(?:\.\d+)?)\s*%", listing_promo_text)
            if m:
                listing_coupon_percent = float(m.group(1))
        except Exception:
            pass

        found.append({
            "asin": asin,
            "title": title_el.get_text(
                " ",
                strip=True
            ),
            "url": asin_url(asin),
            "image_url": image_url,
            "current_price": float(current),
            "old_price": (
                float(old)
                if old
                else None
            ),
            "listing_promo_text": listing_promo_text,
            "listing_coupon_percent": listing_coupon_percent,
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

    # Persist Amazon product image.
    # ASIN remains internal; image travels with the deal payload.
    image_url = str(
        item.get("image_url") or ""
    ).strip()

    if image_url.startswith("http"):
        rec["image_url"] = image_url

    title_ar = str(
        item.get("title_ar") or ""
    ).strip()

    if title_ar:
        rec["title_ar"] = title_ar

    listing_promo_text = str(item.get("listing_promo_text") or "").strip()
    listing_coupon_percent = to_float(item.get("listing_coupon_percent"))
    if listing_promo_text:
        rec["listing_promo_text"] = listing_promo_text
    if listing_coupon_percent > 0:
        rec["listing_coupon_percent"] = listing_coupon_percent
        # Listing badges are a discovery hint only. Product-page verification
        # still decides whether the promotion is real/public. 50%+ coupon
        # hints jump to the fastest lane so they are not lost in a huge dept.
        if listing_coupon_percent >= 50:
            rec["priority_zero"] = True
            rec["anomaly_priority"] = max(100, int(rec.get("anomaly_priority", 0) or 0))
            rec["priority_boost_until"] = max(
                int(time.time()) + 1800,
                int(rec.get("priority_boost_until", 0) or 0),
            )

    watch[asin] = rec

    # AMAZON UNIVERSAL ANOMALY - uses data already fetched.
    if old > current:
        rec["search_old_price"] = max(
            to_float(rec.get("search_old_price")),
            old,
        )

    record_price_sample(rec, current)
    anomaly = evaluate_shadow(rec, current)

    anomaly_keys = (
        "anomaly_tier",
        "anomaly_reason",
        "anomaly_confidence",
        "anomaly_reference",
        "anomaly_drop",
        "anomaly_saving",
        "anomaly_cold_start",
        "anomaly_verified",
        "anomaly_reference_source",
        "anomaly_priority",
    )

    candidate_keys = (
        "value_candidate_tier",
        "value_candidate_reason",
        "value_candidate_confidence",
        "value_candidate_reference",
        "value_candidate_drop",
        "value_candidate_saving",
        "value_candidate_cold_start",
        "value_candidate_source",
        "value_candidate_priority",
    )

    # Always rebuild intelligence state from the
    # single active V9.6 evaluation.
    for k in anomaly_keys:
        rec.pop(k, None)

    for k in candidate_keys:
        rec.pop(k, None)

    if (
        anomaly
        and anomaly.get("tier")
        in ("HOT", "ULTRA", "CRITICAL")
    ):
        tier = str(
            anomaly.get("tier")
        ).upper()

        tier_priority = {
            "HOT": 80,
            "ULTRA": 95,
            "CRITICAL": 110,
        }.get(
            tier,
            70
        )

        is_verified = bool(
            anomaly.get("verified")
        )

        if is_verified:
            # ---------------------------------
            # FINAL VERIFIED VALUE ANOMALY
            # ---------------------------------

            rec["anomaly_tier"] = tier

            rec["anomaly_reason"] = (
                anomaly.get("reason")
            )

            rec["anomaly_confidence"] = (
                anomaly.get("confidence")
            )

            rec["anomaly_reference"] = (
                anomaly.get("reference")
            )

            rec["anomaly_drop"] = (
                anomaly.get("drop")
            )

            rec["anomaly_saving"] = (
                anomaly.get("saving")
            )

            rec["anomaly_cold_start"] = bool(
                anomaly.get("cold_start")
            )

            rec["anomaly_verified"] = True

            rec[
                "anomaly_reference_source"
            ] = anomaly.get(
                "reference_source"
            )

            rec["anomaly_priority"] = max(
                int(
                    rec.get(
                        "anomaly_priority",
                        0
                    ) or 0
                ),
                tier_priority,
            )

        else:
            # ---------------------------------
            # UNVERIFIED VALUE CANDIDATE
            # It must be exact-checked first.
            # ---------------------------------

            rec[
                "value_candidate_tier"
            ] = tier

            rec[
                "value_candidate_reason"
            ] = anomaly.get("reason")

            rec[
                "value_candidate_confidence"
            ] = anomaly.get("confidence")

            rec[
                "value_candidate_reference"
            ] = anomaly.get("reference")

            rec[
                "value_candidate_drop"
            ] = anomaly.get("drop")

            rec[
                "value_candidate_saving"
            ] = anomaly.get("saving")

            rec[
                "value_candidate_cold_start"
            ] = bool(
                anomaly.get("cold_start")
            )

            rec[
                "value_candidate_source"
            ] = anomaly.get(
                "reference_source"
            )

            rec[
                "value_candidate_priority"
            ] = max(
                50,
                tier_priority - 15
            )

        rec["manual_watch"] = True

        rec[
            "priority_boost_until"
        ] = max(
            int(
                rec.get(
                    "priority_boost_until",
                    0
                ) or 0
            ),
            int(time.time()) + 7200,
        )

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


def amazon_circuit_open():
    return time.monotonic() < AMAZON_CIRCUIT_UNTIL


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
                if amazon_priority_url(url):
                    note_amazon_200()
                else:
                    note_amazon_search_200()

                return r.text

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
            "seen_at": int(time.time()),
        }

        # Persist exact Amazon page metadata too.
        # This means review cards keep the real Amazon image.
        if asin in watch:
            rec = watch[asin]

            if image_url.startswith("http"):
                rec["image_url"] = image_url

            if title_ar:
                rec["title_ar"] = title_ar

            if old_price > 0:
                rec["amazon_old_price"] = old_price
                rec[
                    "amazon_old_price_verified"
                ] = True

            rec["amazon_meta_seen_at"] = int(
                time.time()
            )

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



# =========================================================
# AMAZON_UNIVERSAL_PROMO_V1
# Universal checkout/product-page promotions only.
# Account-specific / bank / member offers are NOT stacked
# into the public effective price.
# =========================================================
def _amazon_universal_promo_patch(soup, promo):
    """
    AMAZON UNIVERSAL PROMO V2

    Detect public Amazon promotions separately from:
    - bank/card discounts
    - Prime/member offers
    - selected-account/personalized offers

    Never stacks account-specific discounts into public price.
    """
    try:
        result = dict(promo or {})

        # Dynamic Promo Scanner already performs stricter public/restricted
        # separation. Keep this legacy patch only as a fallback.
        if result.get("dynamic_scan_version"):
            return result

        chunks = []
        seen = set()

        selectors = (
            "#quickPromoBucketContent",
            "#promoPriceBlockMessage_feature_div",
            "#promotions_feature_div",
            "#vpcButton",
            "[id*='promo']",
            "[class*='promo']",
        )

        for selector in selectors:
            try:
                for node in soup.select(selector):
                    text = " ".join(
                        node.stripped_strings
                    )
                    text = re.sub(
                        r"\s+",
                        " ",
                        text
                    ).strip()

                    if (
                        3 <= len(text) <= 1500
                        and text not in seen
                    ):
                        seen.add(text)
                        chunks.append(text)
            except Exception:
                pass

        # Also inspect short individual page lines.
        try:
            page_lines = soup.get_text(
                "\n",
                strip=True
            ).splitlines()

            for line in page_lines:
                text = re.sub(
                    r"\s+",
                    " ",
                    line
                ).strip()

                low = text.lower()

                if (
                    "%" in text
                    and len(text) <= 500
                    and any(
                        k in low
                        for k in (
                            "خصم",
                            "وفر",
                            "توفير",
                            "عرض",
                            "شراء",
                            "اشتر",
                            "save",
                            "discount",
                            "promotion",
                            "promo",
                            "checkout",
                            "buy",
                        )
                    )
                    and text not in seen
                ):
                    seen.add(text)
                    chunks.append(text)
        except Exception:
            pass

        patterns = (
            r"(?:خصم|وفر|توفير)\s*(?:بنسبة\s*)?(\d+(?:\.\d+)?)\s*%",
            r"(\d+(?:\.\d+)?)\s*%\s*(?:خصم|توفير)",
            r"(?:save|discount)\s*(?:up\s*to\s*)?(\d+(?:\.\d+)?)\s*%",
        )

        # These make the percentage NON-universal.
        excluded = (
            "بطاقة",
            "بطاقتك",
            "بنك",
            "فيزا",
            "ماستركارد",
            "visa",
            "mastercard",
            "bank card",
            "credit card",
            "debit card",
            "prime only",
            "member only",
            "أعضاء برايم",
            "برايم فقط",
            "لحسابات مؤهلة",
            "لحسابات محددة",
            "خاص بحسابك",
            "selected accounts",
            "eligible accounts",
            "personalized offer",
        )

        # Require promotional context so a normal "-50%"
        # price badge is NOT treated as another 50% checkout promo.
        promo_context = (
            "عند الشراء",
            "عند شراء",
            "عند الدفع",
            "اشتر",
            "شراء",
            "عرض",
            "ترويجي",
            "promotion",
            "promo",
            "when you buy",
            "buy 1",
            "buy one",
            "checkout",
        )

        best = 0.0
        best_text = ""

        for chunk in chunks:
            low = chunk.lower()

            if any(
                word.lower() in low
                for word in excluded
            ):
                continue

            if not any(
                word.lower() in low
                for word in promo_context
            ):
                continue

            for pattern in patterns:
                for m in re.finditer(
                    pattern,
                    chunk,
                    re.I
                ):
                    try:
                        pct = float(
                            m.group(1)
                        )
                    except Exception:
                        continue

                    if not 5 <= pct <= 95:
                        continue

                    if pct > best:
                        best = pct
                        best_text = chunk[:500]

        existing_type = str(
            result.get(
                "promo_type",
                "none"
            )
            or "none"
        ).lower()

        existing_percent = float(
            result.get(
                "promo_percent",
                0
            )
            or 0
        )

        # A public promo outranks a bank/member offer.
        if (
            best >= 5
            and (
                existing_type in (
                    "none",
                    "card",
                    "bank_card",
                    "member",
                )
                or best >= existing_percent
            )
        ):
            result.update({
                "promo_type": "percent",
                "promo_verified": True,
                "promo_percent": best,
                "coupon_value": 0.0,
                "conditional": False,
                "member_only": False,
                "promo_scope": "universal",
                "details":
                    f"Amazon public promotion {best:g}%",
                "promo_text": best_text,
            })

        # Merely flag account-specific offers.
        # NEVER include their value in public effective price.
        all_text = " ".join(chunks).lower()

        if any(
            x in all_text
            for x in (
                "لحسابات مؤهلة",
                "لحسابات محددة",
                "خاص بحسابك",
                "selected accounts",
                "eligible accounts",
                "personalized",
            )
        ):
            result["account_specific"] = True

        return result

    except Exception:
        return promo or {}


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

        promo = _amazon_universal_promo_patch(
            soup,
            promo
        )

        AMAZON_PROMO_CACHE[
            asin
        ] = promo

        # PUBLIC VERIFIED 5%+ FAST REVIEW POLICY
        if asin in watch:
            now_ts = int(time.time())

            promo_type = str(
                promo.get("promo_type", "none") or "none"
            ).strip().lower()

            promo_scope = str(
                promo.get("promo_scope", "") or ""
            ).strip().lower()

            promo_verified = bool(
                promo.get("promo_verified", False)
            )

            conditional = bool(
                promo.get("conditional", False)
            )

            restricted = (
                bool(promo.get("member_only", False))
                or bool(promo.get("account_specific", False))
                or bool(promo.get("bank_only", False))
                or promo_scope in ("bank", "member", "account")
                or promo_type in (
                    "card",
                    "bank_card",
                    "account_offer",
                )
            )

            public_coupon = (
                promo_type == "coupon"
                and promo_scope == "coupon"
                and not restricted
            )

            try:
                pct = float(
                    promo.get("promo_percent", 0) or 0
                )
            except Exception:
                pct = 0.0

            public_verified = (
                promo_verified
                and not restricted
                and promo_type != "bulk_discount"
                and (
                    not conditional
                    or public_coupon
                )
            )

            special_public = promo_type in (
                "coupon",
                "bogo",
                "buy_1_get_1",
                "buy1get1",
                "flash",
            )

            if public_verified and (
                pct >= 5.0
                or special_public
            ):
                if pct >= 50:
                    priority = 100
                elif pct >= 30:
                    priority = 90
                elif pct >= 15:
                    priority = 80
                elif pct >= 5:
                    priority = 70
                else:
                    priority = 75

                watch[asin]["promo_priority"] = max(
                    priority,
                    int(
                        watch[asin].get(
                            "promo_priority",
                            0
                        ) or 0
                    ),
                )

                watch[asin]["priority_boost_until"] = max(
                    now_ts + 1800,
                    int(
                        watch[asin].get(
                            "priority_boost_until",
                            0
                        ) or 0
                    ),
                )

                watch[asin]["manual_watch"] = True

                print(
                    "⚡ PUBLIC PROMO FAST QUEUE",
                    asin,
                    "|",
                    round(pct, 1),
                    "%",
                    "| PRIORITY =",
                    priority,
                    flush=True,
                )

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

            "promo_scope":
                promo.get("promo_scope", ""),

            "account_specific":
                bool(
                    promo.get(
                        "account_specific",
                        False
                    )
                ),

            "member_only":
                bool(
                    promo.get(
                        "member_only",
                        False
                    )
                ),

            "bank_only":
                bool(
                    promo.get(
                        "bank_only",
                        False
                    )
                ),

            "applies_to_all":
                bool(
                    promo.get(
                        "applies_to_all",
                        False
                    )
                ),

            "general_50_plus":
                bool(
                    promo.get(
                        "general_50_plus",
                        False
                    )
                ),

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

            "promo_source":
                promo.get(
                    "promo_source",
                    ""
                ),

            "bank_offers":
                promo.get(
                    "bank_offers",
                    []
                ),

            "member_offers":
                promo.get(
                    "member_offers",
                    []
                ),

            "account_offers":
                promo.get(
                    "account_offers",
                    []
                ),

            "public_offers":
                promo.get(
                    "public_offers",
                    []
                ),

            "bulk_offers":
                promo.get(
                    "bulk_offers",
                    []
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
                + item["asin"],

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

_AMAZON_ORIGINAL_IS_HOT = is_hot

def is_hot(rec):
    now = int(time.time())

    if rec.get("priority_zero"):
        return True

    if (
        int(rec.get("anomaly_priority") or 0) >= 90
        and int(rec.get("priority_boost_until") or 0) > now
    ):
        return True

    return _AMAZON_ORIGINAL_IS_HOT(rec)



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

    r = await _send_amazon_independent_review(payload)

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



# =========================================================
# AMAZON_CATEGORY_ROTATION_V2
# Balanced whole-store coverage without increasing request rate.
# =========================================================

_AMAZON_DISCOVERY_GROUP_V1 = amazon_discovery_group

def amazon_discovery_group(term):
    t = str(term or "").lower()

    extra_groups = {
        "fashion": (
            "fashion","clothing","shirt","jeans","dress","shoes",
            "sneakers","bags","watches","men fashion","women fashion"
        ),
        "sports": (
            "sports","fitness","gym","exercise","treadmill",
            "cycling","football","outdoor sports"
        ),
        "auto_tools": (
            "automotive","car accessories","car care","motorcycle",
            "tools","power tools","hardware"
        ),
        "grocery": (
            "grocery","food","beverages","coffee","tea","snacks",
            "household supplies"
        ),
        "toys": (
            "toys","games","lego","puzzles","kids toys"
        ),
        "office_books": (
            "office","stationery","books","school supplies",
            "printers","office products"
        ),
        "pet": (
            "pet","pet supplies","cat","dog","pet food"
        ),
    }

    for group, words in extra_groups.items():
        if any(w in t for w in words):
            return group

    return _AMAZON_DISCOVERY_GROUP_V1(term)


# Add missing department discovery terms.
for _v2_term in [
    "fashion deals",
    "men fashion",
    "women fashion",
    "shoes deals",
    "sports fitness",
    "exercise equipment",
    "automotive accessories",
    "car accessories",
    "power tools",
    "grocery deals",
    "food beverages",
    "toys games",
    "office products",
    "books stationery",
    "pet supplies",
]:
    if _v2_term not in EXTRA_AMAZON_SEARCH_TERMS:
        EXTRA_AMAZON_SEARCH_TERMS.append(_v2_term)


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


async def process_queue_item(client, job):
    if job["url"]:
        amazon_url = job["url"]
    else:
        amazon_url = (
            AMAZON
            + "/s?k="
            + quote_plus(job["query"])
        )

    html = await fetch_html(
        client,
        amazon_url
    )

    if not html:
        return False

    items = parse_search(html)

    # AMAZON_FULL_PAGINATION_V1:
    # Detect final/empty result page without another Amazon request.
    if str(job.get("source") or "").startswith("amazon_v7:"):
        term = str(job.get("query") or "").strip()

        if term:
            try:
                u = str(job.get("url") or "")
                pm = re.search(r"[?&]page=(\d+)", u)
                current_page = int(pm.group(1)) if pm else 1

                soup_page = BeautifulSoup(html, "html.parser")
                page_numbers = []

                for a in soup_page.select(
                    'a.s-pagination-item[href*="page="]'
                ):
                    mm = re.search(
                        r"[?&]page=(\d+)",
                        str(a.get("href") or "")
                    )
                    if mm:
                        page_numbers.append(int(mm.group(1)))

                visible_last = max(page_numbers) if page_numbers else 0
                key = _amazon_v7_page_key(term)

                if (
                    not items
                    or (
                        visible_last > 0
                        and current_page >= visible_last
                    )
                ):
                    state[key] = 1

                    print(
                        "🔄 AMAZON PAGE SWEEP COMPLETE",
                        "| TERM =", term,
                        "| LAST =", current_page,
                        "| RESET=1",
                        flush=True
                    )

                    async with lock:
                        save_files()

            except Exception as exc:
                print(
                    "⚠️ AMAZON PAGE STATE ERROR",
                    repr(exc),
                    flush=True
                )

    if not items:
        return True

    # Broad Amazon discovery page.
    if job["kind"] == "discovery":
        added = 0

        for item in items[:24]:
            before = len(watch)

            add_product(
                item,
                "queue:"
                + job["source"]
            )

            if len(watch) > before:
                added += 1

        async with lock:
            save_files()

        print(
            "🔎 QUEUE DISCOVERY",
            job["source"],
            "| FOUND =",
            len(items),
            "| NEW =",
            added,
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
    await asyncio.sleep(20)

    async with httpx.AsyncClient() as client:

        while True:
            # Product-page Ultra/Hot may remain healthy while
            # Amazon Search itself is throttled.
            if amazon_search_backoff_active():
                await asyncio.sleep(5)
                continue

            if not amazon_secondary_allowed():
                await asyncio.sleep(5)
                continue

            job = pop_amazon_candidate()

            if not job:
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
            if job.get("priority", 0) >= 85:
                queue_delay = 45
            elif job.get("priority", 0) >= 50:
                queue_delay = 75
            else:
                queue_delay = 120

            await asyncio.sleep(
                queue_delay
            )





# =========================================================
# AMAZON_FULL_PAGINATION_V1
# Progressive full-page coverage.
# Uses existing queue/governor/backoff. No request-rate increase.
# =========================================================

def _amazon_v7_page_key(term):
    return "v7_full_page:" + str(term or "").strip().lower()


async def deep_discovery():
    # Full progressive Amazon coverage.
    # One page from each major group per rotation.
    # Pages advance persistently until Amazon shows the end.

    soft_limit = int(UNIFIED_QUEUE_LIMIT * 0.75)

    if len(UNIFIED_AMAZON_QUEUE) >= soft_limit:
        print(
            "⏳ DISCOVERY V7 SKIPPED | QUEUE =",
            len(UNIFIED_AMAZON_QUEUE),
            flush=True
        )
        return

    group_order = [
        "electronics",
        "home",
        "beauty_baby",
        "fashion",
        "sports",
        "auto_tools",
        "grocery",
        "toys",
        "office_books",
        "pet",
        "used",
        "other",
    ]

    stats = {}
    pages = {}

    for group in group_order:
        terms = [
            t for t in EXTRA_AMAZON_SEARCH_TERMS
            if amazon_discovery_group(t) == group
        ]

        if not terms:
            stats[group] = 0
            continue

        idx_key = "v7_full_term_idx:" + group

        try:
            idx = int(state.get(idx_key, 0)) % len(terms)
        except Exception:
            idx = 0

        term = terms[idx]
        page_key = _amazon_v7_page_key(term)

        try:
            page_no = max(1, int(state.get(page_key, 1)))
        except Exception:
            page_no = 1

        base = AMAZON + "/s?k=" + quote_plus(term)
        url = base if page_no == 1 else base + "&page=" + str(page_no)

        queued = enqueue_amazon_candidate(
            source="amazon_v7:" + group,
            query=term,
            url=url,
            priority=30,
            boost_seconds=600,
            kind="discovery",
        )

        # Always rotate terms so one slow query cannot monopolize a group.
        state[idx_key] = (idx + 1) % len(terms)

        if queued:
            # Optimistically move forward.
            # process_queue_item resets to page 1 on last/empty page.
            state[page_key] = page_no + 1
            stats[group] = 1
            pages[group] = page_no
        else:
            stats[group] = 0

    async with lock:
        save_files()

    print(
        "🌍 AMAZON FULL PAGE ROTATION",
        "| TOTAL =", sum(stats.values()),
        "| PAGES =", pages,
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
        "priority": tier.lower(),
        "live_rechecked": True,
        "asin": rec["asin"],

        # Intelligence metadata
        "intelligence_tier": tier,
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

    r = await _send_amazon_independent_review(payload)

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

        # V9.6 SINGLE SOURCE OF TRUTH:
        # persist the result of the exact Amazon re-check.
        if (
            second_intel
            and second_intel.get("tier")
            in ("HOT", "ULTRA", "CRITICAL")
        ):
            rec["anomaly_tier"] = second_intel.get("tier")
            rec["anomaly_reason"] = second_intel.get("reason")
            rec["anomaly_confidence"] = second_intel.get("confidence")
            rec["anomaly_reference"] = second_intel.get("reference")
            rec["anomaly_drop"] = second_intel.get("drop")
            rec["anomaly_saving"] = second_intel.get("saving")
            rec["anomaly_cold_start"] = bool(
                second_intel.get("cold_start")
            )
            rec["anomaly_verified"] = bool(
                second_intel.get("verified")
            )
            rec["anomaly_reference_source"] = second_intel.get(
                "reference_source"
            )

            if rec["asin"] in watch:
                watch[rec["asin"]].update({
                    "amazon_old_price":
                        rec.get("amazon_old_price"),
                    "amazon_old_price_verified":
                        rec.get("amazon_old_price_verified"),
                    "search_old_price":
                        rec.get("search_old_price"),
                    "anomaly_tier":
                        rec.get("anomaly_tier"),
                    "anomaly_reason":
                        rec.get("anomaly_reason"),
                    "anomaly_confidence":
                        rec.get("anomaly_confidence"),
                    "anomaly_reference":
                        rec.get("anomaly_reference"),
                    "anomaly_drop":
                        rec.get("anomaly_drop"),
                    "anomaly_saving":
                        rec.get("anomaly_saving"),
                    "anomaly_cold_start":
                        rec.get("anomaly_cold_start"),
                    "anomaly_verified":
                        rec.get("anomaly_verified"),
                    "anomaly_reference_source":
                        rec.get("anomaly_reference_source"),
                })

                async with lock:
                    save_files()

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

    promo_scope = str(
        deal.get("promo_scope", "") or ""
    ).lower()

    account_specific = bool(
        deal.get("account_specific", False)
    )

    promo_lines = []

    promo_saving = 0.0
    promo_final_price = live

    public_percent = (
        promo_type == "percent"
        and promo_percent > 0
        and bool(deal.get("promo_verified", False))
        and not conditional
        and not member_only
        and not account_specific
        and promo_scope in ("", "universal")
    )

    if public_percent:
        promo_saving = live * promo_percent / 100.0
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
        if promo_percent > 0:
            promo_lines.append(
                f"🎟️ كوبون: خصم {promo_percent:.1f}%"
            )
        elif coupon_value > 0:
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

    # Keep restricted offers visible but completely separate from the public
    # effective price. Only show a compact summary to the reviewer.
    for offer in (deal.get("bank_offers", []) or [])[:2]:
        pct = to_float(offer.get("promo_percent"))
        text = str(offer.get("promo_text", "") or "")[:180]
        promo_lines.append(
            f"💳 عرض بنك منفصل: {pct:.1f}%" + (f" — {text}" if text else "")
        )

    for offer in (deal.get("member_offers", []) or [])[:1]:
        pct = to_float(offer.get("promo_percent"))
        promo_lines.append(
            f"👑 عرض عضوية منفصل: {pct:.1f}%"
        )

    for offer in (deal.get("account_offers", []) or [])[:1]:
        pct = to_float(offer.get("promo_percent"))
        promo_lines.append(
            f"👤 عرض لحسابات مؤهلة فقط: {pct:.1f}%"
        )

    for offer in (deal.get("bulk_offers", []) or [])[:3]:
        pct = to_float(offer.get("promo_percent"))
        qty = int(to_float(offer.get("minimum_quantity")) or 0)
        if pct > 0 and qty > 0:
            promo_lines.append(
                f"📦 عرض كمية إضافي: اشترِ {qty}+ ووفر {pct:.1f}%"
            )
        elif pct > 0:
            promo_lines.append(f"📦 عرض كمية إضافي: وفر {pct:.1f}%")

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


    # AMAZON PROMO STACK V2
    promo_stack = compute_promo_stack(live, deal)

    stack_final = to_float(
        promo_stack.get("authoritative_final_price")
    )

    if (
        stack_final > 0
        and stack_final < promo_final_price
    ):
        promo_final_price = stack_final
        promo_saving = max(
            promo_saving,
            live - promo_final_price,
        )

    if promo_stack.get("summary"):
        promo_details = (
            (str(promo_details) + " | ")
            if promo_details
            else ""
        ) + promo_stack["summary"]

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
        "promo_stack": promo_stack.get("components", []),
        "promo_stack_summary": promo_stack.get("summary", ""),
        "promo_stack_verified": bool(promo_stack.get("stack_verified")),
        "promo_stack_best_case_price": promo_stack.get("best_case_final_price", 0),
        "promo_stack_needs_checkout": bool(
            promo_stack.get("needs_checkout_confirmation")
        ),
        "saving": round(saving, 2),
        "verified": True,
        "verification_reason": reason,
        "comparison_report": reason,
        "reason": reason,
        "priority": tier.lower(),
        "live_rechecked": True,
        "asin": rec["asin"],
        "deal_engine": "V5.1",
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
        "promo_scope": promo_scope,
        "account_specific": account_specific,
        "general_50_plus": bool(deal.get("general_50_plus", False)),
        "bank_offers": deal.get("bank_offers", []) or [],
        "member_offers": deal.get("member_offers", []) or [],
        "account_offers": deal.get("account_offers", []) or [],
        "public_offers": deal.get("public_offers", []) or [],
        "bulk_offers": deal.get("bulk_offers", []) or [],
        "promo_label": promo_lines[0] if promo_lines else "",
    }

    r = await _send_amazon_independent_review(payload)

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
            remaining = max(
                1,
                int(
                    AMAZON_CIRCUIT_UNTIL
                    - time.monotonic()
                )
            )

            print(
                "⏸️ ULTRA WAITING FOR AMAZON",
                "| CIRCUIT =",
                remaining,
                "sec",
                flush=True
            )

            await asyncio.sleep(
                min(15, remaining)
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
        competitor_db = os.getenv("COMPETITOR_DB_PATH", "").strip()
        if competitor_db:
            con = sqlite3.connect(
                f"file:{competitor_db}?mode=ro",
                uri=True
            )
            con.row_factory = sqlite3.Row
        else:
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
# AMAZON_PRIORITY_ZERO_MANUAL_V1
manual_processed = set()
manual_retry_after = {}

PRIORITY_ZERO_ASINS = {
    "B0DP8ZX1RX",
}



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

                priority_zero = asin in PRIORITY_ZERO_ASINS

                # Already discovered: no Amazon Search required.
                if asin in watch:
                    watch[asin]["manual_watch"] = True

                    if priority_zero:
                        watch[asin]["priority_zero"] = True
                        watch[asin]["priority_boost_until"] = 4102444800
                    else:
                        watch[asin]["priority_boost_until"] = int(time.time()) + 86400

                    async with lock:
                        save_files()

                    manual_processed.add(asin)

                    print(
                        "🚨 PRIORITY ZERO -> ULTRA HOT:"
                        if priority_zero
                        else "📌 MANUAL -> ULTRA HOT:",
                        asin,
                        flush=True
                    )
                    continue

                # AMAZON_MANUAL_DIRECT_V1
                # Unknown ASIN -> direct product page.
                # Does not depend on Amazon Search /s?k=.
                now_mono = time.monotonic()

                if now_mono < manual_retry_after.get(asin, 0):
                    continue

                # Conservative retry if the product page temporarily fails.
                manual_retry_after[asin] = now_mono + 180

                direct_url = asin_url(asin)

                async with httpx.AsyncClient() as client:
                    current = await live_price(
                        client,
                        direct_url
                    )

                current = to_float(current)

                if current <= 0:
                    print(
                        "📌 MANUAL DIRECT FAILED; RETRY 3m:",
                        asin,
                        flush=True
                    )
                    continue

                meta = AMAZON_PRODUCT_META_CACHE.get(
                    asin,
                    {}
                )

                exact = {
                    "asin": asin,
                    "url": direct_url,
                    "title": (
                        meta.get("title_ar")
                        or asin
                    ),
                    "current_price": current,
                    "old_price": meta.get("old_price"),
                    "image_url": meta.get("image_url", ""),
                }

                add_product(
                    exact,
                    "manual_direct"
                )

                if asin not in watch:
                    print(
                        "📌 MANUAL ADD FAILED; RETRY 15m:",
                        asin,
                        flush=True
                    )
                    continue

                watch[asin]["manual_watch"] = True

                if priority_zero:
                    watch[asin]["priority_zero"] = True
                    watch[asin]["priority_boost_until"] = 4102444800
                else:
                    watch[asin]["priority_boost_until"] = int(time.time()) + 86400

                async with lock:
                    save_files()

                manual_processed.add(asin)
                manual_retry_after.pop(asin, None)

                print(
                    "🚨 PRIORITY ZERO NEW -> ULTRA HOT:"
                    if priority_zero
                    else "📌 MANUAL NEW -> ULTRA HOT:",
                    asin,
                    flush=True
                )

        except Exception as e:
            print(
                "MANUAL WATCH ERROR:",
                repr(e),
                flush=True
            )

        await asyncio.sleep(20)



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
    # GLOBAL_AMAZON_DEEP_V95

    async with lock:
        items = [
            dict(x)
            for x in watch.values()
            if x.get("asin")
            and x.get("url")
        ]

    if not items:
        return

    def check_age(rec):
        return max(
            int(
                rec.get(
                    "v5_full_checked",
                    0
                ) or 0
            ),
            int(
                rec.get(
                    "last_checked",
                    0
                ) or 0
            ),
            int(
                rec.get(
                    "ultra_checked",
                    0
                ) or 0
            ),
        )

    def deep_priority(rec):
        priority = int(
            rec.get(
                "global_deep_v95_priority",
                0
            ) or 0
        )

        if rec.get("priority_zero"):
            priority = max(
                priority,
                1000
            )

        if int(
            rec.get(
                "anomaly_priority",
                0
            ) or 0
        ) >= 90:
            priority = max(
                priority,
                900
            )

        if rec.get(
            "crazy_price_candidate"
        ):
            priority = max(
                priority,
                950
            )

        # New products from every direct Amazon section
        # go before old cold products.
        if (
            rec.get("direct_surface")
            and check_age(rec) == 0
        ):
            priority = max(
                priority,
                50
            )

        return priority

    items.sort(
        key=lambda rec: (
            -deep_priority(rec),
            check_age(rec),
            -int(
                rec.get(
                    "last_seen",
                    0
                ) or 0
            ),
        )
    )

    # Faster only while Amazon is healthy.
    batch_size = (
        12
        if AMAZON_SUCCESS_STREAK >= 8
        else 8
    )

    batch = items[:batch_size]

    priority_in_batch = sum(
        1
        for rec in batch
        if deep_priority(rec) > 0
    )

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

                sent, verified_price = (
                    await maybe_send_v5_review(
                        client,
                        rec,
                        current
                    )
                )

                final_price = (
                    to_float(
                        verified_price
                    )
                    or to_float(
                        current
                    )
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

            rec = watch[asin]

            rec[
                "v5_full_checked"
            ] = now

            rec[
                "global_deep_v95_checked_at"
            ] = now

            rec[
                "global_deep_v95_priority"
            ] = 0

            price = to_float(
                result.get("price")
            )

            if price <= 0:
                continue

            live_count += 1

            if result.get("sent"):
                sent_count += 1

            rec["last_price"] = price

            record_price_sample(
                rec,
                price
            )

            if price > to_float(
                rec.get(
                    "max_seen_price"
                )
            ):
                rec[
                    "max_seen_price"
                ] = price

            old_min = to_float(
                rec.get(
                    "min_seen_price"
                )
            )

            if (
                old_min <= 0
                or price < old_min
            ):
                rec[
                    "min_seen_price"
                ] = price

        save_files()

    print(
        "🌍 V5 GLOBAL DEEP SWEEP",
        datetime.now().strftime(
            "%H:%M:%S"
        ),
        "| CHECKED =",
        len(batch),
        "| PRIORITY =",
        priority_in_batch,
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
# AMAZON_DIRECT_SURFACES_V1
# Direct Deals lane.
# - separate from broad Search backoff
# - shares global Amazon request governor
# - own 403/429/503/CAPTCHA backoff
# - no auto publish
# =========================================================

AMAZON_DIRECT_SURFACES = [
    # Highest priority deal signals
    ("limited_time", AMAZON + "/s?k=" + quote_plus("limited time deals")),
    ("coupons", AMAZON + "/s?k=" + quote_plus("coupon deals")),
    ("today_deals", AMAZON + "/s?k=" + quote_plus("todays deals")),

    # Used / Open Box / Renewed
    ("used_like_new", AMAZON + "/s?k=" + quote_plus("used like new")),
    ("used_very_good", AMAZON + "/s?k=" + quote_plus("used very good")),
    ("used_good", AMAZON + "/s?k=" + quote_plus("used good")),
    ("open_box", AMAZON + "/s?k=" + quote_plus("open box")),
    ("renewed", AMAZON + "/s?k=" + quote_plus("renewed")),

    # Electronics / Tech
    ("electronics", AMAZON + "/s?k=" + quote_plus("electronics deals")),
    ("mobiles", AMAZON + "/s?k=" + quote_plus("mobile phones deals")),
    ("computers", AMAZON + "/s?k=" + quote_plus("computers laptops deals")),
    ("gaming", AMAZON + "/s?k=" + quote_plus("video games deals")),

    # Home
    ("appliances", AMAZON + "/s?k=" + quote_plus("home appliances deals")),
    ("home_kitchen", AMAZON + "/s?k=" + quote_plus("home kitchen deals")),
    ("furniture", AMAZON + "/s?k=" + quote_plus("furniture deals")),
    ("garden", AMAZON + "/s?k=" + quote_plus("garden outdoor deals")),

    # Personal / Family
    ("beauty", AMAZON + "/s?k=" + quote_plus("beauty deals")),
    ("health", AMAZON + "/s?k=" + quote_plus("health household deals")),
    ("baby", AMAZON + "/s?k=" + quote_plus("baby deals")),

    # Fashion
    ("fashion", AMAZON + "/s?k=" + quote_plus("fashion deals")),
    ("mens_fashion", AMAZON + "/s?k=" + quote_plus("mens fashion deals")),
    ("womens_fashion", AMAZON + "/s?k=" + quote_plus("womens fashion deals")),
    ("shoes", AMAZON + "/s?k=" + quote_plus("shoes deals")),

    # Grocery
    ("grocery", AMAZON + "/s?k=" + quote_plus("grocery deals")),
    ("food", AMAZON + "/s?k=" + quote_plus("food beverages deals")),

    # Sports / Toys
    ("sports", AMAZON + "/s?k=" + quote_plus("sports fitness deals")),
    ("toys", AMAZON + "/s?k=" + quote_plus("toys games deals")),

    # Automotive / Tools
    ("automotive", AMAZON + "/s?k=" + quote_plus("automotive deals")),
    ("car_accessories", AMAZON + "/s?k=" + quote_plus("car accessories deals")),
    ("tools", AMAZON + "/s?k=" + quote_plus("tools home improvement deals")),

    # Office / Books
    ("office", AMAZON + "/s?k=" + quote_plus("office products deals")),
    ("books", AMAZON + "/s?k=" + quote_plus("books deals")),
    ("stationery", AMAZON + "/s?k=" + quote_plus("stationery deals")),

    # Other departments
    ("pets", AMAZON + "/s?k=" + quote_plus("pet supplies deals")),
    ("industrial", AMAZON + "/s?k=" + quote_plus("industrial supplies deals")),
    ("music", AMAZON + "/s?k=" + quote_plus("musical instruments deals")),
    ("software", AMAZON + "/s?k=" + quote_plus("software deals")),
]

# Whole-department progressive sweep. These are queryless department pages,
# so a product can be discovered even when its title does not match one of
# our deal keywords. This is especially important for Grocery/Household.
AMAZON_DEPARTMENT_INDEXES = [
    ("electronics", "electronics"),
    ("computers", "computers"),
    ("mobiles", "mobile"),
    ("video_games", "videogames"),
    ("home", "home"),
    ("kitchen", "kitchen"),
    ("appliances", "appliances"),
    ("grocery", "grocery"),
    ("health_household", "hpc"),
    ("beauty", "beauty"),
    ("baby", "baby"),
    ("fashion", "fashion"),
    ("shoes", "shoes"),
    ("sports", "sports"),
    ("toys", "toys"),
    ("automotive", "automotive"),
    ("tools", "tools"),
    ("office", "office-products"),
    ("books", "stripbooks"),
    ("pets", "pets"),
    ("industrial", "industrial"),
    ("music", "mi"),
    ("software", "software"),
]

for _dept_name, _dept_index in AMAZON_DEPARTMENT_INDEXES:
    AMAZON_DIRECT_SURFACES.append((
        "dept_" + _dept_name,
        AMAZON + "/s?i=" + quote_plus(_dept_index) + "&s=featured-rank",
    ))

# AMAZON_FIRST_V92
# Fast lane: Coupons + Grocery/Market + Household + Home + Family.
# Broad lane: all remaining Amazon departments progressively.
AMAZON_PRIORITY_SURFACE_NAMES = {
    "limited_time", "coupons", "today_deals",
    "grocery", "food", "dept_grocery",
    "health", "dept_health_household",
    "home_kitchen", "dept_home", "dept_kitchen",
    "baby", "dept_baby", "beauty", "dept_beauty",
}

AMAZON_PRIORITY_SURFACES = [
    x for x in AMAZON_DIRECT_SURFACES
    if x[0] in AMAZON_PRIORITY_SURFACE_NAMES
]

# AMAZON_FIRST_PRIORITY_ORDER_V92
_PRIORITY_ORDER = {
    "coupons": 0,
    "grocery": 1,
    "dept_grocery": 2,
    "food": 3,
    "limited_time": 4,
    "today_deals": 5,
    "health": 6,
    "dept_health_household": 7,
    "home_kitchen": 8,
    "dept_kitchen": 9,
    "dept_home": 10,
    "baby": 11,
    "dept_baby": 12,
    "beauty": 13,
    "dept_beauty": 14,
}

AMAZON_PRIORITY_SURFACES.sort(
    key=lambda x: _PRIORITY_ORDER.get(x[0], 999)
)

AMAZON_GENERAL_SURFACES = [
    x for x in AMAZON_DIRECT_SURFACES
    if (
        (
            x[0].startswith("dept_")
            and x[0] not in AMAZON_PRIORITY_SURFACE_NAMES
        )
        or x[0] in {
            "used_like_new",
            "used_very_good",
            "used_good",
            "open_box",
            "renewed",
        }
    )
]

AMAZON_SURFACE_BACKOFF_UNTIL = 0.0
AMAZON_SURFACE_FAIL_STREAK = 0


def _surface_price(text):
    try:
        m = re.search(
            r"(\d[\d,]*(?:\.\d+)?)",
            str(text or "").replace("\u066c", ",")
        )
        if not m:
            return 0.0
        return float(m.group(1).replace(",", ""))
    except Exception:
        return 0.0


def parse_amazon_direct_surface(html):
    # First use our proven Amazon search parser where possible.
    found = {}

    try:
        for item in parse_search(html):
            asin = str(item.get("asin") or "").upper()
            if re.fullmatch(r"[A-Z0-9]{10}", asin):
                found[asin] = item
    except Exception:
        pass

    soup = BeautifulSoup(html, "html.parser")

    # Direct deal pages do not always use normal s-result-item cards.
    for link in soup.select(
        'a[href*="/dp/"], a[href*="/gp/product/"]'
    ):
        href = str(link.get("href") or "")

        m = re.search(
            r"/(?:dp|gp/product)/([A-Z0-9]{10})",
            href,
            re.I
        )
        if not m:
            continue

        asin = m.group(1).upper()

        if asin in found:
            continue

        node = link
        card = None

        # Find nearest product container containing a visible price.
        for _ in range(7):
            node = getattr(node, "parent", None)
            if node is None:
                break

            try:
                if node.select_one(
                    ".a-price .a-offscreen, "
                    ".a-text-price .a-offscreen"
                ):
                    card = node
                    break
            except Exception:
                break

        if card is None:
            continue

        prices = []

        try:
            for el in card.select(
                ".a-price .a-offscreen, "
                ".a-text-price .a-offscreen"
            ):
                value = _surface_price(
                    el.get_text(" ", strip=True)
                )
                if value > 0:
                    prices.append(value)
        except Exception:
            continue

        if not prices:
            continue

        current = min(prices)

        old = None
        higher = [
            x for x in prices
            if x > current * 1.01
        ]
        if higher:
            old = max(higher)

        title = ""

        try:
            title = link.get_text(" ", strip=True)
        except Exception:
            pass

        if len(title) < 3:
            try:
                img = link.select_one("img")
                if img:
                    title = str(
                        img.get("alt") or ""
                    ).strip()
            except Exception:
                pass

        if len(title) < 3:
            try:
                el = card.select_one(
                    "h2, h3, .a-size-medium, "
                    ".a-size-base-plus"
                )
                if el:
                    title = el.get_text(
                        " ",
                        strip=True
                    )
            except Exception:
                pass

        if not title:
            title = "Amazon Deal " + asin

        found[asin] = {
            "asin": asin,
            "title": title[:300],
            "url": asin_url(asin),
            "current_price": current,
            "old_price": old,
        }

        if len(found) >= 100:
            break

    return list(found.values())


async def fetch_amazon_direct_surface(client, name, url):
    global AMAZON_SURFACE_BACKOFF_UNTIL
    global AMAZON_SURFACE_FAIL_STREAK

    now = time.monotonic()

    if amazon_circuit_open():
        return ""

    if now < AMAZON_SURFACE_BACKOFF_UNTIL:
        return ""

    # IMPORTANT:
    # Same shared Amazon request gate used by the rest of Radar.
    await amazon_wait_for_slot()

    try:
        r = await client.get(
            url,
            headers={
                "User-Agent":
                    "EgyptDealsAmazonRadar/1.0",
                "Accept-Language":
                    "ar-EG,ar;q=0.9,en;q=0.8",
                "Accept":
                    "text/html,application/xhtml+xml",
            },
            timeout=18,
            follow_redirects=True,
        )

        body = r.text or ""
        low = body.lower()

        protected = (
            "captcha" in low
            or "robot check" in low
            or "enter the characters you see below" in low
        )

        if (
            r.status_code in (403, 429)
            or protected
        ):
            AMAZON_SURFACE_BACKOFF_UNTIL = (
                time.monotonic() + 900
            )

            print(
                "🛑 AMAZON SURFACE PROTECTION",
                name,
                "| HTTP =", r.status_code,
                "| PAUSE = 900 sec",
                flush=True
            )
            return ""

        if r.status_code == 503:
            AMAZON_SURFACE_FAIL_STREAK += 1

            delays = (120, 300, 600, 900)

            delay = delays[
                min(
                    AMAZON_SURFACE_FAIL_STREAK - 1,
                    len(delays) - 1
                )
            ]

            AMAZON_SURFACE_BACKOFF_UNTIL = (
                time.monotonic() + delay
            )

            print(
                "⏳ AMAZON SURFACE BACKOFF",
                name,
                "| STREAK =",
                AMAZON_SURFACE_FAIL_STREAK,
                "| PAUSE =",
                delay,
                "sec",
                flush=True
            )
            return ""

        if r.status_code != 200:
            print(
                "⚠️ AMAZON SURFACE HTTP",
                name,
                r.status_code,
                flush=True
            )
            return ""

        AMAZON_SURFACE_FAIL_STREAK = 0
        AMAZON_SURFACE_BACKOFF_UNTIL = 0.0

        return body

    except Exception as exc:
        AMAZON_SURFACE_FAIL_STREAK += 1
        AMAZON_SURFACE_BACKOFF_UNTIL = (
            time.monotonic() + 90
        )

        print(
            "⚠️ AMAZON SURFACE ERROR",
            name,
            repr(exc),
            flush=True
        )
        return ""





async def ingest_amazon_direct_surface(name, items):
    # UNIVERSAL_VALUE_ANOMALY_V96
    if not items:
        return 0, 0

    added = 0
    hot = []
    anomaly_fast = []

    global_priority_count = 0
    anomaly_count = 0
    candidate_count = 0
    cold_start_count = 0

    now = int(time.time())

    premium_surfaces = {
        "coupons",
        "limited_time",
        "today_deals",
        "grocery",
        "food",
        "dept_grocery",
        "home_kitchen",
        "dept_home",
        "dept_kitchen",
        "health",
        "dept_health_household",
    }

    tier_score = {
        "HOT": 80,
        "ULTRA": 95,
        "CRITICAL": 110,
    }

    for item in items:
        asin = str(
            item.get("asin")
            or ""
        ).upper()

        if not re.fullmatch(
            r"[A-Z0-9]{10}",
            asin
        ):
            continue

        existed = asin in watch

        add_product(
            item,
            "direct_surface:" + name
        )

        newly_added = (
            not existed
            and asin in watch
        )

        if newly_added:
            added += 1

        if asin not in watch:
            continue

        rec = watch[asin]

        current = to_float(
            item.get("current_price")
        )

        old = to_float(
            item.get("old_price")
        )

        drop = 0.0

        if old > current > 0:
            drop = (
                (old - current)
                / old
                * 100.0
            )

        rec[
            "direct_surface"
        ] = name

        rec[
            "direct_surface_seen_at"
        ] = now

        rec[
            "listing_drop_percent"
        ] = max(
            to_float(
                rec.get(
                    "listing_drop_percent"
                )
            ),
            drop,
        )

        # GLOBAL 5%+ POLICY:
        # Any visible Amazon listing discount >=5% from ANY department
        # gets priority exact product-page verification.
        if drop >= 5.0:
            rec["global_deep_v95_priority"] = max(
                int(rec.get("global_deep_v95_priority", 0) or 0),
                88,
            )
            rec["manual_watch"] = True
            rec["priority_boost_until"] = max(
                int(rec.get("priority_boost_until", 0) or 0),
                now + 7200,
            )

        # Used-condition priority remains unchanged.
        if name == "used_like_new":
            rec[
                "used_condition"
            ] = "Like New"

            rec[
                "used_priority"
            ] = 95

            rec[
                "priority_boost_until"
            ] = max(
                int(
                    rec.get(
                        "priority_boost_until",
                        0
                    ) or 0
                ),
                now + 14400,
            )

        elif name == "used_very_good":
            rec[
                "used_condition"
            ] = "Very Good"

            rec[
                "used_priority"
            ] = 85

            rec[
                "priority_boost_until"
            ] = max(
                int(
                    rec.get(
                        "priority_boost_until",
                        0
                    ) or 0
                ),
                now + 7200,
            )

        elif name == "used_good":
            rec[
                "used_condition"
            ] = "Good"

            rec[
                "used_priority"
            ] = 75

            rec[
                "priority_boost_until"
            ] = max(
                int(
                    rec.get(
                        "priority_boost_until",
                        0
                    ) or 0
                ),
                now + 3600,
            )

        # Every NEW Amazon product from EVERY department
        # gets progressive exact verification.
        if newly_added:
            base_priority = (
                80
                if name in premium_surfaces
                else 60
            )

            rec[
                "global_deep_v95_priority"
            ] = max(
                int(
                    rec.get(
                        "global_deep_v95_priority",
                        0
                    ) or 0
                ),
                base_priority,
            )

            rec[
                "global_deep_v95_source"
            ] = name

            rec[
                "global_deep_v95_discovered_at"
            ] = now

            global_priority_count += 1

        # Visible listing drop still remains useful.
        if drop >= 40:
            rec[
                "priority_boost_until"
            ] = max(
                int(
                    rec.get(
                        "priority_boost_until",
                        0
                    ) or 0
                ),
                now + 3600,
            )

            rec[
                "manual_watch"
            ] = True

            hot.append(
                (
                    drop,
                    asin,
                )
            )

        # --------------------------------------------------
        # UNIVERSAL DYNAMIC VALUE ANOMALY
        #
        # NO product-family list.
        # NO <=250 rule.
        # NO minimum EGP saving.
        # --------------------------------------------------

        verified_tier = str(
            rec.get(
                "anomaly_tier"
            )
            or ""
        ).upper()

        candidate_tier = str(
            rec.get(
                "value_candidate_tier"
            )
            or ""
        ).upper()

        verified_signal = (
            verified_tier
            in tier_score
            and bool(
                rec.get(
                    "anomaly_verified"
                )
            )
        )

        candidate_signal = (
            not verified_signal
            and candidate_tier
            in tier_score
        )

        if verified_signal:
            tier = verified_tier
            verified = True

            dyn_drop = to_float(
                rec.get(
                    "anomaly_drop"
                )
            )

            cold_start = bool(
                rec.get(
                    "anomaly_cold_start"
                )
            )

        elif candidate_signal:
            tier = candidate_tier
            verified = False

            dyn_drop = to_float(
                rec.get(
                    "value_candidate_drop"
                )
            )

            cold_start = bool(
                rec.get(
                    "value_candidate_cold_start"
                )
            )

        else:
            tier = ""
            verified = False
            dyn_drop = 0.0
            cold_start = False

        if tier:

            rec[
                "manual_watch"
            ] = True

            rec[
                "priority_boost_until"
            ] = max(
                int(
                    rec.get(
                        "priority_boost_until",
                        0
                    ) or 0
                ),
                now + 10800,
            )

            if verified:
                # FINAL V9.6 anomaly only.

                rec[
                    "anomaly_priority"
                ] = max(
                    int(
                        rec.get(
                            "anomaly_priority",
                            0
                        ) or 0
                    ),
                    tier_score[tier],
                )

                rec[
                    "crazy_price_candidate"
                ] = True

                rec[
                    "crazy_price_reason"
                ] = (
                    "dynamic_value_anomaly:"
                    + str(
                        rec.get(
                            "anomaly_reason"
                        )
                        or ""
                    )
                )

                if tier == "CRITICAL":
                    rec[
                        "priority"
                    ] = (
                        "critical_price_anomaly"
                    )

                    rec[
                        "intelligence_tier"
                    ] = "critical"

                signal_score = (
                    tier_score[tier]
                )

                anomaly_count += 1

            else:
                # Candidate only.
                # It receives fast exact verification
                # but is NOT a final anomaly.

                rec[
                    "value_candidate_priority"
                ] = max(
                    int(
                        rec.get(
                            "value_candidate_priority",
                            0
                        ) or 0
                    ),
                    max(
                        50,
                        tier_score[tier] - 15
                    ),
                )

                # Never allow an unverified candidate
                # to inherit a final crazy-price flag.
                if str(
                    rec.get(
                        "crazy_price_reason"
                    )
                    or ""
                ).startswith(
                    "dynamic_value_anomaly:"
                ):
                    rec[
                        "crazy_price_candidate"
                    ] = False

                    rec.pop(
                        "crazy_price_reason",
                        None
                    )

                if (
                    rec.get("priority")
                    == "critical_price_anomaly"
                ):
                    rec.pop(
                        "priority",
                        None
                    )

                if (
                    rec.get(
                        "intelligence_tier"
                    )
                    == "critical"
                ):
                    rec.pop(
                        "intelligence_tier",
                        None
                    )

                signal_score = max(
                    50,
                    tier_score[tier] - 15
                )

                candidate_count += 1

            anomaly_fast.append(
                (
                    signal_score,
                    dyn_drop,
                    asin,
                    tier,
                    cold_start,
                    verified,
                )
            )

            if cold_start:
                cold_start_count += 1

        else:
            # Remove only compatibility flags that were
            # generated by the dynamic V9.6 anomaly path.
            if str(
                rec.get(
                    "crazy_price_reason"
                )
                or ""
            ).startswith(
                "dynamic_value_anomaly:"
            ):
                rec[
                    "crazy_price_candidate"
                ] = False

                rec.pop(
                    "crazy_price_reason",
                    None
                )

                if (
                    rec.get("priority")
                    == "critical_price_anomaly"
                ):
                    rec.pop(
                        "priority",
                        None
                    )

                if (
                    rec.get(
                        "intelligence_tier"
                    )
                    == "critical"
                ):
                    rec.pop(
                        "intelligence_tier",
                        None
                    )

    manual_path = str(
        ROOT
        / ".amazon_manual_watch.txt"
    )

    newly_queued = 0

    try:
        try:
            existing = set(
                re.findall(
                    r"\b[A-Z0-9]{10}\b",
                    Path(
                        manual_path
                    ).read_text(
                        errors="ignore"
                    ).upper()
                )
            )

        except Exception:
            existing = set()

        lines = []

        for (
            score,
            dyn_drop,
            asin,
            tier,
            cold_start,
            verified,
        ) in sorted(
            anomaly_fast,
            reverse=True
        )[:25]:

            if asin in existing:
                continue

            lines.append(asin)
            existing.add(asin)
            newly_queued += 1

            print(
                "🚨 VALUE ANOMALY FAST",
                asin,
                "| TIER =",
                tier,
                "| DROP =",
                round(dyn_drop, 1),
                "| COLD START =",
                cold_start,
                "| VERIFIED =",
                verified,
                "| SOURCE =",
                name,
                "| EXACT VERIFY = NOW",
                flush=True
            )

        for drop, asin in sorted(
            hot,
            reverse=True
        )[:15]:

            if asin in existing:
                continue

            lines.append(asin)
            existing.add(asin)

        if lines:
            with open(
                manual_path,
                "a",
                encoding="utf-8"
            ) as f:

                for asin in lines:
                    f.write(
                        asin + "\n"
                    )

    except Exception as exc:
        print(
            "⚠️ VALUE ANOMALY QUEUE ERROR",
            repr(exc),
            flush=True
        )

    async with lock:
        save_files()

    if (
        global_priority_count
        or anomaly_count
        or candidate_count
    ):
        print(
            "🌐 AMAZON UNIVERSAL V9.6",
            name,
            "| NEW =",
            global_priority_count,
            "| VALUE ANOMALIES =",
            anomaly_count,
            "| VERIFY CANDIDATES =",
            candidate_count,
            "| COLD START =",
            cold_start_count,
            "| FAST QUEUED =",
            newly_queued,
            "| ALL PRODUCTS = ON",
            flush=True
        )

    return added, len(hot)






# AMAZON_DIRECT_PAGINATION_V1
async def direct_surface_once(lane="general"):
    if lane == "priority":
        surfaces = AMAZON_PRIORITY_SURFACES
        index_key = "priority_surface_index"
    else:
        surfaces = AMAZON_GENERAL_SURFACES
        index_key = "direct_surface_index"

    if not surfaces:
        return

    try:
        idx = int(state.get(index_key, 0))
    except Exception:
        idx = 0

    idx %= len(surfaces)

    name, base_url = surfaces[idx]

    state[index_key] = (
        idx + 1
    ) % len(surfaces)

    page_key = "direct_page_" + name

    try:
        page = int(state.get(page_key, 1))
    except Exception:
        page = 1

    max_page = 20 if str(name).startswith("dept_") else 5
    page = max(1, min(page, max_page))

    if "/s?" in base_url:
        sep = "&" if "?" in base_url else "?"
        url = base_url + sep + "page=" + str(page)
    else:
        url = base_url

    async with httpx.AsyncClient() as client:
        html = await fetch_amazon_direct_surface(
            client,
            name,
            url
        )

    if not html:
        return

    items = parse_amazon_direct_surface(html)

    # Empty page -> reset this source back to page 1.
    if not items:
        state[page_key] = 1

        async with lock:
            save_files()

        print(
            "⚡ AMAZON DIRECT SURFACE",
            name,
            "| PAGE =", page,
            "| FOUND = 0",
            "| RESET -> PAGE 1",
            flush=True
        )
        return

    added, hot = await ingest_amazon_direct_surface(
        name,
        items
    )

    # Deal surfaces cycle through 5 pages; whole departments go deeper.
    state[page_key] = 1 if page >= max_page else page + 1

    async with lock:
        save_files()

    print(
        "⚡ AMAZON DIRECT SURFACE",
        name,
        "| PAGE =", page,
        "| FOUND =", len(items),
        "| NEW =", added,
        "| 40%+ HOT =", hot,
        "| NEXT PAGE =", state[page_key],
        "| WATCHLIST =", len(watch),
        flush=True
    )


async def priority_surface_loop():
    # Amazon #1 lane.
    await asyncio.sleep(18)

    while True:
        try:
            await direct_surface_once("priority")
        except Exception as exc:
            print(
                "⚠️ PRIORITY SURFACE LOOP ERROR",
                repr(exc),
                flush=True
            )

        # Faster only while Amazon is healthy.
        await asyncio.sleep(
            35 if AMAZON_SUCCESS_STREAK >= 8 else 55
        )


async def direct_surface_loop():
    # Every other Amazon department keeps progressing separately.
    await asyncio.sleep(30)

    while True:
        try:
            await direct_surface_once("general")
        except Exception as exc:
            print(
                "⚠️ DIRECT SURFACE LOOP ERROR",
                repr(exc),
                flush=True
            )

        await asyncio.sleep(
            75 if AMAZON_SUCCESS_STREAK >= 8 else 110
        )



async def main():
    print(
        "🚀 AMAZON MULTI-SOURCE RADAR STARTED",
        flush=True
    )

    await asyncio.gather(
        priority_surface_loop(),
        direct_surface_loop(),
        discovery_loop(),
        hot_loop(),
        ultra_hot_loop(),
        competitor_loop(),
        external_sources_loop(),
        extra_store_trigger_loop(),
        manual_watch_loop(),
        unified_queue_loop(),
        full_v5_watchlist_loop()
    )


if __name__ == "__main__":
    asyncio.run(main())
