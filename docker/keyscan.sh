#!/bin/sh
# Внутри контейнера: ключ хоста для known_hosts.<стенд> (ed25519) с отпечатком для сверки.
# Пишет в /conf-rw/known_hosts.<стенд> с LF (PowerShell '>>' записал бы UTF-16 и ssh бы его не прочитал).
# usage: keyscan.sh <стенд> <адрес> [порт]
set -eu
s="$1"; a="$2"; p="${3:-22}"
case "$s" in global|*[!a-z0-9]*|[!a-z]*|?????????????????*) echo "недопустимое имя стенда: $s" >&2; exit 1;; esac
T=$(mktemp)
ssh-keyscan -t ed25519 -p "$p" "$a" 2>/dev/null | grep -v '^#' > "$T" || true
[ -s "$T" ] || { echo "нет ответа ssh от $a:$p" >&2; exit 1; }
echo "отпечаток $a: $(ssh-keygen -l -f "$T" | cut -d' ' -f2)  <- сверьте с сервером: ssh-keygen -l -f /etc/ssh/ssh_host_ed25519_key.pub"
f="/conf-rw/known_hosts.$s"
if [ -f "$f" ] && grep -qF "$(cut -d' ' -f3 "$T")" "$f"; then echo "уже есть в known_hosts.$s"
else cat "$T" >> "$f"; echo "добавлено в known_hosts.$s"; fi
