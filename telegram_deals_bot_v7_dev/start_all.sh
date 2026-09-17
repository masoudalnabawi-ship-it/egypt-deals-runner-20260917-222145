#!/usr/bin/env bash
set -Eeuo pipefail
DEV="${V7_DEV:-$HOME/telegram_deals_bot_v7_dev}"
exec bash "$DEV/start_v11_safe.sh"
