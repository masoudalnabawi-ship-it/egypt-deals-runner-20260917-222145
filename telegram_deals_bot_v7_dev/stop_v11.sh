#!/usr/bin/env bash
set -Eeuo pipefail
DEV="${V7_DEV:-$HOME/telegram_deals_bot_v7_dev}"
cd "$DEV"
if [ ! -s v11_runtime.pid ]; then echo "V11 runtime is not recorded as running"; exit 0; fi
pid="$(cat v11_runtime.pid 2>/dev/null || true)"
if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
  kill "$pid"
  for _ in 1 2 3 4 5; do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
  kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
fi
rm -f v11_runtime.pid
echo "✅ V11 runtime stopped"

