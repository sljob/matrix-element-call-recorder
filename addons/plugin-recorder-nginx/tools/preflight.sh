#!/bin/bash
set -euo pipefail
umask 077
BASE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
[[ $# -eq 1 ]] || { echo 'Usage: tools/preflight.sh /absolute/answers.json' >&2; exit 2; }
exec python3 "$BASE/installer/preflight.py" "$1"
