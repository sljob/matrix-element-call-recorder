#!/bin/bash
set -euo pipefail
umask 077
[ "$(id -u)" -eq 0 ] || { echo 'Нужен root'; exit 1; }
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP=/opt/matrix-staff-directory
exec 9>/run/lock/matrix-staff-cloud-install.lock
flock -n 9 || { echo 'Другой updater работает'; exit 1; }
for UNIT in matrix-staff-directory matrix-staff-directory-collector matrix-staff-directory-adapter; do systemctl is-active --quiet "$UNIT"; done
python3 - "$HERE" <<'PY'
import json,hashlib,sys
from pathlib import Path
root=Path(sys.argv[1]);app=Path('/opt/matrix-staff-directory')
cfg=json.loads(Path('/etc/matrix-staff-directory/config.json').read_text())
if cfg['homeserver']!='https://cloud.milorada.ru' or cfg.get('server_directory_version')!='0.1.5-cloud-candidate':
    raise SystemExit('STOP: patch only for installed Cloud 0.1.5')
if (app/'device_origin.py').exists():raise SystemExit('STOP: patch already applied; nothing overwritten')
for name,digest in json.loads((root/'expected.json').read_text()).items():
    if hashlib.sha256((app/name).read_bytes()).hexdigest()!=digest:raise SystemExit('STOP: unexpected source '+name)
for path in root.glob('*.py'):compile(path.read_text(),str(path),'exec')
PY
BACKUP="$(mktemp -d /root/staff-origin-backup.XXXXXX)"
cp -a "$APP/collector.py" "$APP/staff_directory.py" "$APP/docker_adapter.py" "$BACKUP/"
cp -a /etc/matrix-staff-directory/config.json "$BACKUP/config.json"
cp "$HERE/room_patch.py" "$BACKUP/room_patch.py"
cat > "$BACKUP/rollback.sh" <<'ROLLBACK'
#!/bin/bash
set -euo pipefail
umask 077
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
systemctl stop matrix-staff-directory matrix-staff-directory-collector matrix-staff-directory-adapter
cp -a "$HERE/collector.py" "$HERE/staff_directory.py" "$HERE/docker_adapter.py" /opt/matrix-staff-directory/
cp -a "$HERE/config.json" /etc/matrix-staff-directory/config.json
trap 'systemctl start matrix-staff-directory-collector matrix-staff-directory matrix-staff-directory-adapter' EXIT
rm -f /opt/matrix-staff-directory/device_origin.py
systemd-run --quiet --wait --pipe --collect --unit="staff-room-restore-$RANDOM" \
 -p LoadCredentialEncrypted=bot:/etc/matrix-staff-directory/bot.cred \
 /usr/bin/python3 "$HERE/room_patch.py" rollback "$HERE"
systemctl start matrix-staff-directory-collector matrix-staff-directory matrix-staff-directory-adapter
trap - EXIT
echo 'Прежний код, интервалы и права восстановлены; основной стек не изменён.'
ROLLBACK
chmod 700 "$BACKUP/rollback.sh"
echo "Резервная копия/откат: bash $BACKUP/rollback.sh"
rollback_on_error() {
 rc=$?
 trap - EXIT
 echo 'Патч не завершён; попытка восстановления только дополнения.'
 bash "$BACKUP/rollback.sh" || echo "ВНИМАНИЕ: восстановление требует проверки: $BACKUP/rollback.sh"
 exit "$rc"
}
trap rollback_on_error EXIT
systemctl stop matrix-staff-directory matrix-staff-directory-collector matrix-staff-directory-adapter
# Credentials stay encrypted on disk. Only bot credential enters transient room editor.
systemd-run --quiet --wait --pipe --collect --unit="staff-room-permissions-$RANDOM" \
 -p LoadCredentialEncrypted=admin:/etc/matrix-staff-directory/admin.cred \
 -p LoadCredentialEncrypted=bot:/etc/matrix-staff-directory/bot.cred \
 /usr/bin/python3 "$HERE/room_patch.py" apply "$BACKUP"
for NAME in collector.py staff_directory.py docker_adapter.py; do
 cp "$HERE/$NAME" "$APP/$NAME.new"
 chmod --reference="$APP/$NAME" "$APP/$NAME.new"
 chown --reference="$APP/$NAME" "$APP/$NAME.new"
 mv "$APP/$NAME.new" "$APP/$NAME"
done
install -o root -g root -m 600 "$HERE/device_origin.py" "$APP/device_origin.py"
python3 - <<'CONFIGPY'
import json,os
from pathlib import Path
p=Path('/etc/matrix-staff-directory/config.json')
cfg=json.loads(p.read_text());cfg['poll_seconds']=5
# existing file mode/owner preserved; no credentials in this JSON
with p.open('w') as f:json.dump(cfg,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
CONFIGPY
systemctl start matrix-staff-directory-collector matrix-staff-directory-adapter
python3 - <<'PY'
import json,time
from pathlib import Path
path=Path('/var/lib/matrix-staff-directory/devices.json');deadline=time.monotonic()+120
while True:
    try:
        data=json.loads(path.read_text())
        if 0<=time.time()-data['checked_at']<90 and all('kind' in v for v in data['users'].values()):break
    except (OSError,ValueError,KeyError):pass
    if time.monotonic()>deadline:raise SystemExit('Collector readiness timeout')
    time.sleep(2)
PY
systemd-run --quiet --wait --pipe --collect --unit="staff-origin-publish-$RANDOM" \
 -p LoadCredentialEncrypted=bot:/etc/matrix-staff-directory/bot.cred \
 -p LoadCredentialEncrypted=livekit:/etc/matrix-staff-directory/livekit.cred \
 /usr/bin/python3 "$APP/staff_directory.py" --once
systemd-run --quiet --wait --pipe --collect --unit="staff-origin-verify-$RANDOM" \
 -p LoadCredentialEncrypted=bot:/etc/matrix-staff-directory/bot.cred \
 /usr/bin/python3 "$HERE/room_patch.py" verify "$BACKUP"
systemctl start matrix-staff-directory
sleep 3
for UNIT in matrix-staff-directory matrix-staff-directory-collector matrix-staff-directory-adapter; do systemctl is-active --quiet "$UNIT"; done
trap - EXIT
printf '\n%s Origin patch: bot-only messages, last-device origin; backup=%s\n' "$(date -Is)" "$BACKUP" >> /root/staff-directory-changes.log
echo 'ГОТОВО: только бот публикует сообщения/реакции; добавлен тип последнего обращения.'
echo 'Администратор PL99 сохраняет приглашения, но может повышать других людей до PL99; выше не может.'
echo 'Интервалы addon: publisher=5s, adapter=5s, devices=15s; окно недавней активности=600s без изменения.'
echo 'Matrix/LiveKit/Recorder, compose, answers.conf, credentials и профиль не изменялись.'
echo 'Тип — по User-Agent; фоновые запросы тоже учитываются. Неизвестный клиент не угадываем.'
echo "Откат дополнения: bash $BACKUP/rollback.sh"
