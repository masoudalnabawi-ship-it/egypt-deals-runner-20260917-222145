import re
import os
import time
import threading
import tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright
from PIL import Image, ImageDraw, ImageFont
import arabic_reshaper
from bidi.algorithm import get_display

CACHE = Path(os.getenv("AMAZON_REVIEW_MEDIA_DIR") or (Path(__file__).resolve().parent.parent / "review_media"))
CACHE.mkdir(parents=True, exist_ok=True)

_LOCK = threading.Lock()
_PW = None
_BROWSER = None
_CONTEXT = None
_LAST_REQUEST = 0.0

MIN_GAP = 6.0
CACHE_SECONDS = 90


def _browser():
    global _PW, _BROWSER, _CONTEXT

    if _BROWSER is not None and _BROWSER.is_connected():
        return _CONTEXT

    _PW = sync_playwright().start()

    _BROWSER = _PW.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-background-networking",
        ],
    )

    _CONTEXT = _BROWSER.new_context(
        viewport={"width": 1360, "height": 950},
        locale="ar-EG",
        user_agent=(
            "Mozilla/5.0 (X11; Linux aarch64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/153.0 Safari/537.36"
        ),
        extra_http_headers={
            "Accept-Language":
                "ar-EG,ar;q=0.9,en-US;q=0.7,en;q=0.5"
        },
    )

    return _CONTEXT


def _number(text):
    if not text:
        return 0.0

    trans = str.maketrans(
        "٠١٢٣٤٥٦٧٨٩٫٬",
        "0123456789.,"
    )

    t = str(text).translate(trans)

    matches = re.findall(
        r"\d[\d,]*\.?\d*",
        t
    )

    for raw in matches:
        try:
            v = float(raw.replace(",", ""))
            if v > 0:
                return v
        except Exception:
            pass

    return 0.0



def _brand_screenshot(path):
    # CLEAN MODE: no channel name, no footer, no watermark.
    return path


def capture_amazon_page(url, key="product"):
    global _LAST_REQUEST

    safe = re.sub(
        r"[^A-Za-z0-9_-]",
        "",
        str(key)
    )[:80] or "product"

    out = CACHE / f"{safe}.jpg"

    # Fast cache: don't hit Amazon again for same item.
    if out.exists():
        age = time.time() - out.stat().st_mtime

        if age < CACHE_SECONDS:
            return {
                "ok": True,
                "cached": True,
                "screenshot": str(out),
                "live_price": 0.0,
                "availability": None,
                "page_price_text": "",
                "status": None,
            }

    with _LOCK:
        gap = time.time() - _LAST_REQUEST

        if gap < MIN_GAP:
            time.sleep(MIN_GAP - gap)

        ctx = _browser()
        page = ctx.new_page()

        try:
            response = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=35000
            )

            _LAST_REQUEST = time.time()

            status = (
                response.status
                if response
                else None
            )

            if status in (403, 429, 503):
                return {
                    "ok": False,
                    "blocked": True,
                    "status": status,
                    "reason":
                        f"Amazon HTTP {status}",
                }

            try:
                page.wait_for_selector(
                    "#ppd, #dp-container",
                    timeout=2500
                )
            except Exception:
                pass

            # Short wait only for price/images to settle.
            page.wait_for_timeout(700)

            try:
                body = page.locator(
                    "body"
                ).inner_text(
                    timeout=4000
                )
            except Exception:
                body = ""

            low = body.lower()

            block_terms = (
                "robot check",
                "enter the characters you see below",
                "captcha",
                "أدخل الأحرف التي تراها",
            )

            if any(
                x.lower() in low
                for x in block_terms
            ):
                return {
                    "ok": False,
                    "blocked": True,
                    "status": status,
                    "reason":
                        "Amazon Robot Check/CAPTCHA",
                }

            # Cookies
            for sel in (
                "#sp-cc-accept",
                "input#sp-cc-accept",
            ):
                try:
                    loc = page.locator(sel)
                    if (
                        loc.count()
                        and loc.first.is_visible()
                    ):
                        loc.first.click(
                            timeout=700
                        )
                        break
                except Exception:
                    pass

            price_text = ""

            selectors = (
                ".priceToPay .a-offscreen",
                "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
                "#corePrice_feature_div .a-price .a-offscreen",
                "#apex_desktop .a-price .a-offscreen",
                ".a-price .a-offscreen",
            )

            for sel in selectors:
                try:
                    loc = page.locator(sel)

                    if loc.count():
                        txt = loc.first.inner_text(
                            timeout=1200
                        ).strip()

                        if txt:
                            price_text = txt
                            break
                except Exception:
                    pass

            live_price = _number(price_text)

            unavailable_terms = (
                "currently unavailable",
                "temporarily out of stock",
                "غير متوفر حالياً",
                "غير متوفر حاليا",
                "غير متاح حالياً",
                "غير متاح حاليا",
            )

            if any(
                x in low
                for x in unavailable_terms
            ):
                availability = False
            else:
                availability = None

                for sel in (
                    "#add-to-cart-button",
                    "#buy-now-button",
                ):
                    try:
                        loc = page.locator(sel)

                        if (
                            loc.count()
                            and loc.first.is_visible()
                        ):
                            availability = True
                            break
                    except Exception:
                        pass

            # ==========================================
            # CLEAN AMAZON PRODUCT CROP V2
            # Product + price + Buy Box only.
            # No Amazon header/navigation.
            # ==========================================

            boxes = []

            for sel in (
                "#leftCol",
                "#centerCol",
                "#rightCol",
            ):
                try:
                    loc = page.locator(sel)

                    if loc.count():
                        box = loc.first.bounding_box()

                        if box and box["width"] > 20:
                            boxes.append(box)

                except Exception:
                    pass

            if boxes:
                x1 = max(
                    0,
                    min(b["x"] for b in boxes) - 12
                )

                y1 = max(
                    0,
                    min(b["y"] for b in boxes) - 12
                )

                x2 = min(
                    1360,
                    max(
                        b["x"] + b["width"]
                        for b in boxes
                    ) + 12
                )

                # Enough height to show image, price,
                # coupon/bank offer and Buy Box.
                natural_y2 = max(
                    b["y"] + b["height"]
                    for b in boxes
                ) + 12

                y2 = min(
                    natural_y2,
                    y1 + 820
                )

                clip = {
                    "x": x1,
                    "y": y1,
                    "width": max(100, x2-x1),
                    "height": max(100, y2-y1),
                }

                page.screenshot(
                    path=str(out),
                    type="jpeg",
                    quality=88,
                    clip=clip,
                )

            else:
                # Safe fallback: Amazon product container,
                # still below the site header.
                try:
                    product = page.locator(
                        "#ppd, #dp-container"
                    ).first

                    product.screenshot(
                        path=str(out),
                        type="jpeg",
                        quality=88,
                    )

                except Exception:
                    page.screenshot(
                        path=str(out),
                        type="jpeg",
                        quality=88,
                        full_page=False,
                    )

            # Add channel identity OUTSIDE product content.
            _brand_screenshot(str(out))

            return {
                "ok": True,
                "cached": False,
                "screenshot": str(out),
                "live_price": live_price,
                "availability": availability,
                "page_price_text": price_text,
                "status": status,
            }

        except Exception as e:
            return {
                "ok": False,
                "blocked": False,
                "reason": str(e)[:300],
            }

        finally:
            try:
                page.close()
            except Exception:
                pass
