#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
if [ -s amazon_bot.pid ]; then
  p="$(cat amazon_bot.pid 2>/dev/null || true)"
  if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then
    echo "Amazon bot already running PID=$p"
    exit 0
  fi
fi
nohup python3 -u bot.py >> amazon_bot.runtime.log 2>&1 &
echo $! > amazon_bot.pid
sleep 2
p="$(cat amazon_bot.pid)"
kill -0 "$p" 2>/dev/null || { echo "Amazon bot failed to start"; tail -n 80 amazon_bot.runtime.log; exit 1; }
echo "Amazon bot started PID=$p"
