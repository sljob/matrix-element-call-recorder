#!/bin/bash
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo 'Нужен root'; exit 1; }
systemctl disable --now matrix-staff-directory.service matrix-staff-directory-collector.service matrix-staff-directory-adapter.service
for UNIT in matrix-staff-directory matrix-staff-directory-collector matrix-staff-directory-adapter; do
 if systemctl is-active --quiet "$UNIT"; then echo "Не удалось остановить $UNIT"; exit 1; fi
done
echo 'Дополнение отключено. Matrix/LiveKit/Recorder не перезапускались.'
echo 'Новые бот/комната, credentials и состояние сохранены; существующие данные не удалены.'
