#!/usr/bin/env bash

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT" || exit 1
if [ -f .venv/bin/activate ]; then source .venv/bin/activate; fi
export PYTHONUNBUFFERED=1

while true; do
    echo
    echo "======================================"
    echo "AMAZON FAST WATCH: $(date)"
    echo "======================================"

    python -u amazon_price_watch.py

    echo "NEXT AMAZON CHECK IN 90 SECONDS"
    sleep 90
done
