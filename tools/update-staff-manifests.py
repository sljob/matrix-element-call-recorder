#!/usr/bin/env python3
"""Rebuild standalone and checksums in local working tree; never installs."""
import base64,hashlib,io,json,textwrap,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
ADDON=ROOT/'addons/staff-directory'
runtime=['install.sh','provision.py','staff_directory.py','server_support.py','credentials.py','collector.py','device_snapshot.py','device_origin.py','docker_adapter.py','disable-addon.sh']
buf=io.BytesIO()
with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
    for name in runtime:
        info=zipfile.ZipInfo(name,date_time=(2026,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
        z.writestr(info,(ADDON/name).read_bytes())
raw=buf.getvalue()
script='''#!/bin/bash
set -euo pipefail
umask 077
[ "$(id -u)" -eq 0 ] || { echo 'Run as root'; exit 1; }
DEST="$(mktemp -d /root/staff-addon.XXXXXX)"
base64 -d > "$DEST/addon.zip" <<'PAYLOAD'
'''+ '\n'.join(textwrap.wrap(base64.b64encode(raw).decode(),76))+'''
PAYLOAD
python3 - "$DEST" <<'PY'
from pathlib import Path
import hashlib,zipfile,sys
root=Path(sys.argv[1]);raw=(root/'addon.zip').read_bytes()
assert hashlib.sha256(raw).hexdigest()=='DIGEST'
with zipfile.ZipFile(root/'addon.zip') as z:
 for info in z.infolist():
  p=Path(info.filename);assert len(p.parts)==1 and not p.is_absolute()
  (root/p).write_bytes(z.read(info))
PY
bash "$DEST/install.sh"
'''.replace('DIGEST',hashlib.sha256(raw).hexdigest())
(ADDON/'standalone.sh').write_text(script)
mfile=ROOT/'installer/sources.json';manifest=json.loads(mfile.read_text())
for path in ADDON.iterdir():
    if path.is_file() and path.name!='standalone.sh':manifest['files'][path.relative_to(ROOT).as_posix()]=''
for name in list(manifest['files']):manifest['files'][name]=hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
mfile.write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n')
paths=set()
for line in (ROOT/'SHA256SUMS').read_text().splitlines():
    if '  ' in line:paths.add(line.split('  ',1)[1])
paths.update(p.relative_to(ROOT).as_posix() for p in ADDON.rglob('*') if p.is_file() and '__pycache__' not in p.parts)
paths.update(['tools/update-staff-manifests.py','tools/validate-staff-release.py'])
(ROOT/'SHA256SUMS').write_text(''.join(hashlib.sha256((ROOT/name).read_bytes()).hexdigest()+'  '+name+'\n' for name in sorted(paths) if (ROOT/name).is_file()))
print('Standalone rebuilt; sources.json and SHA256SUMS updated. No installation performed.')
