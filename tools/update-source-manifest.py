#!/usr/bin/env python3
"""Refresh hashes for existing manifest entries after reviewed source changes."""
import hashlib
import json
from pathlib import Path
root = Path(__file__).resolve().parent.parent
path = root / 'installer/sources.json'
manifest = json.loads(path.read_text())
for name in manifest['files']:
    target = (root / name).resolve()
    if not target.is_relative_to(root):
        raise SystemExit('Unsafe manifest path')
    manifest['files'][name] = hashlib.sha256(target.read_bytes()).hexdigest()
path.write_text(json.dumps(manifest, indent=2) + '\n')
print('Updated installer/sources.json; commit the manifest together with sources.')
