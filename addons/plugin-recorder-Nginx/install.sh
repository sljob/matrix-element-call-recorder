#!/bin/bash
set -euo pipefail
umask 077
exec python3 "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/installer/install.py" "$@"
