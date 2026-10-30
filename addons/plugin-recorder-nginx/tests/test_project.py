import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
I=load('installer',ROOT/'installer/install.py')
P=load('publisher',ROOT/'runtime/publish.py')
B=load('backend',ROOT/'runtime/backend.py')

class InstallerTests(unittest.TestCase):
    def setUp(self):self.cfg=I.read_config(ROOT/'answers.example.json')
    def test_provenance_and_observer(self):
        I.verify_sources();code=I.adapt_observer(self.cfg)
        self.assertIn('matrix-auth-store',code)
        self.assertIn("'--user-data-dir=/work/profile-spk'",code)
        source=(ROOT/'vendor/monolith/p63-observer.js').read_text()
        # Critical auth/join and media sections are byte-identical.
        self.assertEqual(source[source.index('// Fresh Matrix'):source.index('// BROWSER')],code[code.index('// Fresh Matrix'):code.index('// BROWSER')])
        self.assertEqual(source[source.index('// INJECT'):],code[code.index('// INJECT'):])
    def test_nginx_insert_idempotent(self):
        text='server { listen 80; server_name matrix.example.org; }\nserver { listen 443 ssl; server_name matrix.example.org; location / { proxy_pass http://element; } }'
        snippet=Path('/opt/matrix-recorder/nginx/recorder.conf')
        changed=I.inject_nginx(text,self.cfg['domain'],snippet)
        self.assertEqual(changed.count('BEGIN MATRIX'),1)
        self.assertEqual(I.inject_nginx(changed,self.cfg['domain'],snippet),changed)
    def test_nginx_conflict(self):
        with self.assertRaises(ValueError):
            I.inject_nginx('server { listen 443 ssl; server_name matrix.example.org; location /recordings/ { } }',self.cfg['domain'],Path('/tmp/x'))
    def test_compose_contract(self):
        cfg=I.compose_config(self.cfg,Path('/opt/release'),'test')
        for s in cfg['services'].values():self.assertNotIn('build',s)
        w=cfg['services']['worker'];self.assertEqual(w['network_mode'],'host')
        self.assertEqual(w['environment']['PULSE_SRC'],'recsink.monitor')
        self.assertTrue(any(v['target']=='/work/profile-spk' for v in w['volumes']))
    def test_compose_real_schema(self):
        import shutil
        if not shutil.which('docker'):self.skipTest('Docker Compose CLI unavailable')
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'compose.json'
            path.write_text(json.dumps(I.compose_config(self.cfg,Path('/opt/release'),'test')))
            result=subprocess.run(['docker','compose','-p','recorder-test','-f',str(path),'config','--format','json'],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)['services']['worker']['network_mode'],'host')
    def test_dedicated_paths_required(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'answers.json';cfg=dict(self.cfg,plugin_dir='/')
            path.write_text(json.dumps(cfg))
            with self.assertRaises(ValueError):I.read_config(path)
    def test_nginx_real_syntax(self):
        nginx=Path('/usr/sbin/nginx')
        if not nginx.exists():self.skipTest('nginx unavailable')
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/'ui').mkdir()
            cfg=dict(self.cfg,plugin_dir=d)
            snippet=p/'recorder.conf';snippet.write_text(I.nginx_snippet(cfg))
            text='server { listen 127.0.0.1:19443 ssl; server_name matrix.example.org; ssl_certificate '+d+'/cert.pem; ssl_certificate_key '+d+'/key.pem; }'
            text=I.inject_nginx(text,cfg['domain'],snippet)
            subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',d+'/key.pem','-out',d+'/cert.pem','-subj','/CN=matrix.example.org','-days','1'],check=True,capture_output=True)
            main=p/'nginx.conf';main.write_text('pid '+d+'/nginx.pid; error_log '+d+'/error.log; events {} http { client_body_temp_path '+d+'/body; proxy_temp_path '+d+'/proxy; fastcgi_temp_path '+d+'/fastcgi; uwsgi_temp_path '+d+'/uwsgi; scgi_temp_path '+d+'/scgi; access_log off; '+text+' }')
            r=subprocess.run([str(nginx),'-t','-p',d,'-c',str(main)],capture_output=True,text=True)
            self.assertEqual(r.returncode,0,r.stderr)

class PublisherTests(unittest.TestCase):
    def test_actual_fragmented_mp4_audio_poster_idempotence(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);source=p/'rec-test.mp4';public=p/'public';public.mkdir()
            subprocess.run(['ffmpeg','-y','-v','error','-f','lavfi','-i','testsrc=size=320x240:rate=10','-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','2','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-movflags','+frag_keyframe+empty_moov',str(source)],check=True)
            before=source.read_bytes()
            meta={'room_id':'!r_with_underscore:example.org','room_name':'Тест','started_at':'2026-01-01T00:00:00Z','source_bytes':len(before)}
            entry=P.publish(source,meta,public)
            self.assertEqual(source.read_bytes(),before)
            for key in ('file','mp3','jpg'):self.assertTrue((public/entry[key]).stat().st_size>0)
            self.assertEqual(P.publish(source,meta,public),entry)
            self.assertEqual(entry['room_id'],meta['room_id'])
            html=B.page([entry],'@test:example.org')
            self.assertEqual(html.count('<video '),1)
            self.assertEqual(html.count('<audio '),1)
            self.assertIn('Скачать MP3',html)

class APITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name)
        B.STATE=self.base/'state';B.PUBLIC=self.base/'public';B.PUBLIC.mkdir();B.initialize()
        B.BOT='@recorder:example.org'
        self.job={'state':'idle'}
        self.member=True;self.power=100
        def matrix(token,path,method='GET',body=None):
            if path=='account/whoami':return {'user_id':'@user:example.org'}
            if path.endswith('/state/m.room.name'):return {'name':'Тест'}
            if '/state/m.room.member/' in path:return {'membership':'join'}
            return {}
        def worker(path,method='GET',body=None):
            if path=='/health':return {'ok':True,'busy':False}
            if path=='/start':self.job={'room':body['roomId'],'state':'starting'}
            if path=='/stop':self.job['state']='stopping'
            return self.job.copy()
        self.patches=[patch.object(B,'matrix',matrix),patch.object(B,'worker',worker),patch.object(B,'permissions',lambda *args:(self.member,self.power))]
        for p in self.patches:p.start()
        self.server=B.ThreadingHTTPServer(('127.0.0.1',0),B.Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url='http://127.0.0.1:'+str(self.server.server_port)
    def tearDown(self):
        self.server.shutdown();self.server.server_close()
        for p in self.patches:p.stop()
        self.tmp.cleanup()
    def request(self,path,body=None,headers=None):
        req=urllib.request.Request(self.url+path,data=json.dumps(body).encode() if body is not None else None,headers={'Authorization':'Bearer test',**(headers or {})})
        try:
            with urllib.request.urlopen(req) as r:return r.status,r.read(),r.headers
        except urllib.error.HTTPError as e:return e.code,e.read(),e.headers
    def test_start_status_stop_persistent_owner(self):
        room='!room:example.org'
        self.assertEqual(self.request('/api/record/start',{'roomId':room})[0],202)
        self.assertEqual(json.loads(self.request('/api/record/status?room='+room)[1])['state'],'starting')
        with B.database() as db:self.assertEqual(db.execute('SELECT uid FROM owners').fetchone()['uid'],'@user:example.org')
        self.assertEqual(self.request('/api/record/stop',{'roomId':room})[0],202)
    def test_membership_denial(self):
        self.member=False
        self.assertEqual(self.request('/api/record/start',{'roomId':'!r:example.org'})[0],403)
    def test_power_denial(self):
        self.power=0
        self.assertEqual(self.request('/api/record/start',{'roomId':'!r:example.org'})[0],403)
    def test_cross_origin_denial(self):
        self.assertEqual(self.request('/api/record/start',{'roomId':'!r:example.org'},{'Origin':'https://attacker.example'})[0],403)
    def test_archive_file_range_and_denial(self):
        room='!r:example.org';key='a'*64;folder=B.PUBLIC/key;folder.mkdir()
        (folder/'test.mp4').write_bytes(b'0123456789')
        (folder/'test.json').write_text(json.dumps({'room_id':room,'file':key+'/test.mp4','started_at':'x'}))
        url='/recordings/files/'+key+'/test.mp4'
        status,data,headers=self.request(url,headers={'Range':'bytes=2-5'})
        self.assertEqual(status,206);self.assertEqual(data,b'2345');self.assertEqual(headers['Content-Range'],'bytes 2-5/10')
        self.member=False;self.assertEqual(self.request(url)[0],403)
        self.assertEqual(self.request('/recordings/files/../../etc/passwd')[0],404)

if __name__=='__main__':unittest.main(verbosity=2)
