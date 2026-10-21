#!/bin/sh
# Внутри контейнера: генерация ключей safety_<стенд>_ro|rw в смонтированные папки /out-ro и /out-rw.
# Для Windows, где нет удобного ssh-keygen; права файлов на хосте не важны (см. entry.sh).
# Существующие ключи не перезаписываются.
# usage: keys-init.sh <стенд>...
set -eu
umask 077
for s in "$@"; do
  case "$s" in global|*[!a-z0-9]*|[!a-z]*|?????????????????*) echo "недопустимое имя стенда: $s" >&2; exit 1;; esac
  for role in ro rw; do
    k="/out-$role/safety_${s}_$role"
    if [ -e "$k" ]; then echo "оставлен: safety_${s}_$role"
    else ssh-keygen -q -t ed25519 -N "" -C "safety $s $role" -f "$k"; echo "создан:   safety_${s}_$role"; fi
  done
done
