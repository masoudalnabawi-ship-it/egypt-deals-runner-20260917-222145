#!/usr/bin/env bash

cd /root/telegram_deals_bot_v1 || exit 1
source .venv/bin/activate

export ENABLED_STORES="amazon"
export MIN_SAVING_EGP="50"
export MAX_VERIFY_CANDIDATES="30"
export MAX_POSTS_PER_CYCLE="10"
export PYTHONUNBUFFERED=1

while true; do
    echo
    echo "======================================"
    echo "AMAZON LIVE SCAN: $(date)"
    echo "======================================"

    python -u - <<'PY'
import asyncio
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

from engine import scan_once

try:
    result = asyncio.run(scan_once())
    print("AMAZON VERIFIED_SENT =", result, flush=True)
except Exception:
    logging.exception("AMAZON SCAN FAILED")
PY

    echo "NEXT AMAZON SCAN IN 5 MINUTES"
    sleep 300
done
