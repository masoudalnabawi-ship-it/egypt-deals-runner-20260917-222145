#!/usr/bin/env bash

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT" || exit 1
if [ -f .venv/bin/activate ]; then source .venv/bin/activate; fi

export ENABLED_STORES="jumia,2b,noon"
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
