#!/usr/bin/env bash

cd "$HOME/telegram_deals_bot_v1" || exit 1
source .venv/bin/activate

echo "===== EGYPT DEALS BOT ====="

# Amazon Radar
if [ -f amazon_radar.pid ] && kill -0 "$(cat amazon_radar.pid)" 2>/dev/null; then
    echo "✅ Amazon Radar already running PID=$(cat amazon_radar.pid)"
else
    nohup python -u amazon_radar.py > amazon_radar.log 2>&1 &
    echo $! > amazon_radar.pid
    echo "🚀 Amazon Radar started PID=$(cat amazon_radar.pid)"
fi

# Jumia + 2B local scanner
if [ -f egypt_scan.pid ] && kill -0 "$(cat egypt_scan.pid)" 2>/dev/null; then
    echo "✅ Jumia/2B scanner already running PID=$(cat egypt_scan.pid)"
else
    nohup bash egypt_scan_loop.sh > egypt_scan.log 2>&1 &
    echo $! > egypt_scan.pid
    echo "🚀 Jumia/2B scanner started PID=$(cat egypt_scan.pid)"
fi

echo "✅ EGYPT DEALS BOT V1 RUNNING"
