#!/bin/bash
# Следит за очередью заявок и открывает страницу подтверждения в браузере (запускать на хосте, не в контейнере).
# usage: open-new-requests.sh [каталог queue/requests] [базовый URL]
# Только открывает страницу, ничего не подтверждает.
set -u
DIR="${1:-./queue/requests}"
URL="${2:-${APPROVER_URL:-http://127.0.0.1:8765}}"
OPEN=$(command -v xdg-open || command -v open || true)
[ -n "$OPEN" ] || { echo "нет xdg-open/open" >&2; exit 1; }
declare -A seen
for f in "$DIR"/*.json; do [ -e "$f" ] && seen[$(basename "$f" .json)]=1; done   # старые заявки не открываем
while true; do
  for f in "$DIR"/*.json; do
    [ -e "$f" ] || continue
    id=$(basename "$f" .json)
    [[ "$id" =~ ^[0-9a-f]{32}$ ]] || continue
    [ -n "${seen[$id]:-}" ] && continue
    seen[$id]=1
    "$OPEN" "$URL/r/$id" >/dev/null 2>&1 &
  done
  sleep 1
done
