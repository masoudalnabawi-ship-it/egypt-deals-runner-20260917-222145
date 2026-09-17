#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
[ -s amazon_bot.pid ] || { echo "No PID"; exit 0; }
p="$(cat amazon_bot.pid 2>/dev/null || true)"
if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then
  kill "$p"
  for _ in 1 2 3 4 5; do kill -0 "$p" 2>/dev/null || break; sleep 1; done
fi
rm -f amazon_bot.pid
echo "Amazon bot stopped"
