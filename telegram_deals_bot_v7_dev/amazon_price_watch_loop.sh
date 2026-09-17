#!/usr/bin/env bash

cd /root/telegram_deals_bot_v1 || exit 1
source .venv/bin/activate
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
