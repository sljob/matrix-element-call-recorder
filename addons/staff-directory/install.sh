#!/bin/bash
set -euo pipefail
umask 077
[ "$(id -u)" -eq 0 ] || { echo 'Нужен root'; exit 1; }
command -v python3 >/dev/null
command -v flock >/dev/null
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec 9>/run/lock/matrix-staff-cloud-install.lock
flock -n 9 || { echo 'Другой установщик дополнения уже работает'; exit 1; }
python3 - "$HERE" <<'PY'
from pathlib import Path
import sys
for p in Path(sys.argv[1]).glob('*.py'):
    compile(p.read_text(),str(p),'exec')
print('Синтаксис пакета проверен; основной installer не вызывается.')
PY
python3 -u "$HERE/provision.py"
echo 'ЖУРНАЛ: addon установлен; credentials encrypted; основные контейнеры не перезапускались.'
