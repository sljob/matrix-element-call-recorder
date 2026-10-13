#!/usr/bin/env python3
"""Optional root-only adapter for lk-jwt-service 0.5.x Docker logs."""
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import time
import sys
from staff_directory import atomic

PATTERN = re.compile(r'for full-access Matrix user (@[^\s]+) \(LiveKit identity: ([^\s)]+)\)')

def docker(*args):
    p = subprocess.run(['docker', *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if p.returncode:
        raise RuntimeError('Docker command failed')
    return p.stdout.decode(errors='replace'), p.stderr.decode(errors='replace')

def extract(text):
    return {identity: uid for uid, identity in PATTERN.findall(text)}

def main():
    cfg = json.loads(Path(sys.argv[1]).read_text())
    dest = Path(cfg['output'])
    data = json.loads(dest.read_text()) if dest.exists() else {'identities': {}}
    since = '168h'
    while True:
        try:
            started = int(time.time())
            info = json.loads(docker('inspect', cfg['livekit_container'])[0])[0]
            ports = info['NetworkSettings'].get('Ports', {}).get('7880/tcp') or []
            local = next((p for p in ports if p['HostIp'] in ('127.0.0.1', '0.0.0.0')), None)
            if local:
                endpoint = 'http://127.0.0.1:' + str(int(local['HostPort']))
            else:
                nets = info['NetworkSettings']['Networks']
                addresses = sorted(n['IPAddress'] for n in nets.values() if n.get('IPAddress'))
                address = str(ipaddress.IPv4Address(addresses[0]))
                endpoint = 'http://' + address + ':7880'
            out, err = docker('logs', '--since', since, '--tail', '50000', cfg['jwt_container'])
            data['identities'].update(extract(out + '\n' + err))
            data['livekit_url'], data['updated_at'] = endpoint, time.time()
            atomic(dest, data)
            since = str(started - 10)
        except Exception:
            print('Adapter update failed; last snapshot will expire.', flush=True)
        time.sleep(5)

if __name__ == '__main__':
    main()
