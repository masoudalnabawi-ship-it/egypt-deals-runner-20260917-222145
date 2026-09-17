from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from urllib.request import Request, urlopen

CACHE_DIR = Path(os.getenv("MULTISTORE_REVIEW_MEDIA_DIR") or (Path.home() / "amazon_dynamic_runtime_v8" / "review_media_multistore"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

VIEWPORT_W = 1360
VIEWPORT_H = 950
MAX_CAPTURE_H = 820

STORE_SELECTORS = {
    "jumia": ["main", "section.-pvs", "div.row.-paxs"],
    "noon": ["main", "[data-qa='product-detail']", "[class*='productDetail']"],
    "noon_minutes": ["main", "[data-qa='product-detail']", "[class*='productDetail']"],
    "2b": ["main", ".product-info-main", ".product.media"],
    "twob": ["main", ".product-info-main", ".product.media"],
    "btech": ["main", "[class*='product-details']", "[class*='productDetails']"],
    "raya": ["main", ".product-info-main", "[class*='product-detail']"],
    "dream2000": ["main", "[class*='product']"],
    "carrefour": ["main", "[data-testid*='product']", "[class*='product']"],
    "raneen": ["main", ".product-info-main", "[class*='product']"],
    "kenzz": ["main", "[class*='product']"],
}

BLOCK_TEXT = (
    "access denied", "robot check", "captcha", "verify you are human",
    "unusual traffic", "تم رفض الوصول", "تحقق من أنك إنسان",
)


def _safe_key(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]", "", str(value or ""))[:80]
    return value or f"deal_{int(time.time())}"


def _download_image(url: str, out: Path) -> bool:
    if not str(url or "").startswith("http"):
        return False
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=20) as r:
            data = r.read(8 * 1024 * 1024)
        if len(data) < 1000:
            return False
        out.write_bytes(data)
        return True
    except Exception:
        return False


def _fallback_product_card(image_url: str, out: Path) -> str:
    raw = out.with_suffix(".rawimg")
    if not _download_image(image_url, raw):
        return ""
    try:
        from PIL import Image
        img = Image.open(raw).convert("RGB")
        canvas = Image.new("RGB", (VIEWPORT_W, MAX_CAPTURE_H), "white")
        img.thumbnail((VIEWPORT_W - 100, MAX_CAPTURE_H - 80))
        x = (VIEWPORT_W - img.width) // 2
        y = (MAX_CAPTURE_H - img.height) // 2
        canvas.paste(img, (x, y))
        canvas.save(out, "JPEG", quality=90, optimize=True)
        return str(out)
    except Exception:
        return ""
    finally:
        try:
            raw.unlink(missing_ok=True)
        except Exception:
            pass


def capture(store: str, url: str, key: str, image_url: str = "") -> dict:
    store = str(store or "").lower().strip()
    safe = _safe_key(key)
    out = CACHE_DIR / f"{store or 'store'}_{safe}.jpg"

    # Reuse a fresh review image. This also guarantees that review and
    # publication can use the exact same file.
    if out.exists() and time.time() - out.stat().st_mtime < 6 * 3600:
        return {"ok": True, "cached": True, "screenshot": str(out), "source": "cache"}

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-background-networking"],
            )
            context = browser.new_context(
                viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
                locale="ar-EG",
                user_agent=(
                    "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/153.0 Safari/537.36"
                ),
                extra_http_headers={"Accept-Language": "ar-EG,ar;q=0.9,en-US;q=0.7,en;q=0.5"},
            )
            page = context.new_page()
            response = page.goto(url, wait_until="domcontentloaded", timeout=35000)
            status = response.status if response else None
            page.wait_for_timeout(1200)

            try:
                body = page.locator("body").inner_text(timeout=4000).lower()
            except Exception:
                body = ""

            if status in (403, 429, 503) or any(x in body for x in BLOCK_TEXT):
                raise RuntimeError(f"blocked status={status}")

            # Remove fixed clutter without touching the actual product content.
            try:
                page.evaluate("""
                    () => {
                      const sels = [
                        '#onetrust-banner-sdk','.cookie-banner','.cookies-banner',
                        '[class*="cookie"]','[id*="cookie"]','[class*="newsletter"]',
                        '[class*="modal-backdrop"]'
                      ];
                      for (const s of sels) {
                        document.querySelectorAll(s).forEach(el => {
                          if (el && el.getBoundingClientRect().height < 450) el.style.display='none';
                        });
                      }
                    }
                """)
            except Exception:
                pass

            selectors = STORE_SELECTORS.get(store, []) + ["main", "[class*='product-detail']", "[class*='productDetail']"]
            box = None
            for sel in selectors:
                try:
                    loc = page.locator(sel)
                    if not loc.count():
                        continue
                    b = loc.first.bounding_box()
                    if b and b.get("width", 0) > 320 and b.get("height", 0) > 220:
                        box = b
                        break
                except Exception:
                    pass

            if box:
                x = max(0, min(float(box["x"]), VIEWPORT_W - 100))
                y = max(0, float(box["y"]) - 8)
                width = min(VIEWPORT_W - x, max(700, float(box["width"])))
                height = min(MAX_CAPTURE_H, max(420, float(box["height"])))
                page.screenshot(
                    path=str(out), type="jpeg", quality=88,
                    clip={"x": x, "y": y, "width": width, "height": height},
                )
            else:
                page.screenshot(path=str(out), type="jpeg", quality=88, full_page=False)

            context.close()
            browser.close()

        if out.exists() and out.stat().st_size > 5000:
            return {"ok": True, "cached": False, "screenshot": str(out), "source": "store_page"}
    except Exception as exc:
        fallback = _fallback_product_card(image_url, out)
        if fallback:
            return {"ok": True, "cached": False, "screenshot": fallback, "source": "product_image_fallback", "capture_error": str(exc)[:200]}
        return {"ok": False, "reason": str(exc)[:300]}

    return {"ok": False, "reason": "capture failed"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--image-url", default="")
    args = ap.parse_args()
    print(json.dumps(capture(args.store, args.url, args.key, args.image_url), ensure_ascii=False))


if __name__ == "__main__":
    main()
