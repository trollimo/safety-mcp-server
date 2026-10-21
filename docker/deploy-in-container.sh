#!/bin/bash
# Внутри контейнера от root: запуск wrapper/deploy.sh, когда на машине нет bash/ssh с нормальными правами (Windows).
# /in/deploy_key — ваш ключ доступа к ssh_user; /keys-src, /keys-rw-src — папки с ключами safety (всё только чтение).
# Из /keys-rw-src берутся только публичные ключи. Ключ с паролем: пароль спросит ssh-add (нужен docker run -it).
# usage: deploy-in-container.sh <стенд> [хост] [--install-keys]
set -euo pipefail
umask 077
KD=$(mktemp -d)
cp /keys-src/safety_* "$KD/" 2>/dev/null || true
cp /keys-rw-src/*.pub "$KD/" 2>/dev/null || true
for f in "$KD"/*; do tr -d '\r' < "$f" > "$f.t" && mv "$f.t" "$f"; done
chmod 600 "$KD"/*
tr -d '\r' < /in/deploy_key > "$KD/.deploy_key"; chmod 600 "$KD/.deploy_key"
eval "$(ssh-agent -s)" >/dev/null
trap 'ssh-agent -k >/dev/null 2>&1; rm -rf "$KD"' EXIT
ssh-add -q "$KD/.deploy_key"
rm -f "$KD/.deploy_key"
export DEPLOY_KEY_DIR="$KD" SAFETY_CONF="${SAFETY_CONF:-/conf}"
bash /app/wrapper/deploy.sh "$@"
