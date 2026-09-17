#!/usr/bin/env bash
set -Eeuo pipefail
DEV="${V7_DEV:-$HOME/telegram_deals_bot_v7_dev}"
PY="$DEV/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || command -v python)"
cd "$DEV"

if [ -s competitor_intel.pid ]; then
  old="$(cat competitor_intel.pid 2>/dev/null || true)"
  if [ -n "$old" ] && kill -0 "$old" 2>/dev/null; then
    cwd="$(readlink -f "/proc/$old/cwd" 2>/dev/null || true)"
    cmd="$(tr '\0' ' ' < "/proc/$old/cmdline" 2>/dev/null || true)"
    if [ "$cwd" = "$DEV" ] && printf '%s' "$cmd" | grep -Fq 'competitor_intel.py'; then
      echo "✅ Competitor Intelligence already running PID=$old"
      exit 0
    fi
  fi
  rm -f competitor_intel.pid
fi

nohup "$PY" -u competitor_intel.py >> competitor_intel.log 2>&1 &
pid=$!
echo "$pid" > competitor_intel.pid
sleep 4
kill -0 "$pid" 2>/dev/null || {
  echo "❌ Competitor Intelligence failed"
  tail -n 80 competitor_intel.log 2>/dev/null || true
  exit 1
}
echo "✅ COMPETITOR INTELLIGENCE STARTED PID=$pid"
echo "Log: $DEV/competitor_intel.log"
