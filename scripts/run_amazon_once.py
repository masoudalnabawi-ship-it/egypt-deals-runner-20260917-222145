import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "amazon_dynamic_runtime_v8" / "telegram_deals_bot_v1_ready"
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import httpx
import amazon_radar as radar

async def safe(name, coro, timeout=90):
    try:
        await asyncio.wait_for(coro, timeout=timeout)
        print(f"AMAZON_ONCE {name}=OK", flush=True)
    except asyncio.TimeoutError:
        print(f"AMAZON_ONCE {name}=TIMEOUT", flush=True)
    except Exception as exc:
        print(f"AMAZON_ONCE {name}=ERROR {exc!r}", flush=True)

async def drain_queue(limit=8):
    async with httpx.AsyncClient() as client:
        for i in range(limit):
            job = radar.pop_amazon_candidate()
            if not job:
                break
            try:
                await asyncio.wait_for(radar.process_queue_item(client, job), timeout=55)
            except Exception as exc:
                print(f"AMAZON_ONCE queue[{i}]={exc!r}", flush=True)

async def main():
    # Progressive discovery + priority checks. Every invocation advances persisted state.
    await safe("priority_surface", radar.direct_surface_once("priority"), 70)
    await safe("general_surface", radar.direct_surface_once("general"), 70)
    await safe("deep_discovery", radar.deep_discovery(), 20)
    await safe("hot_watch", radar.hot_watch_once(), 110)
    await safe("ultra_hot", radar.ultra_hot_once(), 130)
    await safe("full_v5", radar.full_v5_watchlist_once(), 130)
    await safe("competitor_trigger", radar.competitor_trigger_once(), 45)
    await drain_queue(8)
    print("AMAZON_ONCE_COMPLETE", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
