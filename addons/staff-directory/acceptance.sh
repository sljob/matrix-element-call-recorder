#!/bin/bash
set -euo pipefail
umask 077
[ "$(id -u)" -eq 0 ] || { echo 'Run as root'; exit 1; }
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
for UNIT in matrix-staff-directory matrix-staff-directory-collector matrix-staff-directory-adapter; do systemctl is-active --quiet "$UNIT"; done
DOMAIN="$(python3 -c 'import json; from urllib.parse import urlsplit; print(urlsplit(json.load(open("/etc/matrix-staff-directory/config.json"))["homeserver"]).hostname)')"
read -r -p "Matrix ID тестового пользователя [@user3:$DOMAIN]: " TARGET </dev/tty
TARGET="${TARGET:-@user3:$DOMAIN}"
echo "Проверяется $TARGET. Сервисы не перезапускаются, секреты не выводятся."
prompt() { printf '\n%s\n' "$1"; read -r -p 'После выполнения нажмите Enter: ' REPLY </dev/tty; }
check() {
 systemd-run --quiet --wait --pipe --collect --unit="staff-acceptance-$(date +%s)-$RANDOM" \
  -p LoadCredentialEncrypted=bot:/etc/matrix-staff-directory/bot.cred \
  -p LoadCredentialEncrypted=livekit:/etc/matrix-staff-directory/livekit.cred \
  /usr/bin/python3 "$HERE/acceptance.py" "$1" "$2" "$TARGET"
}
prompt '1/4. Выйдите из звонков, отправьте сообщение с телефона и подтвердите доставку.'
check recent 300
prompt '2/4. Войдите в тестовую конференцию и оставайтесь в ней до PASS.'
check call 240
prompt '3/4. Выйдите из конференции и отправьте ещё одно сообщение.'
check after_call 300
prompt '4/4. Авиарежим + выключенный Wi-Fi. Другие устройства этого аккаунта тоже отключите от сети. Ждите 10–15 минут.'
check offline 900
echo 'CLOUD ACCEPTANCE: PASS (4/4). Верните телефону сеть.'
echo 'Проверена серверная публикация; проверьте те же изменения в интерфейсе Сотрудников.'
echo 'Это не тест foreground, блокировки экрана или отсутствия фоновой синхронизации.'
