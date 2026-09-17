import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import quote_plus

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


async def scan_department_batch(count=8):
    """Progressively cover every Amazon department.

    23 department indexes / 8 per run => full department rotation
    in about 3 successful workflow runs.
    Each department advances through its own pages over time.
    """
    depts = list(radar.AMAZON_DEPARTMENT_INDEXES)

    if not depts:
        return

    try:
        start = int(radar.state.get("github_all_dept_index", 0))
    except Exception:
        start = 0

    start %= len(depts)
    attempted = 0

    async with httpx.AsyncClient() as client:
        for offset in range(min(count, len(depts))):
            pos = (start + offset) % len(depts)
            dept_name, dept_index = depts[pos]
            surface = "dept_" + dept_name

            page_key = "github_all_dept_page_" + dept_name

            try:
                page = int(radar.state.get(page_key, 1))
            except Exception:
                page = 1

            page = max(1, min(page, 20))

            url = (
                radar.AMAZON
                + "/s?i="
                + quote_plus(dept_index)
                + "&s=featured-rank&page="
                + str(page)
            )

            attempted += 1

            html = await radar.fetch_amazon_direct_surface(
                client,
                surface,
                url,
            )

            if not html:
                print(
                    "🌍 AMAZON ALL-DEPT",
                    dept_name,
                    "| PAGE =", page,
                    "| SKIPPED/BACKOFF",
                    flush=True,
                )
                # Advance one department so a bad category cannot block all others.
                break

            items = radar.parse_amazon_direct_surface(html)

            if items:
                added, hot = await radar.ingest_amazon_direct_surface(
                    surface,
                    items,
                )

                radar.state[page_key] = 1 if page >= 20 else page + 1

                print(
                    "🌍 AMAZON ALL-DEPT",
                    dept_name,
                    "| PAGE =", page,
                    "| FOUND =", len(items),
                    "| NEW =", added,
                    "| HOT =", hot,
                    flush=True,
                )
            else:
                radar.state[page_key] = 1

            await asyncio.sleep(0.35)

    radar.state["github_all_dept_index"] = (
        start + max(1, attempted)
    ) % len(depts)

    async with radar.lock:
        radar.save_files()


async def main():
    # User priority: every Amazon department, including 5%+ deals.
    await safe("all_departments", scan_department_batch(8), 170)

    # Progressive discovery + priority checks.
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
