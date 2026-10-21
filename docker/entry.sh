#!/bin/sh
# Точка входа контейнера: копирует ключи из смонтированной папки (только чтение) в tmpfs с правами 600.
# Так права файлов на хосте не важны: на Windows (NTFS) bind-монт виден как 0777, и ssh отверг бы ключ.
# Источник -> назначение: /keys-src -> /keys (MCP, ro-ключи), /keys-rw-src -> /keys-rw (approver, rw-ключи).
# Копируется, только если назначение доступно на запись (tmpfs этого сервиса). CRLF убирается.
set -eu
umask 077
copy_keys() {
  src="$1"; dst="$2"
  [ -d "$src" ] && [ -w "$dst" ] || return 0
  for f in "$src"/*; do
    [ -f "$f" ] || continue
    n=$(basename "$f")
    case "$n" in *[!A-Za-z0-9._-]*|.*) continue ;; esac
    tr -d '\r' < "$f" > "$dst/$n"
    chmod 600 "$dst/$n"
  done
}
copy_keys "${SAFETY_KEYS_SRC:-/keys-src}" "${SAFETY_KEYS_DST:-/keys}"
copy_keys "${SAFETY_KEYS_RW_SRC:-/keys-rw-src}" "${SAFETY_KEYS_RW_DST:-/keys-rw}"
exec "$@"
