# NOON_DEDICATED_WORKER_V1
import os

# Must be set BEFORE config/engine imports.
os.environ["ENABLED_STORES"] = "noon,noon_minutes"
os.environ["V11_ENABLED_STORES"] = "noon,noon_minutes"
os.environ.setdefault("REQUEST_TIMEOUT_SECONDS", "18")

import asyncio
import logging
import random
import time

from engine import scan_once

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | noon-worker | %(message)s",
)

log=logging.getLogger("noon-worker")

INTERVAL=120
SCAN_TIMEOUT=90


async def main():
    log.warning(
        "NOON DEDICATED WORKER ON | stores=noon,noon_minutes "
        "| no_auto_public=1 | isolated_from_main_v11=1"
    )

    while True:
        started=time.monotonic()

        try:
            sent=await asyncio.wait_for(
                scan_once(),
                timeout=SCAN_TIMEOUT,
            )

            log.info(
                "NOON CYCLE COMPLETE | reviews=%s",
                sent,
            )

        except asyncio.TimeoutError:
            log.warning(
                "NOON CYCLE TIMEOUT ISOLATED | %ss",
                SCAN_TIMEOUT,
            )

        except asyncio.CancelledError:
            raise

        except Exception:
            log.exception("NOON CYCLE ERROR")

        elapsed=time.monotonic()-started

        # Respectful cadence + jitter.
        delay=max(
            30,
            INTERVAL-elapsed+random.uniform(5,20),
        )

        await asyncio.sleep(delay)


if __name__ == "__main__":
    asyncio.run(main())
