#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
python3 - <<'PY'
import ast,hashlib,json,pathlib,subprocess
root=pathlib.Path('.')
manifest=json.loads((root/'vendor/PROVENANCE.json').read_text())
for name,expected in manifest['files'].items():
    assert hashlib.sha256((root/name).read_bytes()).hexdigest()==expected,name
for p in root.rglob('*.py'):ast.parse(p.read_text(),filename=str(p))
for p in [root/'runtime/worker.mjs',root/'ui/index.js',root/'vendor/monolith/p63-observer.js']:
    subprocess.run(['node','--check',str(p)],check=True)
for p in [root/'install.sh',root/'runtime/entrypoint.sh']:
    subprocess.run(['bash','-n',str(p)],check=True)
print('OK provenance, Python/JS/Bash syntax')
PY
python3 -m unittest discover -s tests -v

node tests/test_ui.mjs
