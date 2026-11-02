#!/usr/bin/env python3
"""Non-destructive preflight for plugin-recorder-nginx.

Inspired by plugin-recorder 2.2.6 and the Traefik add-on. It deliberately
never stops containers, scales compose services, changes Matrix state, creates
users, deletes profiles, prunes images, or edits Nginx/Element configuration.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request


class Preflight:
    def __init__(self, cfg, run, inspect, inject_nginx):
        self.cfg, self.run, self.inspect, self.inject_nginx = cfg, run, inspect, inject_nginx
        self.errors=[]; self.warnings=[]; self.ok=[]
    def error(self, code, message, hint=''):
        self.errors.append((code, message, hint))
    def warn(self, code, message, hint=''):
        self.warnings.append((code, message, hint))
    def good(self, code, message): self.ok.append((code,message))
    def report(self):
        for code,msg in self.ok: print('[ OK ]',code,msg)
        for code,msg,hint in self.warnings:
            print('[WARN]',code,msg)
            if hint: print('       ',hint)
        for code,msg,hint in self.errors:
            print('[FAIL]',code,msg)
            if hint: print('       ',hint)
        if self.errors: raise RuntimeError('Preflight blocked installation: '+', '.join(x[0] for x in self.errors))

    def host(self):
        for command in ('python3','docker','nginx','openssl'):
            if shutil.which(command): self.good('HOST-'+command, command+' available')
            else: self.error('HOST-'+command, command+' is missing', 'Install it before running the installer')
        if shutil.which('docker'):
            try:self.run(['docker','compose','version']);self.good('COMPOSE','Docker Compose v2 available')
            except Exception:self.error('COMPOSE','Docker Compose v2 is unavailable','Install docker-compose-plugin; do not use legacy docker-compose')

    def paths(self):
        for key in ('element_config','nginx_server_config'):
            p=Path(self.cfg[key])
            if p.is_file():self.good('FILE-'+key,p.as_posix()+' exists')
            else:self.error('FILE-'+key,p.as_posix()+' does not exist')
        try: json.loads(Path(self.cfg['element_config']).read_text());self.good('ELEMENT-JSON','Element config is valid JSON')
        except Exception:self.error('ELEMENT-JSON','Element config is not valid JSON')
        profile=Path(self.cfg['profile_dir'])
        if profile.exists() and not profile.is_dir():self.error('PROFILE','profile_dir is not a directory')
        elif profile.exists():self.good('PROFILE','profile directory exists and will be preserved')
        else:self.warn('PROFILE-NEW','profile directory does not exist; Chromium will create a fresh profile','Do not reuse this device in another recorder')

    def nginx(self):
        if not shutil.which('nginx'):return
        try:self.run(['nginx','-t']);self.good('NGINX-CONFIG','existing nginx configuration passes nginx -t')
        except Exception:self.error('NGINX-CONFIG','existing nginx configuration fails nginx -t','Fix the host Nginx configuration first')
        text=Path(self.cfg['nginx_server_config']).read_text()
        try:self.inject_nginx(text,self.cfg['domain'],Path(self.cfg['plugin_dir'])/'nginx/recorder.conf');self.good('NGINX-VHOST','exact TLS server and locations are compatible')
        except Exception as e:self.error('NGINX-VHOST',str(e),'The installer will not edit an ambiguous vhost or conflicting locations')

    def ports(self):
        for port in (self.cfg['worker_port'],self.cfg['api_port']):
            with socket.socket() as s:
                try:s.bind(('127.0.0.1',port));self.good('PORT-'+str(port),'loopback port is free')
                except OSError:self.error('PORT-'+str(port),'loopback port is occupied','Find the owner with ss -ltnp and stop/reconfigure it; installer will not kill it')

    def images(self):
        for image in (self.cfg['recorder_image'],self.cfg['backend_image']):
            try:
                self.run(['docker','image','inspect',image]);self.good('IMAGE-'+image,'local image exists')
            except Exception:self.error('IMAGE-'+image,'required image is not present locally','The installer never pulls or builds images')
        image=self.cfg['recorder_image']
        script='''set -eu
for x in node python3 ffmpeg ffprobe Xvfb xdpyinfo pactl pulseaudio; do command -v "$x" >/dev/null || { echo missing:$x; exit 21; }; done
node -e "if(+process.versions.node.split('.')[0] < 20) process.exit(22)"
ffmpeg -hide_banner -encoders 2>&1 | grep -q libx264
ffmpeg -hide_banner -encoders 2>&1 | grep -q libmp3lame
node --input-type=module -e "import('puppeteer-core')"
[ -x "$1" ]'''
        try:self.run(['docker','run','--rm','--network','none','--entrypoint','/bin/sh',image,'-c',script,'preflight',self.cfg['chromium_path']],timeout=120);self.good('RECORDER-DEPS','recorder image contains required runtime dependencies')
        except Exception:self.error('RECORDER-DEPS','recorder image dependency probe failed','Required: node>=20, python3, Chromium, puppeteer-core, FFmpeg/ffprobe, Xvfb, xdpyinfo, PulseAudio/pactl, libx264 and libmp3lame')

    def conflicts(self):
        try:
            ids=self.run(['docker','ps','-aq']).splitlines(); infos=json.loads(self.run(['docker','inspect',*ids])) if ids else []
        except Exception as e:self.error('DOCKER-INSPECT','cannot inspect Docker containers',str(e));return
        bot_local=self.cfg['bot_user'].lstrip('@').split(':',1)[0]
        hints=('element-rec-worker','rec-worker','element-recorder','recorder-pool','recording-controller','recording-overlay')
        for info in infos:
            name=info['Name'].lstrip('/');env=dict(v.split('=',1) for v in info['Config'].get('Env',[]) if '=' in v)
            image=info['Config'].get('Image','')
            user=(env.get('REC_USER') or env.get('RECORDER_USER') or '').lstrip('@').split(':',1)[0]
            if user==bot_local and info['State'].get('Running'):
                self.error('BOT-DUPLICATE',f'running container {name} already uses bot {self.cfg["bot_user"]}','Stop the old recorder explicitly; installer does not scale/kill it')
            if any(x in name.lower() or x in image.lower() for x in hints) and info['State'].get('Running'):
                self.warn('RECORDER-LEFTOVER',f'possible existing recorder container: {name}','Verify it does not use the configured bot, profile or ports')
            for mount in info.get('Mounts',[]):
                if Path(mount.get('Source','/')).resolve()==Path(self.cfg['profile_dir']).resolve() and info['State'].get('Running'):
                    self.error('PROFILE-LOCK',f'profile_dir is mounted by running container {name}','Do not share Chromium profile between recorders')
        # Compose project files and old installer override hints are reported, never modified.
        for path in ('/opt/element-stack/compose.json','/opt/element-stack/docker-compose.yml','/matrix/recorder-plugin/state/compose.yaml'):
            if Path(path).is_file():self.warn('COMPOSE-LEFTOVER',path+' exists','Review it for another recorder before installing')

    def element_routes(self):
        # Existing route checks are informational: a clean server has no plugin route yet.
        base='https://'+self.cfg['domain']
        for suffix in ('/recording/','/recordings/','/modules/recorder-nginx/index.js'):
            try:
                req=urllib.request.Request(base+suffix,method='HEAD')
                with urllib.request.urlopen(req,timeout=5) as response:
                    self.warn('ROUTE-EXISTS',suffix+' already responds HTTP '+str(response.status),'Verify this is not an old recorder installation')
            except urllib.error.HTTPError as e:
                if e.code not in (404,405):self.warn('ROUTE-CHECK',suffix+' responds HTTP '+str(e.code),'Check the proxy route before installation')
            except Exception:pass

    def run_all(self):
        self.host();self.paths();self.ports();self.nginx();self.images();self.conflicts();self.element_routes();self.report()
