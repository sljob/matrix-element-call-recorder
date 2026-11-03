#!/usr/bin/env python3
"""Explicit Nginx installer. No Docker autodiscovery, profile migration or room power changes."""
import argparse
import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
VERSION = '3.0.0-rc3'
DOCKER_BINARY = '/usr/bin/docker'


class CommandError(RuntimeError):
    def __init__(self,args,result):
        self.stdout=result.stdout or ''
        self.stderr=result.stderr or ''
        stage=re.findall(r'RECDEPS_FAIL:([A-Za-z0-9:_-]+)',self.stdout+' '+self.stderr)
        label=' '.join(args[:2])
        super().__init__(label+' failed, exit='+str(result.returncode)+(('; stage='+stage[-1]) if stage else '')+'; raw output withheld to protect secrets')

def run(args, timeout=120):
    args=list(args)
    if args and args[0]=='docker':args[0]=DOCKER_BINARY
    p=subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    if p.returncode:raise CommandError(args,p)
    return p.stdout + (p.stderr if args[:2]==['nginx','-T'] else '')


def read_config(path):
    cfg = json.loads(Path(path).read_text())
    required = ['domain','synapse_url','element_call_url','livekit_internal','recorder_image',
                'bot_user','bot_password','device_id','profile_dir','element_config','nginx_server_config']
    for key in required:
        if not cfg.get(key): raise ValueError('Missing setting: ' + key)
    defaults = dict(plugin_dir='/opt/matrix-recorder-nginx', project='matrix-recorder-nginx',
                    docker_binary='/usr/bin/docker',backend_image='python:3.11-slim-bookworm', worker_port=8789, api_port=8765,
                    chromium_path='/usr/bin/chromium', ca_file='', spki_hash='', host_resolver='',
                    recording_min_power=50, recordings_min_power=50)
    cfg = dict(defaults, **cfg)
    if not re.fullmatch(r'[A-Za-z0-9.-]+',cfg['domain']): raise ValueError('Invalid domain')
    if not cfg['bot_user'].startswith('@') or ':' not in cfg['bot_user']: raise ValueError('Use full bot MXID')
    if not re.fullmatch(r'[A-Za-z0-9_-]+',cfg['device_id']): raise ValueError('Invalid device_id')
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]+',cfg['project']): raise ValueError('Invalid project name')
    for key in ['plugin_dir','profile_dir','element_config','nginx_server_config']:
        cfg[key]=str(Path(cfg[key]).resolve())
        if not re.fullmatch(r'/[A-Za-z0-9_./-]+',cfg[key]): raise ValueError('Unsafe path '+key)
    protected={'/','/etc','/root','/opt','/var','/var/lib','/usr'}
    if cfg['plugin_dir'] in protected or cfg['profile_dir'] in protected:raise ValueError('Choose dedicated plugin/profile directories')
    if cfg['element_config']==cfg['nginx_server_config']:raise ValueError('Element config and Nginx config must differ')
    if cfg['plugin_dir'] == cfg['profile_dir'] or Path(cfg['profile_dir']).is_relative_to(Path(cfg['plugin_dir'])/'current'):
        raise ValueError('Profile must be outside immutable release directory')
    for key in ['synapse_url','element_call_url']:
        u=urllib.parse.urlsplit(cfg[key])
        if u.scheme not in ('http','https') or not u.hostname or u.username or u.password: raise ValueError('Invalid '+key)
        if cfg[key].endswith('/'): cfg[key]=cfg[key].rstrip('/')
    for key in ['api_port','worker_port']:
        if type(cfg[key]) is not int or not 1024<=cfg[key]<=65535: raise ValueError('Invalid port')
    if cfg['api_port']==cfg['worker_port']: raise ValueError('Ports must differ')
    if not Path(cfg['docker_binary']).is_absolute():raise ValueError('docker_binary must be an absolute real CLI path')
    return cfg


def verify_sources():
    manifest=json.loads((ROOT/'vendor/PROVENANCE.json').read_text())
    for name,sha in manifest['files'].items():
        if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=sha: raise ValueError('Etalon checksum mismatch: '+name)


def adapt_observer(cfg):
    verify_sources()
    code=(ROOT/'vendor/monolith/p63-observer.js').read_text()
    old="executablePath:'/usr/bin/chromium'"
    if code.count(old)!=1: raise ValueError('Unexpected etalon executable path')
    code=code.replace(old,"executablePath:process.env.CHROME_PATH || '/usr/bin/chromium'")
    old="args:['--unsafely"
    if code.count(old)!=1: raise ValueError('Unexpected etalon arguments')
    code=code.replace(old,"args:[...(process.env.SPKI_HASH ? ['--ignore-certificate-errors-spki-list='+process.env.SPKI_HASH] : []), ...(process.env.HOST_RESOLVER ? ['--host-resolver-rules='+process.env.HOST_RESOLVER] : []), '--unsafely")
    origin=urllib.parse.urlsplit(cfg['element_call_url'])
    secure_origin=origin.scheme+'://'+origin.netloc
    code=code.replace('--unsafely-treat-insecure-origin-as-secure=http://127.0.0.1:8090,http://call:8080',
                      '--unsafely-treat-insecure-origin-as-secure='+secure_origin)
    # No changes to login, localStorage, Matrix SDK, room join, media gating or FFmpeg.
    return code


def nginx_snippet(cfg):
    port=cfg['api_port']
    return f'''# recorder-nginx {VERSION}; include inside the existing TLS server block.
location = /recording {{ return 308 /recording/; }}
location /recording/ {{
    proxy_pass http://127.0.0.1:{port}/;
    proxy_set_header Authorization $http_authorization;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 120s;
    proxy_buffering off;
    client_max_body_size 16k;
}}
location = /recordings {{ return 308 /recordings/; }}
location /recordings/ {{
    proxy_pass http://127.0.0.1:{port};
    proxy_set_header Host $host;
    proxy_set_header Authorization $http_authorization;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 120s;
    proxy_buffering off;
    client_max_body_size 16k;
}}
location /modules/recorder-nginx/ {{
    alias {cfg['plugin_dir']}/current/ui/;
    default_type application/javascript;
    add_header Cache-Control "no-cache";
}}
'''


# Importable via CLI and importlib-based tests without relying on cwd.
import importlib.util as _util
_spec=_util.spec_from_file_location('recorder_nginx_config',ROOT/'installer/nginx_config.py')
_nginx=_util.module_from_spec(_spec);_spec.loader.exec_module(_nginx)
def inject_nginx(text,domain,snippet):
    return _nginx.inject(text,domain,snippet)

def verify_deployment(cfg, release):
    """No success on a mock API, wrong vhost, stale module or unserved config."""
    context=ssl.create_default_context(cafile=cfg.get('ca_file') or None)
    base='https://'+cfg['domain']
    def get(path,headers=None):
        request=urllib.request.Request(base+path,headers=headers or {})
        with urllib.request.urlopen(request,context=context,timeout=15) as response:
            return response.read()
    expected=(release/'ui/index.js').read_bytes()
    if get('/modules/recorder-nginx/index.js')!=expected:
        raise ValueError('Live Nginx module differs from installed ui/index.js; wrong vhost, alias, permissions or cache')
    config=json.loads(get('/config.json'))
    if '/modules/recorder-nginx/index.js' not in config.get('modules',[]):
        raise ValueError('Live Element /config.json does not contain the module; element_config is not authoritative')
    worker=json.loads(urllib.request.urlopen('http://127.0.0.1:'+str(cfg['worker_port'])+'/health',timeout=10).read())
    health=json.loads(get('/recording/health'))
    if health.get('service')!='matrix-recorder-nginx-backend' or worker.get('service')!='matrix-recorder-nginx-worker':
        raise ValueError('Unexpected worker/backend identity: refuse mock or unrelated services')
    if not health.get('ok') or not health.get('worker',{}).get('ok'):
        raise ValueError('Worker/backend not ready')
    # TLS verification is mandatory. This is route/runtime readiness, NOT an E2EE media test.
    print('OK live HTTPS module bytes, Element config and recorder backend/worker identity')


def compose_config(cfg, release, token):
    root=Path(cfg['plugin_dir']); ca=cfg['ca_file']
    common={'DOMAIN':cfg['domain'],'RECORDER_TOKEN':token}
    worker_env=dict(common, PORT=str(cfg['worker_port']), REC_USER=cfg['bot_user'],REC_PASS=cfg['bot_password'],
        REC_DEVICE_ID=cfg['device_id'],SYNAPSE_URL=cfg['synapse_url'],MATRIX_HS='https://'+cfg['domain'],
        EC_URL=cfg['element_call_url'],LK_INTERNAL=cfg['livekit_internal'],OUT_DIR='/out',
        PULSE_SRC='recsink.monitor',RECORD_SECS='7200',WAIT_VID='120',REQUIRE_REMOTE='0',
        CHROME_PATH=cfg['chromium_path'],SPKI_HASH=cfg['spki_hash'],HOST_RESOLVER=cfg['host_resolver'])
    backend_env=dict(common,PORT=str(cfg['api_port']),STATE_DIR='/state',PUBLIC_DIR='/public',
        SYNAPSE_URL=cfg['synapse_url'],WORKER_URL='http://127.0.0.1:'+str(cfg['worker_port']),
        RECORDER_USER=cfg['bot_user'],RECORDING_MIN_POWER_LEVEL=str(cfg['recording_min_power']),
        RECORDINGS_MIN_POWER_LEVEL=str(cfg['recordings_min_power']))
    def mount(source,target,ro=False):return {'type':'bind','source':str(source),'target':target,'read_only':ro}
    wvol=[mount(release/'runtime','/plugin',True),mount(release/'observer.nginx.mjs','/app/observer.nginx.mjs',True),
          mount(cfg['profile_dir'],'/work/profile-spk'),mount(root/'output','/out'),mount(root/'public','/public')]
    bvol=[mount(release/'runtime','/plugin',True),mount(root/'state','/state'),mount(root/'public','/public',True)]
    if ca:
        copied=root/'ca/ca.crt'
        wvol.append(mount(copied,'/site-ca.crt',True));bvol.append(mount(copied,'/site-ca.crt',True))
        worker_env['NODE_EXTRA_CA_CERTS']='/site-ca.crt';backend_env['TLS_CA_FILE']='/site-ca.crt'
    logging={'driver':'json-file','options':{'max-size':'10m','max-file':'3'}}
    return {'services':{
        'worker':{'image':cfg['recorder_image'],'network_mode':'host','init':True,'restart':'unless-stopped',
            'entrypoint':['/bin/bash','/plugin/entrypoint.sh'],'environment':worker_env,'volumes':wvol,
            'shm_size':'1gb','stop_grace_period':'120s','logging':logging,
            'healthcheck':{'test':['CMD','node','-e',f"fetch('http://127.0.0.1:{cfg['worker_port']}/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"], 'interval':'10s','timeout':'5s','retries':6,'start_period':'30s'}},
        'backend':{'image':cfg['backend_image'],'network_mode':'host','restart':'unless-stopped',
            'entrypoint':['python3','/plugin/backend.py'],'environment':backend_env,'volumes':bvol,
            'depends_on':{'worker':{'condition':'service_healthy'}},'logging':logging,
            'healthcheck':{'test':['CMD','python3','-c',f"import urllib.request;urllib.request.urlopen('http://127.0.0.1:{cfg['api_port']}/health',timeout=4)"],'interval':'10s','timeout':'5s','retries':6}}}}


def check(cfg,repair,allow_create=False):
    global DOCKER_BINARY
    DOCKER_BINARY=cfg.get('docker_binary','/usr/bin/docker')
    verify_sources()
    # Same implementation for standalone preflight, --check, --install and --repair.
    import importlib.util
    spec=importlib.util.spec_from_file_location('recorder_preflight',ROOT/'installer/preflight.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.Preflight(cfg,run,inject_nginx,repair=repair).run_all()
    if repair:
        with urllib.request.urlopen('http://127.0.0.1:'+str(cfg['worker_port'])+'/health',timeout=10) as response:health=json.load(response)
        if health.get('busy') is not False:raise ValueError('Worker busy: finish recording first')
    context=ssl.create_default_context(cafile=cfg['ca_file'] or None)
    # A temporary separate device never replaces the persistent recorder device.
    def request(path,body,token=None):
        req=urllib.request.Request(cfg['synapse_url']+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json',**({'Authorization':'Bearer '+token} if token else {})})
        with urllib.request.urlopen(req,context=context,timeout=20) as response:return json.load(response)
    login_body={'type':'m.login.password','identifier':{'type':'m.id.user','user':cfg['bot_user']},'password':cfg['bot_password'],'device_id':'PREFLIGHT_'+secrets.token_hex(8)}
    try:auth=request('/_matrix/client/v3/login',login_body)
    except urllib.error.HTTPError as error:
        if not allow_create or error.code!=403 or not cfg.get('bot_registration_secret_file'):raise ValueError('Bot login failed. Set valid credentials; optional shared-secret bot creation is supported only by --install.') from None
        secret=Path(cfg['bot_registration_secret_file']).read_text().strip().encode()
        register_url=cfg['synapse_url']+'/_synapse/admin/v1/register'
        with urllib.request.urlopen(register_url,context=context,timeout=20) as response:nonce=json.load(response)['nonce']
        username=cfg['bot_user'][1:].split(':',1)[0]
        message=(nonce+'\x00'+username+'\x00'+cfg['bot_password']+'\x00notadmin').encode()
        mac=hmac.new(secret,message,hashlib.sha1).hexdigest()
        try:request('/_synapse/admin/v1/register',{'nonce':nonce,'username':username,'password':cfg['bot_password'],'admin':False,'mac':mac})
        except Exception:raise ValueError('Bot registration failed; existing account passwords are never reset') from None
        print('Created non-admin Matrix bot:',cfg['bot_user'])
        auth=request('/_matrix/client/v3/login',login_body)
    try:
        if auth.get('user_id')!=cfg['bot_user']:raise ValueError('Bot MXID differs from login identity')
    finally:request('/_matrix/client/v3/logout',{},auth['access_token'])
    print('OK bot login (temporary device logged out); permanent device unchanged')
    print('OK configuration, source checksums, Nginx syntax, dependencies, identity and ports')


def install(cfg,repair):
    check(cfg,repair,allow_create=not repair)
    root=Path(cfg['plugin_dir']);root.mkdir(parents=True,exist_ok=True);root.chmod(0o755)
    for directory in ('releases','backups','output','public','state','nginx','ca'):
        (root/directory).mkdir(exist_ok=True)
    (root/'state').chmod(0o700)
    (root/'releases').chmod(0o755)
    profile=Path(cfg['profile_dir']);profile.mkdir(parents=True,exist_ok=True)
    # Fresh profile is valid. No Default-directory assumption, no profile copying.
    stamp=datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backup=root/'backups'/stamp;backup.mkdir(mode=0o700)
    element=Path(cfg['element_config']);nginx=Path(cfg['nginx_server_config']);compose=root/'compose.json'
    snapshots={p:p.read_bytes() if p.exists() else None for p in (element,nginx,compose,root/'nginx/recorder.conf',root/'identity.json',root/'ca/ca.crt')}
    for index,(path,data) in enumerate(snapshots.items()):
        if data is not None:(backup/str(index)).write_bytes(data)
    (backup/'paths.json').write_text(json.dumps([str(p) for p in snapshots]))
    release=root/'releases'/stamp;release.mkdir(mode=0o755);release.chmod(0o755)
    shutil.copytree(ROOT/'runtime',release/'runtime');shutil.copytree(ROOT/'ui',release/'ui')
    (release/'observer.nginx.mjs').write_text(adapt_observer(cfg))
    # Nginx traverses these directories to serve JS; secrets are stored elsewhere.
    for p in release.rglob('*'):
        p.chmod(0o755 if p.is_dir() else 0o644)
    token_file=root/'state/worker-token'
    if not token_file.exists():token_file.write_text(secrets.token_urlsafe(48));token_file.chmod(0o600)
    token=token_file.read_text().strip()
    old_current=os.readlink(root/'current') if (root/'current').is_symlink() else None
    (backup/'rollback.json').write_text(json.dumps({'old_current':old_current,'worker_port':cfg['worker_port'],'plugin_dir':cfg['plugin_dir'],'project':cfg['project'],'docker_binary':cfg.get('docker_binary','/usr/bin/docker')}))
    dc=['docker','compose','-p',cfg['project'],'-f',str(compose)]
    try:
        if cfg['ca_file']:shutil.copy2(cfg['ca_file'],root/'ca/ca.crt')
        # Both files live on host; source paths remain stable across container recreation.
        compose.write_text(json.dumps(compose_config(cfg,release,token),indent=2));compose.chmod(0o600)
        run(dc+['config','--quiet'])
        run(dc+['up','-d','--no-build','--pull','never'],timeout=180)
        ready=False
        for _ in range(30):
            try:
                with urllib.request.urlopen('http://127.0.0.1:'+str(cfg['api_port'])+'/health',timeout=4) as response:
                    ready=json.load(response).get('ok') is True
                if ready:break
            except Exception:pass
            time.sleep(2)
        if not ready:raise RuntimeError('Backend/worker health failed')
        tmp=root/'current.new';tmp.symlink_to(release);os.replace(tmp,root/'current')
        data=json.loads(element.read_text());modules=data.setdefault('modules',[])
        module='/modules/recorder-nginx/index.js'
        if module not in modules:modules.append(module)
        element.write_text(json.dumps(data,ensure_ascii=False,indent=2))
        (root/'nginx/recorder.conf').write_text(nginx_snippet(cfg))
        nginx.write_text(inject_nginx(nginx.read_text(),cfg['domain'],root/'nginx/recorder.conf'))
        run(['nginx','-t']);run(['nginx','-s','reload'])
        verify_deployment(cfg,release)
        (root/'identity.json').write_text(json.dumps({k:cfg[k] for k in ('bot_user','device_id','profile_dir')}));(root/'identity.json').chmod(0o600)
        print('Installed',VERSION,'backup:',backup)
        print('Matrix bot login verified in preflight; no power levels modified.')
        print('Reload Element. End-to-end camera/audio recording must be checked before production acceptance.')
    except Exception:
        for path,data in snapshots.items():
            if data is None:path.unlink(missing_ok=True)
            else:path.write_bytes(data)
        if (root/'current').is_symlink():(root/'current').unlink()
        if old_current:(root/'current').symlink_to(old_current)
        if snapshots[compose] is not None:
            try:run(dc+['up','-d','--no-build','--pull','never'])
            except Exception:print('Rollback runtime needs operator; backup:',backup,file=sys.stderr)
        else:
            for service in ('backend','worker'):
                # remove only containers bearing our exact project/service labels
                ids=run(['docker','ps','-aq','--filter','label=com.docker.compose.project='+cfg['project'],'--filter','label=com.docker.compose.service='+service]).splitlines()
                if ids:run(['docker','rm','-f',*ids])
        try:run(['nginx','-t']);run(['nginx','-s','reload'])
        except Exception:print('Nginx rollback check failed; backup:',backup,file=sys.stderr)
        raise


def main():
    parser=argparse.ArgumentParser()
    group=parser.add_mutually_exclusive_group(required=True)
    for mode in ('check','install','repair','render'):group.add_argument('--'+mode,action='store_true')
    parser.add_argument('answers');parser.add_argument('--output',default='rendered')
    args=parser.parse_args();cfg=read_config(args.answers)
    if args.render:
        out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
        (out/'observer.nginx.mjs').write_text(adapt_observer(cfg))
        (out/'recorder.conf').write_text(nginx_snippet(cfg));print('Rendered source and Nginx snippet:',out);return
    if os.geteuid()!=0:raise SystemExit('Run as root')
    if args.check:check(cfg,(Path(cfg['plugin_dir'])/'compose.json').exists())
    else:install(cfg,args.repair)


if __name__=='__main__':
    try:main()
    except Exception as error:
        print('ERROR:',str(error),file=sys.stderr);sys.exit(1)
