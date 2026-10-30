#!/usr/bin/env python3
"""Restore installer-managed configuration files. Never deletes profiles/recordings."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import urllib.request


def run(args):
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('backup', help='plugin_dir/backups/<timestamp>')
    args=p.parse_args()
    if os.geteuid()!=0:raise SystemExit('Run as root')
    backup=Path(args.backup).resolve()
    manifest=json.loads((backup/'rollback.json').read_text())
    with urllib.request.urlopen('http://127.0.0.1:'+str(manifest['worker_port'])+'/health',timeout=10) as response:
        if json.load(response).get('busy') is not False:raise SystemExit('Finish recording before rollback')
    paths=json.loads((backup/'paths.json').read_text())
    # Keep a rollback-of-rollback before touching files.
    emergency=backup/'before-manual-rollback'
    emergency.mkdir(mode=0o700)
    for i,path in enumerate(paths):
        path=Path(path)
        if path.exists():(emergency/str(i)).write_bytes(path.read_bytes())
        old=backup/str(i)
        if old.exists():path.write_bytes(old.read_bytes())
        else:path.unlink(missing_ok=True)
    current=Path(manifest['plugin_dir'])/'current'
    if current.is_symlink():current.unlink()
    if manifest['old_current']:current.symlink_to(manifest['old_current'])
    compose=Path(manifest['plugin_dir'])/'compose.json'
    if compose.exists():run(['docker','compose','-p',manifest['project'],'-f',str(compose),'up','-d','--no-build','--pull','never'])
    else:
        for service in ('backend','worker'):
            result=subprocess.run(['docker','ps','-aq','--filter','label=com.docker.compose.project='+manifest['project'],'--filter','label=com.docker.compose.service='+service],check=True,capture_output=True,text=True)
            ids=result.stdout.splitlines()
            if ids:run(['docker','rm','-f',*ids])
    run(['nginx','-t']);run(['nginx','-s','reload'])
    print('Configuration restored. Profiles and recordings retained.')

if __name__=='__main__':main()
