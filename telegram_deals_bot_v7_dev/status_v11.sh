#!/usr/bin/env bash
DEV="${V7_DEV:-$HOME/telegram_deals_bot_v7_dev}"
cd "$DEV" || exit 2
if [ -s v11_runtime.pid ] && kill -0 "$(cat v11_runtime.pid)" 2>/dev/null; then
  echo "✅ V11 runtime running PID=$(cat v11_runtime.pid)"
else
  echo "⏹ V11 runtime not running"
fi
for p in amazon_radar.pid amazon_scan.pid amazon_price_watch.pid; do
  if [ -s "$p" ] && kill -0 "$(cat "$p")" 2>/dev/null; then echo "⚠️ Amazon worker running: $p PID=$(cat "$p")"; fi
done
if [ -f v11_state.db ] && [ -f v11_state_report.py ]; then
  echo "--- V11 state ---"
  python v11_state_report.py 2>/dev/null | tail -n 80 || true
fi
[ -f v11_runtime.log ] && { echo "--- runtime log ---"; tail -n 30 v11_runtime.log; }
