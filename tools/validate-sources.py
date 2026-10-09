#!/usr/bin/env python3
"""Offline syntax and manifest validation; does not install services."""
import ast
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
root = Path(__file__).resolve().parent.parent
manifest = json.loads((root / 'installer/sources.json').read_text())
for name, digest in manifest['files'].items():
    data = (root / name).read_bytes()
    assert hashlib.sha256(data).hexdigest() == digest, 'Hash mismatch: ' + name
    if name.endswith('.py') and not name.endswith('publisher-metadata.py'):
        ast.parse(data.decode(), filename=name)
    elif name.endswith('.js'):
        if not shutil.which('node'):
            raise SystemExit('Node.js is needed to validate JavaScript (not a host installation dependency)')
        subprocess.run(['node', '--check', '--input-type=module'], input=data, check=True)
    elif name.endswith('.sh'):
        subprocess.run(['bash', '-n', str(root / name)], check=True)
subprocess.run(['bash', '-n', str(root / 'install.sh')], check=True)
launcher = (root / 'install.sh').read_text().split("<<'BOOTSTRAP_PY'\n", 1)[1].rsplit('\nBOOTSTRAP_PY', 1)[0]
ast.parse(launcher)
print('OK: manifest and syntax; no deployment or real call was performed.')
