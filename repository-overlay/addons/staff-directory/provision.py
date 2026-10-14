"""First installation addon only. No stack writes, container restarts or agent software."""
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
import signal

def interrupted(signum, frame):
    raise KeyboardInterrupt()

signal.signal(signal.SIGTERM, interrupted)
from urllib.parse import urlsplit
from staff_directory import API, APIError, MATRIX, atomic, enc, Directory
from credentials import publisher_config

ROOT=Path('/etc/matrix-staff-directory')
STATE=Path('/var/lib/matrix-staff-directory')
APP=Path('/opt/matrix-staff-directory')
UNITS=['matrix-staff-directory-adapter','matrix-staff-directory-collector','matrix-staff-directory']
SOURCE=Path(__file__).parent

def ask(label,default=''):
    with open('/dev/tty','w') as out,open('/dev/tty','r') as inp:
        out.write(label+(f' [{default}]' if default else '')+': ');out.flush()
        return inp.readline().strip() or default

def command(args,data=None):
    p=subprocess.run(args,input=data,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=120)
    if p.returncode:
        # Never emit subprocess output: curl, docker and credentials may include sensitive data.
        raise ValueError('Command failed: '+args[0]+' '+args[1])
    return p.stdout

def encrypt(name,value):
    payload=value.encode() if isinstance(value,str) else json.dumps(value).encode()
    blob=command(['systemd-creds','encrypt','--with-key=host','--name='+name,'-','-'],payload)
    path=ROOT/(name+'.cred');tmp=path.with_suffix('.tmp')
    fd=os.open(tmp,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
    with os.fdopen(fd,'wb') as f: f.write(blob);f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)

def decrypt(name):
    return command(['systemd-creds','decrypt','--name='+name,str(ROOT/(name+'.cred')),'-']).decode()

def docker(service,stack):
    ids=command(['docker','compose','-f',str(stack/'compose.json'),'ps','-q',service]).decode().split()
    if len(ids)!=1: raise ValueError('Expected one running container: '+service)
    data=json.loads(command(['docker','inspect',ids[0]]))[0]
    if not data['State']['Running']: raise ValueError('Not running: '+service)
    return ids[0],data

def health(domain,stack):
    command(['curl','--silent','--show-error','--fail','--noproxy','*','--connect-timeout','5','--max-time','20',
             '--cacert',str(stack/'certs/ca.crt'),'--resolve',domain+':443:127.0.0.1',
             'https://'+domain+'/_matrix/client/versions'])

def fingerprints(stack):
    return {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [stack/'compose.json',stack/'.installer-config-sha256'] if p.is_file()}

def save(progress): encrypt('progress',progress)

def systemd_probe():
    encrypt('probe','CREDENTIAL_PROBE_OK')
    command(['systemd-run','--quiet','--wait','--pipe','--collect','--unit=staff-credential-probe-'+secrets.token_hex(4),
             '-p','LoadCredentialEncrypted=probe:'+str(ROOT/'probe.cred'),'/usr/bin/python3','-c',
             'import os,pathlib; assert (pathlib.Path(os.environ["CREDENTIALS_DIRECTORY"])/"probe").read_text()=="CREDENTIAL_PROBE_OK"'])
    (ROOT/'probe.cred').unlink()

def unit(name,script,creds,extra=''):
    loads=''.join('LoadCredentialEncrypted='+c+':'+str(ROOT/(c+'.cred'))+'\n' for c in creds)
    content='''[Unit]
Description=Matrix Staff Directory addon
After=network-online.target docker.service
[Service]
Type=simple
User=root
UMask=0077
WorkingDirectory=/opt/matrix-staff-directory
ExecStart=/usr/bin/python3 /opt/matrix-staff-directory/'''+script+'''\nRestart=on-failure
RestartSec=15
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
ReadWritePaths=/var/lib/matrix-staff-directory
'''+loads+extra+'''[Install]
WantedBy=multi-user.target
'''
    path=Path('/etc/systemd/system')/(name+'.service')
    if path.exists(): raise ValueError('Refusing existing unit: '+name)
    path.write_text(content)
    return path

def verify():
    cfg=publisher_config(json.loads((ROOT/'config.json').read_text()))
    w=Directory(cfg)
    state=json.loads((STATE/'state.json').read_text())
    event=w.matrix.call('GET',w.prefix+'/event/'+enc(state['last_edit_event_id']))
    if event['sender']!=cfg['bot_user_id']: raise ValueError('Wrong publication sender')
    busy,ok,unknown=w.calls(w.matrix.call('GET',w.prefix+'/joined_members')['joined'])
    if not ok: raise ValueError('LiveKit not ready')
    data=json.loads((STATE/'devices.json').read_text())
    if not 0<=time.time()-data['checked_at']<=180: raise ValueError('Collector snapshot stale')
    if any(x['error'] for x in data['users'].values()): raise ValueError('Device collection errors')
    print('VERIFY: publisher readback, collector and LiveKit OK')
    print('Unknown LiveKit identities:',unknown)

if '--verify' in sys.argv:
    verify();sys.exit(0)

started=False
owned=[]
try:
    if os.geteuid()!=0: raise ValueError('Run as root')
    if sys.version_info<(3,10): raise ValueError('Python 3.10+ required')
    for tool in ['docker','curl','systemd-creds','systemd-run','systemctl','systemd-analyze']:
        if not shutil.which(tool): raise ValueError('Required command missing: '+tool+'; no packages installed automatically')
    if command(['ps','-p','1','-o','comm=']).decode().strip()!='systemd': raise ValueError('Systemd PID1 required')
    if int(command(['systemctl','--version']).decode().split()[1])<250: raise ValueError('systemd >=250 required for encrypted credentials')
    # Own only this new addon. Unknown existing installation is never overwritten.
    marker=ROOT/'cloud-install-marker'
    if any(p.exists() for p in [ROOT,STATE,APP]) and not marker.exists():
        raise ValueError('Existing addon paths found. Not a clean addon installation; no files overwritten')
    if (ROOT/'config.json').exists(): raise ValueError('Addon already configured; no repeated provisioning. Use health checks or resume documented in README')
    for name in UNITS:
        path=Path('/etc/systemd/system')/(name+'.service')
        if path.exists(): raise ValueError('Existing service unit: '+name)
    stack=Path(os.environ.get('STAFF_INSTALL_STACK') or ask('Существующий INSTALL_DIR (не меняется)','/opt/element-stack'))
    if not stack.is_absolute() or not (stack/'compose.json').is_file(): raise ValueError('Expected existing absolute INSTALL_DIR/compose.json')
    if not (stack/'certs/ca.crt').is_file(): raise ValueError('Existing certs/ca.crt required')
    domain=os.environ.get('STAFF_INSTALL_DOMAIN') or ask('DOMAIN сервера (без https://)')
    if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?',domain): raise ValueError('Invalid DOMAIN')
    health(domain,stack)
    before=fingerprints(stack)
    synapse_id,synapse=docker('synapse',stack)
    jwt_id,jwt=docker('jwt',stack)
    livekit_id,livekit=docker('livekit',stack)
    env=dict(x.split('=',1) for x in jwt['Config']['Env'] if '=' in x)
    key,secret=env.get('LIVEKIT_KEY'),env.get('LIVEKIT_SECRET')
    if not key or not secret: raise ValueError('Existing jwt lacks LIVEKIT_KEY/LIVEKIT_SECRET; no guessing')
    print('Проверены HTTPS и работающие Synapse/JWT/LiveKit. Стек не изменяется.')
    print('Будут созданы только новый бот, приватная комната Сотрудники и три службы дополнения.')
    print('Credentials шифруются ключом этого хоста (не TPM). Root сможет их расшифровать.')
    print('При отключении дополнения бот/комната останутся; это не транзакционный откат Matrix.')
    if ask('Для разрешения установки введите точный DOMAIN')!=domain: raise ValueError('Cancelled')
    for path in [ROOT,STATE,APP]: path.mkdir(mode=0o700,parents=True,exist_ok=True)
    marker.write_text('cloud-addon-v015\n')
    for path in [ROOT,STATE,APP]: os.chmod(path,0o700)
    atomic(ROOT/'local-https.json',{'hostname':domain,'port':443,'connect_ip':'127.0.0.1','legacy_ca':False})
    shutil.copyfile(stack/'certs/ca.crt',ROOT/'ca.crt');os.chmod(ROOT/'ca.crt',0o600)
    api=API('https://'+domain,ca=str(ROOT/'ca.crt'))
    try: api.call('GET','/_matrix/client/versions')
    except APIError as exc:
        # Explicit compatibility option only after strict verification failed and curl trust check passed.
        if exc.status!=0: raise
        print('Python TLS-проверка не прошла:',exc.code)
        if 'SSLCertVerificationError' not in exc.code or ask('Совместимый режим CA без STRICT, с проверкой имени/доверия? yes/no','no')!='yes': raise
        atomic(ROOT/'local-https.json',{'hostname':domain,'port':443,'connect_ip':'127.0.0.1','legacy_ca':True})
        api=API('https://'+domain,ca=str(ROOT/'ca.crt'));api.call('GET','/_matrix/client/versions')
    systemd_probe()  # prove actual credential delivery BEFORE creating accounts/rooms
    admin_token=getpass.getpass('Административный access token Cloud (скрыто, будет зашифрован): ').strip()
    if not admin_token: raise ValueError('Empty token')
    admin=API('https://'+domain,admin_token,str(ROOT/'ca.crt'))
    admin_id=admin.call('GET',MATRIX+'/account/whoami')['user_id']
    if admin_id.split(':',1)[1]!=domain: raise ValueError('Synapse server name and DOMAIN differ')
    if not admin.call('GET','/_synapse/admin/v1/users/'+enc(admin_id)+'/admin').get('admin'): raise ValueError('Admin token required')
    admin.call('GET','/_synapse/admin/v2/users/'+enc(admin_id)+'/devices')
    encrypt('admin',admin_token)
    encrypt('livekit',{'api_key':key,'api_secret':secret})
    progress=json.loads(decrypt('progress')) if (ROOT/'progress.cred').exists() else {'domain':domain,'stack':str(stack),'admin_id':admin_id,'bot_user_id':'@staff_directory_'+secrets.token_hex(6)+':'+domain,'password':secrets.token_urlsafe(48)}
    if progress['domain']!=domain or progress['stack']!=str(stack) or progress['admin_id']!=admin_id: raise ValueError('Resume requires same domain, stack and administrator')
    save(progress)
    if not progress.get('bot_token'):
        uid=progress['bot_user_id']
        try:
            admin.call('GET','/_synapse/admin/v2/users/'+enc(uid))
        except APIError as exc:
            if exc.status!=404: raise
            admin.call('PUT','/_synapse/admin/v2/users/'+enc(uid),{'password':progress['password'],'admin':False,'displayname':'Сотрудники · статусы'})
        # Existing random account is NEVER modified; login proves ownership on resume.
        login=api.call('POST',MATRIX+'/login',{'type':'m.login.password','identifier':{'type':'m.id.user','user':uid},'password':progress['password'],'initial_device_display_name':'Staff Directory Service'})
        progress['bot_token']=login['access_token'];progress.pop('password',None);save(progress)
    encrypt('bot',progress['bot_token'])
    bot=API('https://'+domain,progress['bot_token'],str(ROOT/'ca.crt'))
    if not progress.get('room_id'):
        # recover a room after create succeeded but local journal write failed
        rooms=bot.call('GET',MATRIX+'/joined_rooms')['joined_rooms']
        if len(rooms)>1: raise ValueError('Ambiguous existing bot rooms; manual inspection needed')
        if rooms:
            progress['room_id']=rooms[0]
        else:
            levels={'users':{progress['bot_user_id']:100,admin_id:99},'users_default':0,'events_default':100,'state_default':100,'invite':50,'kick':100,'ban':100,'redact':100,'notifications':{'room':100},'events':{'m.room.message':100,'m.reaction':100,'m.sticker':100,'m.room.encrypted':100,'m.room.encryption':100}}
            progress['room_id']=bot.call('POST',MATRIX+'/createRoom',{'name':'Сотрудники','visibility':'private','preset':'private_chat','invite':[admin_id],'power_level_content_override':levels,'initial_state':[{'type':'m.room.history_visibility','state_key':'','content':{'history_visibility':'shared'}}],'topic':'Справочник статусов; без E2EE. Бот не читает другие комнаты.'})['room_id']
        save(progress)
    # Join only the NEW room with administrator's explicit installation consent.
    admin.call('POST',MATRIX+'/join/'+enc(progress['room_id']),{})
    room_levels=bot.call('GET',MATRIX+'/rooms/'+enc(progress['room_id'])+'/state/m.room.power_levels/')
    if room_levels.get('users',{}).get(progress['bot_user_id'])!=100 or room_levels.get('events',{}).get('m.room.message')!=100:
        raise ValueError('Recovered room has old policy; not silently overwritten')
    if any(v>=100 for u,v in room_levels.get('users',{}).items() if u!=progress['bot_user_id']):
        raise ValueError('Only bot may have PL100')

    cfg={'homeserver':'https://'+domain,'bot_user_id':progress['bot_user_id'],'room_id':progress['room_id'],'state_dir':str(STATE),'ca_file':str(ROOT/'ca.crt'),'poll_seconds':5,'heartbeat_seconds':900,'max_members':300,'exclude_users':[],'recent_activity_rooms':[],'server_directory_version':'0.1.6-staff','livekit':{'url':'docker-adapter','adapter_file':str(STATE/'identities.json'),'identity_map':{}}}
    atomic(ROOT/'adapter.json',{'jwt_container':jwt_id,'livekit_container':livekit_id,'output':str(STATE/'identities.json')})
    # Store stable container names instead of IDs so recreation remains discoverable.
    adapter=json.loads((ROOT/'adapter.json').read_text())
    adapter['jwt_container']=jwt['Name'].lstrip('/');adapter['livekit_container']=livekit['Name'].lstrip('/')
    atomic(ROOT/'adapter.json',adapter)
    for path in SOURCE.glob('*.py'): shutil.copyfile(path,APP/path.name);os.chmod(APP/path.name,0o600)
    shutil.copyfile(SOURCE/'disable-addon.sh', APP/'disable-addon.sh')
    os.chmod(APP/'disable-addon.sh',0o700)
    print('Бот:',cfg['bot_user_id']);print('Комната:',cfg['room_id'])
    print('Администратор вступил в новую комнату с PL99: приглашения доступны, сообщения запрещены.')
    atomic(ROOT/'config.json',cfg)
    paths=[unit(UNITS[0],'docker_adapter.py /etc/matrix-staff-directory/adapter.json',[], 'InaccessiblePaths=/etc/matrix-staff-directory/admin.cred /etc/matrix-staff-directory/progress.cred /var/lib/systemd/credential.secret\n'),
           unit(UNITS[1],'collector.py',['admin','bot'], 'RestrictAddressFamilies=AF_INET AF_INET6\n'),
           unit(UNITS[2],'staff_directory.py',['bot','livekit'],'RestrictAddressFamilies=AF_INET AF_INET6\nInaccessiblePaths=/etc/matrix-staff-directory/admin.cred /etc/matrix-staff-directory/progress.cred /var/lib/systemd/credential.secret\n')]
    owned=paths
    command(['systemd-analyze','verify',*[str(p) for p in paths]])
    command(['systemctl','daemon-reload'])
    started=True
    command(['systemctl','start',UNITS[0],UNITS[1]])
    deadline=time.monotonic()+90
    while True:
        if (STATE/'identities.json').exists() and (STATE/'devices.json').exists(): break
        if time.monotonic()>deadline: raise ValueError('Collectors not ready')
        time.sleep(2)
    # One publication in actual systemd credential environment, then read it back.
    props=['-p','LoadCredentialEncrypted=bot:'+str(ROOT/'bot.cred'),'-p','LoadCredentialEncrypted=livekit:'+str(ROOT/'livekit.cred')]
    for script,args in [('staff_directory.py',['--once']),('provision.py',['--verify'])]:
        command(['systemd-run','--quiet','--wait','--pipe','--collect','--unit=staff-first-check-'+secrets.token_hex(4),*props,'/usr/bin/python3',str(APP/script),*args])
    command(['systemctl','enable','--now',*UNITS])
    for u in UNITS: command(['systemctl','is-active','--quiet',u])
    health(domain,stack)
    if before!=fingerprints(stack): raise ValueError('Stack fingerprint changed during installation')
    print('INSTALL OK: 3 addon services active; HTTPS OK; compose/hash unchanged.')
    print('Ключ хоста systemd-creds хранится отдельно; зашифрованные .cred не защищают от root-компрометации.')
    print('Отключение: bash /opt/matrix-staff-directory/disable-addon.sh')
except (Exception, KeyboardInterrupt) as exc:
    if started:
        subprocess.run(['systemctl','disable','--now',*UNITS],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    print('STOP:',str(exc) if isinstance(exc,(ValueError,APIError)) else type(exc).__name__)
    print('Основной стек не перезапускался. Существующие пользователи/комнаты не изменялись.')
    print('При создании нового бота/комнаты они сохраняются в зашифрованном progress.cred; ничего не удаляется автоматически.')
    print('Если config.json уже создан, не запускайте установщик заново; используйте disable-addon.sh и сообщите STOP.')
    sys.exit(1)
