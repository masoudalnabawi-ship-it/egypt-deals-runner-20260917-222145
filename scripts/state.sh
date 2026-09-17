#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE="$ROOT/.runtime_state"
AMZ="$ROOT/amazon_dynamic_runtime_v8/telegram_deals_bot_v1_ready"
V11="$ROOT/telegram_deals_bot_v7_dev"
mkdir -p "$STATE"

restore_artifact() {
  if [ -z "${GITHUB_TOKEN:-}" ] || [ -z "${GITHUB_REPOSITORY:-}" ]; then return 1; fi
  local id
  id="$(curl -fsSL \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H 'Accept: application/vnd.github+json' \
    "https://api.github.com/repos/$GITHUB_REPOSITORY/actions/artifacts?per_page=100" \
    | jq -r '[.artifacts[] | select(.name=="runtime-state" and (.expired|not))] | sort_by(.created_at) | reverse | .[0].id // empty' \
    2>/dev/null || true)"
  [ -n "$id" ] || return 1
  rm -rf "$STATE"/* /tmp/runtime-state.zip
  curl -fsSL -L \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H 'Accept: application/vnd.github+json' \
    "https://api.github.com/repos/$GITHUB_REPOSITORY/actions/artifacts/$id/zip" \
    -o /tmp/runtime-state.zip
  unzip -q -o /tmp/runtime-state.zip -d "$STATE"
  echo "RESTORED_RUNTIME_STATE artifact=$id"
}

hydrate() {
  restore_artifact || true
  [ -s "$STATE/amazon_radar_watch.json" ] || cp "$ROOT/seed_state/amazon_radar_watch.json" "$STATE/amazon_radar_watch.json"
  [ -s "$STATE/amazon_radar_state.json" ] || cp "$ROOT/seed_state/amazon_radar_state.json" "$STATE/amazon_radar_state.json"
  [ -s "$STATE/amazon_manual_watch.txt" ] || cp "$ROOT/seed_state/amazon_manual_watch.txt" "$STATE/amazon_manual_watch.txt"

  cp "$STATE/amazon_radar_watch.json" "$AMZ/.amazon_radar_watch.json"
  cp "$STATE/amazon_radar_state.json" "$AMZ/.amazon_radar_state.json"
  cp "$STATE/amazon_manual_watch.txt" "$AMZ/.amazon_manual_watch.txt"
  [ ! -s "$STATE/v11_deals.db" ] || cp "$STATE/v11_deals.db" "$V11/deals.db"
  [ ! -s "$STATE/v11_state.db" ] || cp "$STATE/v11_state.db" "$V11/v11_state.db"
}

collect() {
  cp "$AMZ/.amazon_radar_watch.json" "$STATE/amazon_radar_watch.json"
  cp "$AMZ/.amazon_radar_state.json" "$STATE/amazon_radar_state.json"
  cp "$AMZ/.amazon_manual_watch.txt" "$STATE/amazon_manual_watch.txt"
  [ ! -f "$V11/deals.db" ] || cp "$V11/deals.db" "$STATE/v11_deals.db"
  [ ! -f "$V11/v11_state.db" ] || cp "$V11/v11_state.db" "$STATE/v11_state.db"
}

case "${1:-}" in
  hydrate) hydrate ;;
  collect) collect ;;
  *) echo "usage: $0 hydrate|collect" >&2; exit 2 ;;
esac
