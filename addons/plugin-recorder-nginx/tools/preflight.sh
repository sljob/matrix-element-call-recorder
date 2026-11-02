#!/bin/bash
set -euo pipefail
umask 077
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
[[ $# -eq 1 ]] || { echo "Использование: tools/preflight.sh answers.json" >&2; exit 2; }
exec python3 - "$1" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'installer'))
from install import read_config, run, inspect, inject_nginx
from preflight import Preflight
cfg=read_config(sys.argv[1])
Preflight(cfg, run, inspect, inject_nginx).run_all()
print('PREFLIGHT OK: установка не выполнялась; конфигурации не изменялись.')
PY
