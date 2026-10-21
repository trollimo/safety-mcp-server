#!/bin/bash
# usage: deploy.sh <stand> [host] [--install-keys]
# Генерирует правила из commands.json и копирует mcp-wrapper + rules.* в ~/.mcp-ssh/ пользователя ssh_user.
# Запускает ЧЕЛОВЕК своим обычным доступом к ssh_user (DEPLOY_SSH_KEY — необязательный путь к ключу).
# НЕ использует ключи MCP (ro/rw). Пользователей не создаёт, системные файлы не меняет.
# Конфиг: MCP_SSH_CONF (по умолчанию /etc/mcp-ssh). Публичные ключи: <путь key-path>.pub
# DEPLOY_KEY_DIR: каталог с ключами на этой машине, если в inventory указаны контейнерные пути (/keys/...).
set -euo pipefail
STAND=${1:?usage: deploy.sh <stand> [host] [--install-keys]}; shift
ONLY=""; INSTALL=0
for a in "$@"; do case "$a" in --install-keys) INSTALL=1;; *) ONLY=$a;; esac; done
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${PYTHON:-python3}"
SRV="$HERE/../server.py"
CONF="${MCP_SSH_CONF:-/etc/mcp-ssh}"
export MCP_SSH_CONF="$CONF"

U=$("$PY" "$SRV" ssh-user "$STAND")
BASE=(-o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$CONF/known_hosts.$STAND" -o LogLevel=ERROR)
SSHO=("${BASE[@]}")
[ -n "${DEPLOY_SSH_KEY:-}" ] && SSHO+=(-i "$DEPLOY_SSH_KEY" -o IdentitiesOnly=yes)

"$PY" "$SRV" list-hosts "$STAND" | while read -r NAME ADDR; do
  [[ -z "$ONLY" || "$ONLY" == "$NAME" ]] || continue
  T=$(mktemp -d)
  "$PY" "$SRV" gen-rules "$STAND" "$NAME" ro > "$T/rules.ro"
  "$PY" "$SRV" gen-rules "$STAND" "$NAME" rw > "$T/rules.rw" 2>/dev/null || rm -f "$T/rules.rw"
  H=$(ssh -n "${SSHO[@]}" "$U@$ADDR" 'echo $HOME'); D="$H/.mcp-ssh"
  RM=""; [ -f "$T/rules.rw" ] || RM="rm -f $D/rules.rw;"
  ssh -n "${SSHO[@]}" "$U@$ADDR" "umask 077; mkdir -p $D"
  scp -q "${SSHO[@]}" "$HERE/mcp-wrapper" "$T"/rules.* "$U@$ADDR:$D/"
  ssh -n "${SSHO[@]}" "$U@$ADDR" "chmod 700 $D $D/mcp-wrapper; chmod 600 $D/rules.*; $RM $D/mcp-wrapper --version"
  rm -rf "$T"

  for ROLE in ro rw; do
    KP=$("$PY" "$SRV" key-path "$STAND" "$ROLE" 2>/dev/null) || continue
    [ -n "${DEPLOY_KEY_DIR:-}" ] && KP="$DEPLOY_KEY_DIR/$(basename "$KP")"   # пути в inventory — контейнерные
    PUB="$KP.pub"; [ -f "$PUB" ] || { echo "$NAME $ROLE: нет файла $PUB, пропускаю"; continue; }
    LINE="command=\"$D/mcp-wrapper $ROLE\",restrict $(cat "$PUB")"
    KEY=$(awk '{print $2}' "$PUB")
    AK=$(ssh -n "${SSHO[@]}" "$U@$ADDR" 'cat ~/.ssh/authorized_keys 2>/dev/null' || true)
    if grep -qxF "$LINE" <<<"$AK"; then echo "$NAME $ROLE: ключ с ограничением на месте"
    elif grep -qF "$KEY" <<<"$AK"; then echo "ВНИМАНИЕ $NAME $ROLE: ключ уже есть, но строка отличается (возможно, без ограничения). Замените её на: $LINE"
    elif [ "$INSTALL" = 1 ]; then
      ssh -n "${SSHO[@]}" "$U@$ADDR" "umask 077; mkdir -p ~/.ssh; cp -p ~/.ssh/authorized_keys ~/.ssh/authorized_keys.bak-mcp 2>/dev/null; printf '%s\n' '$LINE' >> ~/.ssh/authorized_keys"
      echo "$NAME $ROLE: ключ добавлен (бэкап ~/.ssh/authorized_keys.bak-mcp)"
    else echo "$NAME $ROLE: добавьте в ~/.ssh/authorized_keys строку: $LINE"; fi
  done

  # проверка: через ro-ключ любая команда вне списка должна быть отклонена кодом 126
  K=$("$PY" "$SRV" key-path "$STAND" ro)
  [ -n "${DEPLOY_KEY_DIR:-}" ] && K="$DEPLOY_KEY_DIR/$(basename "$K")"
  set +e; ssh -n -i "$K" -o IdentitiesOnly=yes "${BASE[@]}" "$U@$ADDR" 'id' >/dev/null 2>&1; RC=$?; set -e
  if [ "$RC" = 126 ]; then echo "$NAME: ok (ro-ключ ограничен)"
  else echo "$NAME: ПРОВЕРКА НЕ ПРОЙДЕНА (код $RC): ro-ключ не ограничен или не добавлен"; fi
done
