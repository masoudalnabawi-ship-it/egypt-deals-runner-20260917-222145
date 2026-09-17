#!/usr/bin/env bash
set -Eeuo pipefail
DEV="${V7_DEV:-$HOME/telegram_deals_bot_v7_dev}"
OUT="${V7_OUT:-/sdcard/Download}"
STAMP="$(date +%Y%m%d_%H%M%S)"
TMP="/tmp/v11_checkpoint_$STAMP"
PACK="$OUT/V11_CONTINUATION_CHECKPOINT_$STAMP.tar.gz"
[ -d "$DEV" ] || { echo "DEV missing: $DEV"; exit 2; }
mkdir -p "$TMP/source" "$OUT"
tar -C "$DEV" \
  --exclude='./.venv' --exclude='./.git' --exclude='./.wrangler' \
  --exclude='./__pycache__' --exclude='*/__pycache__' \
  --exclude='./_v7_backups' --exclude='./_v11_backup_*' \
  --exclude='.env' --exclude='.env.*' --exclude='*.db' --exclude='*.sqlite*' \
  --exclude='*.log' --exclude='*.pid' --exclude='*.tar.gz' --exclude='*.zip' \
  --exclude='.amazon_hard_block_state.json' --exclude='*state*.json' \
  -cf - . | tar -C "$TMP/source" -xf -
(
  cd "$TMP/source"
  python -m compileall -q .
)
if grep -RPEq --exclude-dir='__pycache__' '[0-9]{6,12}:[A-Za-z0-9_-]{30,}' "$TMP/source"; then
  echo "❌ Token-like text found in source; checkpoint not created."
  exit 20
fi
cat > "$TMP/CONTINUE_FROM_HERE.md" <<EOF
# Egypt Deals Bot V11 Continuation
Generated: $(date -Is 2>/dev/null || date)
DEV: ~/telegram_deals_bot_v7_dev
V11 safe runtime launcher: start_v11_safe.sh
Amazon stays OFF until a separately controlled clean canary.
Production stays untouched until explicit promotion.
EOF
(
  cd "$TMP"
  find . -type f ! -name SHA256SUMS.txt -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS.txt
)
tar -C "$TMP" -czf "$PACK" .
echo "✅ $PACK"
