#!/bin/bash
# Auditable GitHub source installer. No encoded application payloads.
set -euo pipefail
umask 077
command -v python3 >/dev/null || { echo 'python3 is required'; exit 1; }
exec python3 - "$@" <<'BOOTSTRAP_PY'
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.request

REPOSITORY = 'sljob/matrix-element-call-recorder'
if len(sys.argv) != 3 or sys.argv[1] not in ('--check', '--install'):
    raise SystemExit('Usage: bash install.sh --check|--install /root/answers.conf')
if os.geteuid() != 0:
    raise SystemExit('Run as root')
mode, answers = sys.argv[1], Path(sys.argv[2]).resolve()
values = {}
for raw in answers.read_text(encoding='utf-8').splitlines():
    raw = raw.strip()
    if not raw or raw.startswith('#'):
        continue
    if not re.match(r'^[A-Z][A-Z0-9_]*=', raw):
        raise SystemExit('Invalid answers syntax')
    key, value = raw.split('=', 1)
    if key in values:
        raise SystemExit('Duplicate setting: ' + key)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    values[key] = value
commit = values.get('SOURCE_COMMIT', '')
if not re.fullmatch(r'[0-9a-fA-F]{40}', commit):
    raise SystemExit('SOURCE_COMMIT must be the full 40-character commit SHA containing the published source release')
commit = commit.lower()
base = 'https://raw.githubusercontent.com/' + REPOSITORY + '/' + commit + '/'

def download(path, limit):
    request = urllib.request.Request(base + path, headers={'User-Agent': 'matrix-recorder-source-installer'})
    with urllib.request.urlopen(request, timeout=60) as response:
        if not response.url.startswith(base):
            raise RuntimeError('Unexpected download redirect')
        data = response.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError('Download too large: ' + path)
    return data

try:
    print('Source repository:', REPOSITORY, flush=True)
    print('Pinned commit:', commit, flush=True)
    manifest = json.loads(download('installer/sources.json', 1048576))
    if manifest.get('schema') != 1 or not isinstance(manifest.get('files'), dict):
        raise RuntimeError('Invalid source manifest')
    entries = manifest['files']
    if not 1 <= len(entries) <= 100 or 'installer/install.py' not in entries:
        raise RuntimeError('Incomplete source manifest')
    with tempfile.TemporaryDirectory(prefix='matrix-recorder-source.') as temp:
        root = Path(temp)
        for path, expected in entries.items():
            parts = path.split('/')
            if (not re.fullmatch(r'[A-Za-z0-9_./-]+', path) or
                    any(p in ('', '.', '..') for p in parts) or
                    not re.fullmatch(r'[0-9a-f]{64}', expected)):
                raise RuntimeError('Invalid manifest entry')
            data = download(path, 5 * 1024 * 1024)
            if hashlib.sha256(data).hexdigest() != expected:
                raise RuntimeError('SHA256 mismatch: ' + path)
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            print('Verified:', path, flush=True)
        # Every file is fetched and verified before the privileged engine executes.
        result = subprocess.run([sys.executable, str(root / 'installer/install.py'), mode, str(answers)])
        raise SystemExit(result.returncode)
except (OSError, ValueError, RuntimeError) as error:
    raise SystemExit('Source installation stopped: ' + str(error))
BOOTSTRAP_PY
