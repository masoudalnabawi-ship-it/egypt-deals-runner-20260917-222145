#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
PY="$ROOT/.venv/bin/python"
RADAR="$ROOT/telegram_deals_bot_v1_ready"
REVIEW="$ROOT/amazon_deals_bot_ready"

[ -x "$PY" ] || { echo "❌ Run install_and_start.sh first"; exit 1; }
[ -f "$RADAR/.env" ] || { echo "❌ Missing private .env in ready runtime"; exit 1; }
[ -f "$REVIEW/config.json" ] || { echo "❌ Missing private review config.json"; exit 1; }

stop_pid() {
  local file="$1"
  if [ -s "$file" ]; then
    local pid
    pid="$(cat "$file" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      sleep 1
    fi
    rm -f "$file"
  fi
}

stop_pid "$ROOT/amazon_radar_ready.pid"
stop_pid "$ROOT/amazon_review_ready.pid"

cd "$REVIEW"
nohup "$PY" -u bot.py >> "$ROOT/amazon_review_ready.log" 2>&1 &
echo $! > "$ROOT/amazon_review_ready.pid"

cd "$RADAR"
nohup "$PY" -u amazon_radar.py >> "$ROOT/amazon_radar_ready.log" 2>&1 &
echo $! > "$ROOT/amazon_radar_ready.pid"

sleep 2
for f in "$ROOT/amazon_review_ready.pid" "$ROOT/amazon_radar_ready.pid"; do
  pid="$(cat "$f")"
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "❌ Failed to start $(basename "$f")"
    tail -n 80 "$ROOT/amazon_review_ready.log" 2>/dev/null || true
    tail -n 80 "$ROOT/amazon_radar_ready.log" 2>/dev/null || true
    exit 1
  fi
done

echo "✅ Amazon Dynamic Ready stack is running"
echo "Radar PID:  $(cat "$ROOT/amazon_radar_ready.pid")"
echo "Review PID: $(cat "$ROOT/amazon_review_ready.pid")"
