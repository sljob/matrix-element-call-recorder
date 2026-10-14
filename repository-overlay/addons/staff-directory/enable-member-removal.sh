#!/bin/bash
set -euo pipefail
umask 077
[ "$(id -u)" -eq 0 ] || { echo 'Run as root'; exit 1; }
command -v flock >/dev/null
command -v systemd-run >/dev/null
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec 9>/run/lock/matrix-staff-cloud-install.lock
flock -n 9 || { echo 'Another staff installer is running'; exit 1; }
test -r /etc/matrix-staff-directory/bot.cred
systemctl is-active --quiet matrix-staff-directory
MODE=apply
if [ "$#" -eq 2 ] && [ "$1" = --restore ]; then
 MODE=restore; BACKUP="$2"
 test -d "$BACKUP"
elif [ "$#" -eq 0 ]; then
 BACKUP="$(mktemp -d /root/staff-member-rights.XXXXXX)"
else
 echo 'Usage: enable-member-removal.sh [--restore BACKUP_DIR]'; exit 1
fi
python3 - "$HERE/member-removal.py" <<'PY'
from pathlib import Path
import sys
compile(Path(sys.argv[1]).read_text(),sys.argv[1],'exec')
PY
systemd-run --quiet --wait --pipe --collect \
 --unit="staff-member-rights-$(date +%s)-$RANDOM" \
 -p LoadCredentialEncrypted=bot:/etc/matrix-staff-directory/bot.cred \
 /usr/bin/python3 "$HERE/member-removal.py" "$MODE" "$BACKUP"
echo "Backup (if changed): $BACKUP"
echo "Restore: bash $HERE/enable-member-removal.sh --restore $BACKUP"
echo 'Checked room rights. No users removed; no services/containers restarted; secrets unchanged.'
