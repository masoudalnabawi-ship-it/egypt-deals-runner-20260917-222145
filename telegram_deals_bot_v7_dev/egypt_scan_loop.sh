#!/usr/bin/env bash

cd "$HOME/telegram_deals_bot_v7_dev" || exit 1
source .venv/bin/activate

export ENABLED_STORES="jumia,2b,noon,noon_minutes,btech,raya,dream2000,carrefour,raneen,kenzz"
export PYTHONUNBUFFERED=1

while true; do
    echo
    echo "======================================"
    echo "EGYPT SCAN: $(date)"
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
    print("VERIFIED_SENT =", result, flush=True)
except Exception as e:
    logging.exception("SCAN FAILED")
PY

    echo "NEXT SCAN IN 10 MINUTES"
    sleep 600
done
