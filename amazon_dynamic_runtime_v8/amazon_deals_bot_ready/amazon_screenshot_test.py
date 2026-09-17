import json
import os
import re
import tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright

from visual_card import send_photo_file

ROOT = Path(__file__).resolve().parent
RADAR_ROOT = Path(os.environ.get("AMAZON_RADAR_ROOT", str(ROOT.parent / "telegram_deals_bot_v1_ready")))
WATCH = RADAR_ROOT / ".amazon_radar_watch.json"
CFG = Path(os.environ.get("AMAZON_REVIEW_CONFIG", str(ROOT / "config.json")))
OUT = str(Path(tempfile.gettempdir()) / "amazon_real_page_test.jpg")

watch = json.loads(WATCH.read_text(errors="ignore"))
cfg   = json.loads(CFG.read_text(errors="ignore"))

# اختر منتجًا حقيقيًا معروفًا للرادار
rows = []
for r in watch.values():
    if not isinstance(r, dict):
        continue

    url = str(r.get("url") or "")
    price = float(r.get("last_price") or r.get("current_price") or 0)

    if "/dp/" in url and price > 0:
        rows.append(r)

if not rows:
    raise SystemExit("❌ لا يوجد منتج Amazon حقيقي جاهز")

# نفضل منتج عنده صورة وعنوان
rows.sort(
    key=lambda r: (
        bool(r.get("image_url")),
        len(str(r.get("title") or "")),
        float(r.get("last_price") or r.get("current_price") or 0),
    ),
    reverse=True
)

p = rows[0]
url = str(p["url"])
title = str(p.get("title") or "Amazon product")
radar_price = float(p.get("last_price") or p.get("current_price") or 0)

print("PRODUCT =", title[:100])
print("RADAR PRICE =", radar_price)
print("URL =", url)

with sync_playwright() as pw:
    browser = pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )

    context = browser.new_context(
        viewport={"width": 1440, "height": 1200},
        locale="ar-EG",
        user_agent=(
            "Mozilla/5.0 (X11; Linux aarch64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/153.0 Safari/537.36"
        ),
        extra_http_headers={
            "Accept-Language": "ar-EG,ar;q=0.9,en;q=0.7"
        },
    )

    page = context.new_page()

    resp = page.goto(
        url,
        wait_until="domcontentloaded",
        timeout=45000
    )

    print("HTTP =", resp.status if resp else "UNKNOWN")

    page.wait_for_timeout(2500)

    body = page.locator("body").inner_text(timeout=10000)

    blocked_terms = [
        "Robot Check",
        "Enter the characters you see below",
        "أدخل الأحرف التي تراها",
        "captcha",
    ]

    if any(x.lower() in body.lower() for x in blocked_terms):
        browser.close()
        raise SystemExit(
            "⚠️ Amazon عرض CAPTCHA/Robot Check — تم الإيقاف بدون محاولة تجاوزها"
        )

    # اغلق رسائل الكوكيز إن ظهرت
    for selector in [
        "#sp-cc-accept",
        "input#sp-cc-accept",
        "button:has-text('قبول')",
        "button:has-text('Accept')",
    ]:
        try:
            loc = page.locator(selector)
            if loc.count() and loc.first.is_visible():
                loc.first.click(timeout=1000)
                page.wait_for_timeout(500)
                break
        except Exception:
            pass

    # استخراج السعر الظاهر من الصفحة كمعلومة اختبار
    price_text = ""
    for selector in [
        "#corePriceDisplay_desktop_feature_div",
        "#corePrice_feature_div",
        "#apex_desktop",
        ".a-price .a-offscreen",
    ]:
        try:
            loc = page.locator(selector)
            if loc.count():
                t = loc.first.inner_text(timeout=2000).strip()
                if t:
                    price_text = t
                    break
        except Exception:
            pass

    print("PAGE PRICE TEXT =", price_text[:300] if price_text else "NOT FOUND")

    # لقطة حقيقية للجزء العلوي من صفحة المنتج
    page.screenshot(
        path=OUT,
        type="jpeg",
        quality=82,
        full_page=False
    )

    browser.close()

print("✅ REAL AMAZON SCREENSHOT CREATED =", OUT)

token = (
    cfg.get("BOT_TOKEN")
    or cfg.get("bot_token")
    or cfg.get("TOKEN")
    or cfg.get("token")
)

review = (
    cfg.get("REVIEW_GROUP_ID")
    or cfg.get("review_group_id")
    or cfg.get("REVIEW")
)

if not token or not review:
    raise SystemExit("❌ بيانات Telegram غير موجودة في config.json")

api_base = f"https://api.telegram.org/bot{token}"

caption = (
    "📸 <b>Amazon Real Screenshot Test</b>\n"
    "✅ لقطة حقيقية من صفحة المنتج\n"
    f"💰 سعر الرادار: <b>{radar_price:,.2f} ج.م</b>\n"
)

if price_text:
    clean = re.sub(r"\s+", " ", price_text)[:300]
    caption += f"🌐 الظاهر في الصفحة: <b>{clean}</b>\n"

caption += "\n⚠️ اختبار للمراجعة فقط — لا يُنشر تلقائيًا."

markup = {
    "inline_keyboard": [
        [
            {"text": "🔗 فتح المنتج", "url": url}
        ]
    ]
}

send_photo_file(
    api_base,
    review,
    OUT,
    caption,
    markup,
    timeout=45
)

print("✅ SCREENSHOT SENT TO REVIEW GROUP")
