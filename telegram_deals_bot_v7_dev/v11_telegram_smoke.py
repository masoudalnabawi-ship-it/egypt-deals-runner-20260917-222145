from __future__ import annotations
import asyncio
import base64
import os
import tempfile
import httpx
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
REVIEW = (os.getenv("REVIEW_CHAT_ID", "").strip() or os.getenv("ADMIN_CHAT_ID", "").strip())

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAIAAAACACAIAAABMXPacAAABLklEQVR4nO3RMQ0AMAzAsG386fYvDB+LEUTKnZkT5+mA3zUAawDWAKwBWAOwBmANwBqANQBrANYArAFYA7AGYA3AGoA1AGsA1gCsAVgDsAZgDcAagDUAawDWAKwBWAOwBmANwBqANQBrANYArAFYA7AGYA3AGoA1AGsA1gCsAVgDsAZgDcAagDUAawDWAKwBWAOwBmANwBqANQBrANYArAFYA7AGYA3AGoA1AGsA1gCsAVgDsAZgDcAagDUAawDWAKwBWAOwBmANwBqANQBrANYArAFYA7AGYA3AGoA1AGsA1gCsAVgDsAZgDcAagDUAawDWAKwBWAOwBmANwBqANQBrANYArAFYA7AGYA3AGoA1AGsA1gCsAVgDsAZgDcAagDUAawDWAKwBWAOwBmAL0EkD3wyW87IAAAAASUVORK5CYII="
)

async def main():
    if not TOKEN or not REVIEW:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN or REVIEW_CHAT_ID/ADMIN_CHAT_ID")
    base = f"https://api.telegram.org/bot{TOKEN}"
    async with httpx.AsyncClient(timeout=30) as client:
        me = await client.get(base + "/getMe")
        if me.status_code != 200 or not me.json().get("ok"):
            raise SystemExit(f"Telegram auth failed: HTTP {me.status_code}")
        bot = me.json()["result"]
        keyboard = {"inline_keyboard": [
            [{"text": "🚀 نشر عاجل", "callback_data": "v11_ui_only:u"}, {"text": "📢 نشر عادي", "callback_data": "v11_ui_only:p"}],
            [{"text": "🔗 فتح المنتج", "url": "https://example.com/"}, {"text": "❌ رفض", "callback_data": "v11_ui_only:r"}],
        ]}
        caption = (
            "🧪 V11 TELEGRAM UI SMOKE\n\n"
            "📦 Electric Treadmill Test Product\n"
            "💰 130 EGP\n🏷️ Reference: 12,000 EGP (TEST / unverified)\n"
            "🚨 سعر غير طبيعي — مراجعة عاجلة\n\n"
            "TEST ONLY — no store/Amazon request was made.\n"
            "Buttons in this card are UI-only; do not use it for publishing."
        )
        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            f.write(PNG); f.flush()
            with open(f.name, "rb") as img:
                r = await client.post(
                    base + "/sendPhoto",
                    data={"chat_id": REVIEW, "caption": caption, "reply_markup": __import__("json").dumps(keyboard, ensure_ascii=False)},
                    files={"photo": ("v11-smoke.png", img, "image/png")},
                )
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or not data.get("ok"):
            raise SystemExit(f"Smoke send failed: HTTP {r.status_code} | {data.get('description','unknown')}")
        print(f"PASS Telegram auth @{bot.get('username','-')}")
        print(f"PASS review photo+buttons message_id={data['result'].get('message_id')}")
        print("PASS Amazon/store requests=0")
        print("NOTE buttons are deliberately UI-only in this smoke card")

if __name__ == "__main__":
    asyncio.run(main())

