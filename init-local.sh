#!/bin/bash
# Создаёт каталог данных (по умолчанию local/) из шаблонов conf.example/ и .env.example.
# Ничего не перезаписывает: существующие файлы, ключи и секрет OTP остаются как есть.
#
# usage: ./init-local.sh [--dir <каталог>] [--keys <стенд>]... [--no-chown]
#   --dir       каталог данных (по умолчанию local/ рядом со скриптом)
#   --keys      сгенерировать ключи safety_<стенд>_ro / safety_<стенд>_rw (можно несколько раз)
#   --no-chown  не менять владельца каталогов approver (без Docker; для тестов)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DIR="$HERE/local"
STANDS=()
CHOWN=1
IMAGE="${IMAGE:-safety-mcp-server:0.1.0}"

usage() { sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="$(realpath -m "$2")"; shift 2 ;;
    --keys) STANDS+=("$2"); shift 2 ;;
    --no-chown) CHOWN=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "неизвестный параметр: $1" >&2; usage >&2; exit 1 ;;
  esac
done
for s in "${STANDS[@]}"; do
  [[ "$s" =~ ^[a-z][a-z0-9]{0,15}$ && "$s" != global ]] || { echo "недопустимое имя стенда: $s" >&2; exit 1; }
done

APPROVER_UID="${APPROVER_UID:-1001}"; APPROVER_GID="${APPROVER_GID:-1001}"
if [ -f "$DIR/.env" ]; then
  v=$(sed -n 's/^APPROVER_UID=\([0-9]\+\)$/\1/p' "$DIR/.env"); [ -n "$v" ] && APPROVER_UID=$v
  v=$(sed -n 's/^APPROVER_GID=\([0-9]\+\)$/\1/p' "$DIR/.env"); [ -n "$v" ] && APPROVER_GID=$v
fi

created=(); skipped=()
note_new() { created+=("$1"); }
note_old() { skipped+=("$1"); }

as_root() {  # выполнить команду в контейнере от root с каталогом данных в /w
  docker run --rm --user 0 --entrypoint sh -v "$DIR:/w" "$@"
}
if [ "$CHOWN" = 1 ]; then
  command -v docker >/dev/null || { echo "нужен docker (или запустите с --no-chown)" >&2; exit 1; }
  docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "нет образа $IMAGE: сначала docker compose build" >&2; exit 1; }
fi

umask 077
# 1. каталоги
NEWCONF=0; [ -d "$DIR/conf" ] || NEWCONF=1
for d in conf keys-ro keys-rw state queue queue/requests queue/results approver-state; do
  if [ -d "$DIR/$d" ]; then note_old "$d/"; else mkdir -p "$DIR/$d"; note_new "$d/"; fi
done
chmod 700 "$DIR/keys-ro" 2>/dev/null || true
[ -O "$DIR/keys-rw" ] && chmod 700 "$DIR/keys-rw"
for d in conf state queue queue/requests; do [ -O "$DIR/$d" ] && chmod 755 "$DIR/$d"; done

# 2. шаблоны
umask 022
for f in "$HERE"/conf.example/*; do
  n=$(basename "$f")
  # заглушки known_hosts примера нужны только в новом conf/, в рабочий не подкладываем
  [[ "$n" == known_hosts.* && "$NEWCONF" = 0 && ! -e "$DIR/conf/$n" ]] && continue
  if [ -e "$DIR/conf/$n" ]; then note_old "conf/$n"; else cp "$f" "$DIR/conf/$n"; note_new "conf/$n"; fi
done
if [ -e "$DIR/.env" ]; then note_old ".env"; else cp "$HERE/.env.example" "$DIR/.env"; note_new ".env"; fi

# 3. токен MCP (Bearer для клиента, например opencode)
umask 077
if [ -e "$DIR/mcp-token" ]; then
  note_old "mcp-token"
else
  python3 -c 'import secrets; print(secrets.token_urlsafe(32), end="")' > "$DIR/mcp-token"
  note_new "mcp-token (Bearer-токен для клиента MCP)"
fi

# 4. ключи
umask 077
mkdir -p "$HOME/.ssh"
for s in "${STANDS[@]}"; do
  ro="safety_${s}_ro"; rw="safety_${s}_rw"
  if [ -e "$DIR/keys-ro/$ro" ] || [ -e "$HOME/.ssh/$ro" ]; then
    note_old "ключ $ro"
  else
    ssh-keygen -q -t ed25519 -N "" -C "safety $s ro" -f "$DIR/keys-ro/$ro"
    cp -p "$DIR/keys-ro/$ro" "$DIR/keys-ro/$ro.pub" "$HOME/.ssh/"
    note_new "ключ $ro (local/keys-ro и ~/.ssh)"
  fi
  if [ -e "$HOME/.ssh/$rw.pub" ]; then
    note_old "ключ $rw"
  else
    T=$(mktemp -d)
    ssh-keygen -q -t ed25519 -N "" -C "safety $s rw" -f "$T/$rw"
    cp "$T/$rw.pub" "$HOME/.ssh/$rw.pub"
    if [ "$CHOWN" = 1 ]; then
      docker run --rm --user 0 --entrypoint sh -v "$DIR:/w" -v "$T:/t:ro" "$IMAGE" -c \
        "test ! -e /w/keys-rw/$rw && cp /t/$rw /t/$rw.pub /w/keys-rw/ && chmod 600 /w/keys-rw/$rw"
    else
      cp "$T/$rw" "$T/$rw.pub" "$DIR/keys-rw/"
    fi
    rm -rf "$T"
    note_new "ключ $rw (приватный только в keys-rw, публичный в ~/.ssh)"
  fi
done

# 5. владелец каталогов approver
if [ "$CHOWN" = 1 ]; then
  as_root "$IMAGE" -c "chown -R $APPROVER_UID:$APPROVER_GID /w/keys-rw /w/approver-state /w/queue/results \
    && chmod 700 /w/keys-rw && chmod 755 /w/queue/results /w/approver-state"
else
  note_old "владелец keys-rw, approver-state, queue/results не изменён (--no-chown)"
fi

echo "Каталог данных: $DIR"
[ ${#created[@]} -gt 0 ] && printf '  создано:   %s\n' "${created[@]}"
[ ${#skipped[@]} -gt 0 ] && printf '  оставлено: %s\n' "${skipped[@]}"
cat <<EOF

Дальше:
  1. Впишите стенды и хосты: $DIR/conf/inventory.json (пути ключей: /keys/<имя> и /keys-rw/<имя>)
     и команды: $DIR/conf/commands.json
  2. Ключи хостов: ssh-keyscan -t ed25519 <адрес> >> $DIR/conf/known_hosts.<стенд>  (сверьте отпечаток!)
  3. Обёртка на хостах: SAFETY_CONF=$DIR/conf DEPLOY_SSH_KEY=~/.ssh/<ваш_ключ> DEPLOY_KEY_DIR=~/.ssh \\
       wrapper/deploy.sh <стенд> --install-keys
  4. Запуск: docker compose --project-directory $DIR -f $HERE/docker-compose.yml up -d
     MCP: http://127.0.0.1:8766/mcp, заголовок Authorization: Bearer <содержимое $DIR/mcp-token>
  5. FreeOTP (в своём терминале): docker compose --project-directory $DIR -f $HERE/docker-compose.yml run --rm approver enroll --account <имя>
EOF
