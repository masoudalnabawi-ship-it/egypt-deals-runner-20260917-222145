import asyncio
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "telegram_deals_bot_v7_dev"
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

# Cloudflare handles Telegram moderation; scanner only discovers/verifies/submits.
os.environ.setdefault("DIRECT_FLASH_REVIEW", "false")
os.environ.setdefault("V11_ENABLED_STORES", "noon,noon_minutes,jumia,2b,btech,raya,dream2000,carrefour,raneen,kenzz")
os.environ.setdefault("ENABLED_STORES", os.environ["V11_ENABLED_STORES"])
os.environ.setdefault("MIN_DISCOUNT_PERCENT", "5")
os.environ.setdefault("MIN_SAVING_EGP", "0")
os.environ.setdefault("MAX_POSTS_PER_CYCLE", "10")
os.environ.setdefault("MAX_VERIFY_CANDIDATES", "20")
os.environ.setdefault("REQUEST_TIMEOUT_SECONDS", "20")
os.environ.setdefault("STRICT_VERIFICATION", "true")

import db
import engine
from db import init_db
from v11_state import init_state
from v11_runtime import install_instrumentation

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")

async def main():
    init_db()
    init_state()
    install_instrumentation()
    sent = await engine.scan_once()
    print(f"V11_ONCE_COMPLETE reviews={sent}", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
