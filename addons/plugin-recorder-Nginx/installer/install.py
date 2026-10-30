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
VERSION = '3.0.0-rc1'


def run(args, timeout=120):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError(args[0] + ' failed (output suppressed: may contain credentials)')
    return p.stdout


def read_config(path):
    cfg = json.loads(Path(path).read_text())
    required = ['domain','synapse_url','element_call_url','livekit_internal','recorder_image',
                'bot_user','bot_password','device_id','profile_dir','element_config','nginx_server_config']
    for key in required:
        if not cfg.get(key): raise ValueError('Missing setting: ' + key)
    defaults = dict(plugin_dir='/opt/matrix-recorder-nginx', project='matrix-recorder-nginx',
                    backend_image='python:3.11-slim-bookworm', worker_port=8789, api_port=8765,
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


def inject_nginx(text,domain,snippet):
    marker='# BEGIN MATRIX RECORDER NGINX'
    if marker in text:
        text=re.sub(r'\n[ \t]*# BEGIN MATRIX RECORDER NGINX\n.*?^[ \t]*# END MATRIX RECORDER NGINX\n?', '', text, flags=re.S | re.M)
    # Explicit server-name matching and brace tracking. Reject ambiguity.
    tokens=[]
    quote=None;escaped=False;comment=False;stack=[];servers=[]
    for i,char in enumerate(text):
        if comment:
            if char=='\n': comment=False
            continue
        if quote:
            if escaped: escaped=False
            elif char=='\\': escaped=True
            elif char==quote: quote=None
            continue
        if char=='#': comment=True;continue
        if char in ('"',"'"): quote=char;continue
        if char=='{':
            prefix=text[max(0,i-80):i]
            stack.append((i,bool(re.search(r'\bserver\s*$',prefix))))
        if char=='}':
            if not stack: raise ValueError('Unbalanced nginx braces')
            start,is_server=stack.pop()
            if is_server:
                block=text[start:i]
                # Server names in nested contexts are invalid nginx anyway; nginx -t follows.
                names=re.findall(r'\bserver_name\s+([^;]+);',re.sub(r'#[^\n]*','',block))
                if any(domain in n.split() for n in names) and re.search(r'\blisten\s+[^;]*\bssl\b',block):
                    servers.append(i)
    if len(servers)!=1: raise ValueError('Expected one TLS server with exact server_name '+domain)
    index=servers[0]
    for location in ('/recording','/recordings','/modules/recorder-nginx'):
        if re.search(r'location\s+(?:[=~^*]+\s+)?'+re.escape(location)+r'(?:/|\s|\{)',text):
            raise ValueError('Existing conflicting Nginx location '+location)
    insert='\n    '+marker+'\n    include '+str(snippet)+';\n    # END MATRIX RECORDER NGINX\n'
    return text[:index]+insert+text[index:]


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
    verify_sources()
    for tool in ('docker','nginx'): run([tool,'--version'] if tool=='docker' else [tool,'-v'])
    run(['docker','compose','version'])
    if not Path(cfg['element_config']).is_file():raise ValueError('element_config not found')
    json.loads(Path(cfg['element_config']).read_text())
    nginx=Path(cfg['nginx_server_config']);text=nginx.read_text()
    inject_nginx(text,cfg['domain'],Path(cfg['plugin_dir'])/'nginx/recorder.conf')
    run(['nginx','-t'])
    if cfg['ca_file']:
        cfg['ca_file']=str(Path(cfg['ca_file']).resolve());ssl.create_default_context(cafile=cfg['ca_file'])
        if not cfg['spki_hash']:raise ValueError('Private CA requires explicit Chromium spki_hash (leaf SPKI SHA256).')
    if cfg['spki_hash']:
        import base64
        if len(base64.b64decode(cfg['spki_hash'],validate=True))!=32:raise ValueError('SPKI must be SHA256 base64')
    for image in (cfg['recorder_image'],cfg['backend_image']):
        run(['docker','image','inspect',image])
    # Verify the existing recorder image has monolith dependencies. No build/pull.
    run(['docker','run','--rm','--network','none','--entrypoint','/bin/bash',cfg['recorder_image'],'-c',
         'set -e; command -v node; command -v python3; command -v ffmpeg; command -v ffprobe; command -v Xvfb; command -v xdpyinfo; command -v pactl; command -v pulseaudio; test -x "$1"; node -e "if(Number(process.versions.node.split(\'.\')[0])<20)process.exit(1)"; ffmpeg -hide_banner -h full 2>&1 | grep -q -- -fps_mode; ffmpeg -hide_banner -encoders 2>&1 | grep -q libx264; ffmpeg -hide_banner -encoders 2>&1 | grep -q libmp3lame; cd /app; node --input-type=module -e "await import(\'puppeteer-core\')"','check',cfg['chromium_path']])
    for image in (cfg['recorder_image'],cfg['backend_image']): print('OK existing image:',image)
    live=run(['docker','ps','-q']).splitlines()
    if live:
        infos=json.loads(run(['docker','inspect',*live]))
        for i in infos:
            own=i['Config'].get('Labels',{}).get('com.docker.compose.project')==cfg['project']
            if own:continue
            other_env=dict(v.split('=',1) for v in i['Config'].get('Env',[]) if '=' in v)
            other_user=other_env.get('REC_USER') or other_env.get('RECORDER_USER') or ''
            if other_user and other_user.lstrip('@').split(':',1)[0]==cfg['bot_user'][1:].split(':',1)[0]:raise ValueError('Bot account is used by another running recorder '+i['Name'])
            if any(Path(m.get('Source','/')).resolve()==Path(cfg['profile_dir']) for m in i.get('Mounts',[])):
                raise ValueError('Profile used by running container '+i['Name'])
    root=Path(cfg['plugin_dir']); saved=root/'identity.json'
    identity={k:cfg[k] for k in ('bot_user','device_id','profile_dir')}
    if saved.exists() and json.loads(saved.read_text())!=identity:raise ValueError('Repair cannot change bot/device/profile identity')
    if repair:
        with urllib.request.urlopen('http://127.0.0.1:'+str(cfg['worker_port'])+'/health',timeout=10) as r:h=json.load(r)
        if h.get('busy') is not False:raise ValueError('Worker busy: finish recording first')
    else:
        if (root/'compose.json').exists():raise ValueError('Already installed; use --repair')
        for port in (cfg['worker_port'],cfg['api_port']):
            with socket.socket() as s:s.bind(('127.0.0.1',port))
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
    release=root/'releases'/stamp;release.mkdir(mode=0o755)
    shutil.copytree(ROOT/'runtime',release/'runtime');shutil.copytree(ROOT/'ui',release/'ui')
    (release/'observer.nginx.mjs').write_text(adapt_observer(cfg))
    # Nginx traverses these directories to serve JS; secrets are stored elsewhere.
    for p in release.rglob('*'):
        p.chmod(0o755 if p.is_dir() else 0o644)
    token_file=root/'state/worker-token'
    if not token_file.exists():token_file.write_text(secrets.token_urlsafe(48));token_file.chmod(0o600)
    token=token_file.read_text().strip()
    old_current=os.readlink(root/'current') if (root/'current').is_symlink() else None
    (backup/'rollback.json').write_text(json.dumps({'old_current':old_current,'worker_port':cfg['worker_port'],'plugin_dir':cfg['plugin_dir'],'project':cfg['project']}))
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
