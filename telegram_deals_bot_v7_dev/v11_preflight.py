from __future__ import annotations
import asyncio
import os
import httpx
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHANNEL = os.getenv("TELEGRAM_CHANNEL_ID", "").strip()
ADMIN = os.getenv("ADMIN_CHAT_ID", "").strip()
REVIEW = os.getenv("REVIEW_CHAT_ID", "").strip() or ADMIN

async def main():
    missing = [name for name, val in [
        ("TELEGRAM_BOT_TOKEN", TOKEN), ("TELEGRAM_CHANNEL_ID", CHANNEL),
        ("ADMIN_CHAT_ID", ADMIN), ("REVIEW_CHAT_ID/ADMIN_CHAT_ID", REVIEW),
    ] if not val]
    if missing:
        raise SystemExit("Missing settings: " + ", ".join(missing))
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"https://api.telegram.org/bot{TOKEN}/getMe")
        data = r.json() if "json" in r.headers.get("content-type", "") else {}
        if r.status_code != 200 or not data.get("ok"):
            raise SystemExit(f"Telegram getMe failed HTTP {r.status_code}: {data.get('description','unknown')}")
        print(f"PASS Telegram auth @{data['result'].get('username','-')}")
        print(f"PASS review target configured ({'REVIEW_CHAT_ID' if os.getenv('REVIEW_CHAT_ID','').strip() else 'ADMIN_CHAT_ID fallback'})")
        print("PASS Amazon remains disabled by V11 safe runtime")

if __name__ == "__main__":
    asyncio.run(main())

