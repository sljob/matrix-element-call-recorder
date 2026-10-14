#!/usr/bin/env python3
import hashlib
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
manifest=json.loads((ROOT/'installer/sources.json').read_text())
assert len(manifest['files'])<=100
for name,digest in manifest['files'].items():
    assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest,name
for line in (ROOT/'SHA256SUMS').read_text().splitlines():
    digest,name=line.split('  ',1)
    assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest,name
for path in (ROOT/'addons/staff-directory').glob('*.py'):
    compile(path.read_text(),str(path),'exec')
s=(ROOT/'install.sh').read_text();py=s.split("<<'BOOTSTRAP_PY'\n",1)[1].split('\nBOOTSTRAP_PY',1)[0]
compile(py,'bootstrap','exec')
assert 'STAFF_DIRECTORY_ENABLED=false' in (ROOT/'answers.example').read_text()
assert 'addons/staff-directory/answers_bridge.py' in s
print('PASS: manifests, addon Python, bootstrap Python and selection hook. No live deployment performed.')
