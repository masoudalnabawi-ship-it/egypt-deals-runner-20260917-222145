# V11_FAST_SAFE_CYCLE
export V11_ENABLED_STORES="noon,noon_minutes,jumia,2b,btech,raya,dream2000,carrefour,raneen,kenzz"
export CHECK_INTERVAL_MINUTES="2"
export REQUEST_TIMEOUT_SECONDS="20"
#!/usr/bin/env bash

# V11 FAIR REVIEW QUEUE
# Allow all stores a fair chance while preserving priority order.
export MAX_POSTS_PER_CYCLE=10
export MAX_VERIFY_CANDIDATES=20
set -Eeuo pipefail
DEV="${V7_DEV:-$HOME/telegram_deals_bot_v7_dev}"
cd "$DEV"
[ -f .venv/bin/activate ] && source .venv/bin/activate

# Refuse only Amazon workers that belong to THIS DEV tree.
# External/legacy/production Amazon workers are reported but never touched.
for pidfile in amazon_radar.pid amazon_scan.pid amazon_price_watch.pid; do
  if [ -s "$pidfile" ]; then
    pid="$(cat "$pidfile" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || ps -p "$pid" -o args= 2>/dev/null || true)"
      cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
      if [ "$cwd" = "$DEV" ] || printf '%s' "$cmd" | grep -Fq "$DEV"; then
        echo "❌ Refusing safe runtime: DEV Amazon worker is already running ($pidfile PID=$pid)"
        exit 20
      else
        echo "ℹ️ External Amazon worker detected and left untouched: $pidfile PID=$pid CWD=${cwd:-unknown}"
      fi
    fi
  fi
done

if [ -s v11_runtime.pid ]; then
  pid="$(cat v11_runtime.pid 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    echo "✅ V11 safe DEV runtime already running PID=$pid"
    exit 0
  fi
fi

python -m py_compile config.py db.py models.py engine.py telegram_client.py flash_review_v9.py review_media.py store_page_capture.py v11_runtime.py promo_stack_v11.py v11_preflight.py
python v11_preflight.py

export V11_ENABLED_STORES="noon,noon_minutes,jumia,2b,btech,raya,dream2000,carrefour,raneen,kenzz"
nohup python -u v11_runtime.py > v11_runtime.log 2>&1 &
echo $! > v11_runtime.pid
sleep 1
pid="$(cat v11_runtime.pid)"
if ! kill -0 "$pid" 2>/dev/null; then
  echo "❌ Runtime exited during startup"
  tail -n 80 v11_runtime.log || true
  exit 21
fi
echo "✅ V11 SAFE DEV RUNTIME STARTED PID=$pid"
echo "✅ Amazon=OFF"
echo "Log: $DEV/v11_runtime.log"

