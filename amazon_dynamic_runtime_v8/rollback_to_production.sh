#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
bash "$ROOT/stop_ready.sh"
if [ -x "$HOME/amazon_deals_bot/start.sh" ]; then bash "$HOME/amazon_deals_bot/start.sh" || true; fi
if [ -x "$HOME/telegram_deals_bot_v1/start_all.sh" ]; then bash "$HOME/telegram_deals_bot_v1/start_all.sh" || true; fi
echo "✅ Original production start scripts invoked. Original files were never overwritten."
