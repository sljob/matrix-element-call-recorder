#!/usr/bin/env python3
"""Shared installer preflight. Never edits services, profiles, users or room state."""
import json
from pathlib import Path
import re
import shutil
import socket
import ssl
import sys

RECORDER_PROBE = r'''set -eu
stage=bootstrap
trap 'printf "RECDEPS_FAIL:%s\n" "$stage" >&2' EXIT
for x in bash node python3 ffmpeg ffprobe Xvfb xdpyinfo pactl pulseaudio; do
 stage=command:$x; command -v "$x" >/dev/null
done
stage=node-version; node -e 'if(+process.versions.node.split(".")[0]<20)process.exit(1)'
stage=python-version; python3 -c 'import sys;assert sys.version_info>=(3,11)'
stage=chromium-executable; test -x "$1"
stage=chromium-version; "$1" --version >/dev/null
stage=ffmpeg-fps-mode; ffmpeg -hide_banner -h full > /tmp/probe-help 2>&1; grep -q -- -fps_mode /tmp/probe-help
stage=ffmpeg-encoders; ffmpeg -hide_banner -encoders > /tmp/probe-encoders 2>&1
stage=libx264; grep -q libx264 /tmp/probe-encoders
stage=libmp3lame; grep -q libmp3lame /tmp/probe-encoders
stage=puppeteer-core; cd /app; node --input-type=module -e 'await import("puppeteer-core")'
trap - EXIT
printf 'RECDEPS_OK\n'
'''

def normalize_bot(value, server):
    if not isinstance(value, str) or not value.strip(): return ''
    value = value.strip().lstrip('@')
    return '@' + (value if ':' in value else value + ':' + server)

def bot_accounts(env, server):
    found=set()
    for key in ('REC_USER','RECORDER_USER','RECORDER_USER_ID'):
        if env.get(key): found.add(normalize_bot(env[key],server))
    for value in env.get('RECORDER_ACCOUNTS','').split(','):
        if value.strip():found.add(normalize_bot(value,server))
    for key in ('POOL_WORKERS','WORKER_ACCOUNTS'):
        if not env.get(key):continue
        try:
            values=json.loads(env[key])
            if not isinstance(values,list):raise ValueError()
            for item in values:
                if isinstance(item,dict):
                    value=item.get('user_id') or item.get('user') or item.get('account')
                    if value:found.add(normalize_bot(value,server))
        except (ValueError,TypeError):raise ValueError('Malformed '+key+'; inspect pool accounts locally')
    return found

class Preflight:
    def __init__(self,cfg,run,inject_nginx,repair=False):
        self.cfg,self.run,self.inject_nginx,self.repair=cfg,run,inject_nginx,repair
        self.errors=[];self.warnings=[];self.ok=[];self.containers=[];self.own=[]
        self.docker_ready=False;self.local_images=set();self.context=None
    def error(self,code,msg,hint=''):self.errors.append((code,msg,hint))
    def warn(self,code,msg,hint=''):self.warnings.append((code,msg,hint))
    def good(self,code,msg):self.ok.append((code,msg))
    def report(self):
        for code,msg in self.ok:print('[ OK ]',code,msg)
        for label,items in [('WARN',self.warnings),('FAIL',self.errors)]:
            for code,msg,hint in items:
                print('['+label+']',code,msg)
                if hint:print('       ',hint)
        if self.errors:raise RuntimeError('Preflight blocked: '+', '.join(dict.fromkeys(e[0] for e in self.errors)))
    def host(self):
        for cmd in ('python3','nginx','openssl'):
            if shutil.which(cmd):self.good('HOST-'+cmd,cmd+' available')
            else:self.error('HOST-'+cmd,cmd+' missing','Install the corresponding Ubuntu package; no packages are installed automatically.')
        binary=self.cfg.get('docker_binary','/usr/bin/docker')
        if not Path(binary).is_file():
            self.error('DOCKER-BINARY','Configured Docker CLI does not exist','Install Docker Engine and Compose v2; set docker_binary to its real absolute executable, not a logging shim.');return
        if shutil.which('docker')!=binary:
            self.warn('DOCKER-PATH','PATH Docker differs from configured CLI','Using '+binary+' for both probes and Compose; /usr/local/bin/docker injections are not used or deleted.')
        try:
            self.run(['docker','info','--format','{{.ServerVersion}}']);self.docker_ready=True
            self.good('DOCKER-DAEMON','Docker daemon accessible')
        except Exception:self.error('DOCKER-DAEMON','Docker daemon unavailable','Start Docker or correct permissions/context; do not replace images with stubs.');return
        try:
            text=self.run(['docker','compose','version','--short'])
            if not re.match(r'^v?2\.',text.strip()):raise ValueError()
            self.good('COMPOSE','Compose v2 available')
        except Exception:self.error('COMPOSE','Compose v2 unavailable','Install docker-compose-plugin or the Ubuntu docker-compose-v2 package for this OS.')
    def paths(self):
        for key in ('element_config','nginx_server_config'):
            p=Path(self.cfg[key])
            if p.is_file():self.good('FILE-'+key,'Configured file exists')
            else:self.error('FILE-'+key,'Configured file missing: '+str(p),'Point answers at the real host configuration. Do not create an empty or stub file.')
        p=Path(self.cfg['element_config'])
        if p.is_file():
            try:
                data=json.loads(p.read_text())
                if not isinstance(data,dict) or not isinstance(data.get('modules',[]),list):raise ValueError()
                self.good('ELEMENT-JSON','Element object and modules list valid')
                for item in data.get('modules',[]):
                    if 'record' in str(item).lower() and item!='/modules/recorder-nginx/index.js':
                        self.error('ELEMENT-OLD-MODULE','Another recorder module is configured','Back up and remove only the obsolete recorder module entry; preserve unrelated modules.')
            except (ValueError,OSError):self.error('ELEMENT-JSON','Invalid Element configuration','Restore a real Element config; no placeholder HTML/JSON.')
        profile=Path(self.cfg['profile_dir'])
        if profile.exists() and not profile.is_dir():self.error('PROFILE','profile_dir is not a directory')
        elif not profile.exists():self.warn('PROFILE-NEW','New persistent Chromium profile will be created','Do not reuse a device ID with a different existing crypto database.')
        root=Path(self.cfg['plugin_dir']);saved=root/'identity.json'
        if saved.exists():
            try:
                if json.loads(saved.read_text())!={k:self.cfg[k] for k in ('bot_user','device_id','profile_dir')}:raise ValueError()
                self.good('IDENTITY','Stored bot/device/profile matches')
            except (ValueError,OSError):self.error('IDENTITY','Stored identity differs or cannot be read','Repair cannot change bot/device/profile. Use a separate installation for another identity.')
        elif self.repair:self.error('IDENTITY','Repair requires an existing identity.json','Do not claim another Compose project as this installation.')
        if not self.repair and (root/'compose.json').exists():self.error('EXISTING-INSTALL','Installation already exists','Use --repair after finishing the current recording.')
    def tls(self):
        try:
            self.context=ssl.create_default_context(cafile=self.cfg.get('ca_file') or None)
            if self.cfg.get('ca_file') and not self.cfg.get('spki_hash'):raise ValueError('Private CA requires explicit leaf SPKI hash')
            if self.cfg.get('spki_hash'):
                import base64
                if len(base64.b64decode(self.cfg['spki_hash'],validate=True))!=32:raise ValueError('Invalid SPKI SHA256/base64')
            self.good('TLS','CA and SPKI input syntax valid (not a live Chromium TLS test)')
        except (ValueError,OSError,ssl.SSLError) as e:self.error('TLS',str(e),'Use the real CA PEM and SHA256 SPKI of the current TLS leaf; no global ignore-certificate-errors flag.')
    def nginx(self):
        if shutil.which('nginx'):
            try:self.run(['nginx','-t']);self.good('NGINX-CONFIG','nginx -t passed')
            except Exception:self.error('NGINX-CONFIG','nginx -t failed','Run nginx -t locally to see the certificate/include error; do not publish private keys.')
        p=Path(self.cfg['nginx_server_config'])
        if not p.is_file():return
        try:self.inject_nginx(p.read_text(),self.cfg['domain'],Path(self.cfg['plugin_dir'])/'nginx/recorder.conf');self.good('NGINX-VHOST','Target TLS server and locations compatible')
        except (ValueError,OSError) as e:self.error('NGINX-VHOST',str(e),'Select the authoritative TLS server file; remove obsolete recorder locations only after backup.')
    def is_own(self,info):
        labels=info['Config'].get('Labels') or {}
        expected=str(Path(self.cfg['plugin_dir'])/'compose.json')
        files=labels.get('com.docker.compose.project.config_files','').split(',')
        service=labels.get('com.docker.compose.service')
        if not self.repair or labels.get('com.docker.compose.project')!=self.cfg.get('project') or expected not in files:return False
        targets={'worker':('/work/profile-spk',self.cfg['profile_dir']),'backend':('/state',str(Path(self.cfg['plugin_dir'])/'state'))}
        if service not in targets:return False
        dest,source=targets[service]
        return any(m.get('Destination')==dest and Path(m.get('Source','/')).resolve()==Path(source).resolve() for m in info.get('Mounts',[]))
    def conflicts(self):
        if not self.docker_ready:return
        try:
            ids=self.run(['docker','ps','-aq']).splitlines()
            self.containers=json.loads(self.run(['docker','inspect',*ids])) if ids else []
        except Exception:self.error('DOCKER-INSPECT','Cannot inspect container metadata','Check Docker daemon/context.');return
        server=self.cfg['bot_user'].split(':',1)[1]
        for info in self.containers:
            if not info.get('State',{}).get('Running') and not info.get('State',{}).get('Restarting'):continue
            if self.is_own(info):self.own.append(info);continue
            name=info.get('Name','container').lstrip('/')
            env=dict(x.split('=',1) for x in info['Config'].get('Env',[]) if '=' in x)
            try:accounts=bot_accounts(env,server)
            except ValueError as e:self.error('POOL-ACCOUNTS',str(e),'Inspect '+name+' pool configuration locally; secrets are not printed.');accounts=set()
            if self.cfg['bot_user'] in accounts:self.error('BOT-DUPLICATE','Configured bot already used by '+name,'Stop the old recorder explicitly or configure a DIFFERENT bot, device and profile. An existing unrelated bot is not a conflict.')
            for m in info.get('Mounts',[]):
                if m.get('Type')!='bind':continue
                a=Path(m['Source']).resolve();b=Path(self.cfg['profile_dir']).resolve()
                if a==b or a in b.parents or b in a.parents:self.error('PROFILE-LOCK','Profile overlaps a bind mount of '+name,'Do not share the persistent profile across running containers.')
            if re.search(r'recorder|rec-worker|recording-controller|recording-overlay',name,re.I):self.warn('RECORDER-LEFTOVER','Other recorder container: '+name,'Different bots/profiles may coexist; review ports and room participation. No automatic stop/delete.')
        for p in ('/opt/element-stack/compose.json','/matrix/recorder-plugin/state/compose.yaml'):
            if Path(p).is_file():self.warn('COMPOSE-LEFTOVER','Existing stack file: '+p,'Never edit or scale an unrelated stack automatically.')
    def own_listener(self,port):
        # Never exempt an occupied port based only on a project name or HTTP /health.
        try:
            text=self.run(['ss','-H','-ltnp','sport = :'+str(port)])
            pids=set(re.findall(r'pid=(\d+)',text))
            if not pids:return False
            service='worker' if port==self.cfg['worker_port'] else 'backend'
            ownpids=set()
            for info in self.own:
                if info['Config']['Labels'].get('com.docker.compose.service')!=service:continue
                lines=self.run(['docker','top',info['Id'],'-eo','pid']).splitlines()[1:]
                ownpids.update(x.strip() for x in lines if x.strip().isdigit())
            return pids.issubset(ownpids)
        except Exception:return False
    def ports(self):
        for port in (self.cfg['worker_port'],self.cfg['api_port']):
            try:
                with socket.socket() as s:s.bind(('127.0.0.1',port))
                self.good('PORT-'+str(port),'Loopback port free')
            except OSError:
                if self.repair and self.own_listener(port):self.good('PORT-'+str(port),'Listener belongs to this installation (PID verified)')
                else:self.error('PORT-'+str(port),'Port is occupied or unavailable','Use ss -ltnp locally. Select a free port or stop its actual owner; no process is killed.')
    def images(self):
        if not self.docker_ready:return
        for image in dict.fromkeys([self.cfg['recorder_image'],self.cfg['backend_image']]):
            try:self.run(['docker','image','inspect',image]);self.local_images.add(image);self.good('IMAGE','Local image found: '+image)
            except Exception:self.error('IMAGE','Missing local image: '+image,'Import the genuine image using docker load, or pull a verified image explicitly. Never retag a stub as a recorder.')
        if self.cfg['recorder_image'] in self.local_images:
            try:
                self.run(['docker','run','--rm','--pull','never','--network','none','--workdir','/app','--entrypoint','/bin/bash',self.cfg['recorder_image'],'-c',RECORDER_PROBE,'preflight',self.cfg['chromium_path']],timeout=120)
                self.good('RECORDER-DEPS','Real image tools, versions, FFmpeg capabilities and /app ESM import passed')
            except Exception as e:
                result=getattr(e,'stdout','')+' '+getattr(e,'stderr','')
                stage=re.findall(r'RECDEPS_FAIL:([A-Za-z0-9:_-]+)',result)
                self.error('RECORDER-DEPS','Recorder dependency probe failed'+(' at '+stage[-1] if stage else ''),'Use a complete existing recorder image. puppeteer-core must resolve from /app/node_modules at runtime; NODE_PATH/docker shims are not a fix. No npm install or image modification is performed.')
        if self.cfg['backend_image'] in self.local_images:
            try:self.run(['docker','run','--rm','--pull','never','--network','none','--entrypoint','python3',self.cfg['backend_image'],'-c','import sys,sqlite3,ssl;assert sys.version_info>=(3,11)']);self.good('BACKEND-DEPS','Backend Python >=3.11 and stdlib dependencies passed')
            except Exception:self.error('BACKEND-DEPS','Backend image lacks Python >=3.11/SQLite/SSL','Choose a compatible Python image, not the recorder image by assumption.')
    def run_all(self):
        for name in ('host','paths','tls','nginx','conflicts','ports','images'):
            try:getattr(self,name)()
            except Exception as e:self.error('CHECK-'+name,'Check could not complete: '+type(e).__name__,'Correct the input or environment; no success is assumed for an incomplete check.')
        self.report()

def main():
    import argparse
    parser=argparse.ArgumentParser(description='Run the same --check path as the installer, including temporary bot login/logout.')
    parser.add_argument('answers');args=parser.parse_args()
    try:
        from install import read_config,check
        import os
        if os.geteuid()!=0:raise ValueError('Run as root')
        cfg=read_config(args.answers)
        check(cfg,(Path(cfg['plugin_dir'])/'compose.json').exists())
    except Exception as e:print('ERROR:',e,file=sys.stderr);return 1
    return 0

if __name__=='__main__':sys.exit(main())
