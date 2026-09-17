#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
for f in "$ROOT/amazon_radar_ready.pid" "$ROOT/amazon_review_ready.pid"; do
  if [ -s "$f" ]; then
    pid="$(cat "$f" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then kill "$pid" 2>/dev/null || true; fi
    rm -f "$f"
  fi
done
echo "✅ Ready stack stopped"
