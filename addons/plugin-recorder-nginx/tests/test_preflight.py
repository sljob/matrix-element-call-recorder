import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
P=load('preflight_tested',ROOT/'installer/preflight.py')
I=load('install_tested',ROOT/'installer/install.py')

class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.cfg=I.read_config(ROOT/'answers.example.json')
        self.cfg.update(plugin_dir=str(self.root/'plugin'),profile_dir=str(self.root/'profile'),
            element_config=str(self.root/'element.json'),nginx_server_config=str(self.root/'nginx.conf'))
        Path(self.cfg['element_config']).write_text('{}')
        Path(self.cfg['nginx_server_config']).write_text('server { listen 443 ssl; server_name matrix.example.org; }')
    def tearDown(self):self.tmp.cleanup()
    def pf(self,run=None,repair=False):
        p=P.Preflight(self.cfg,run or (lambda *a,**k:''),I.inject_nginx,repair=repair)
        p.docker_ready=True;return p
    def fixture_container(self,env=None,own=False):
        labels={}
        mounts=[]
        if own:
            labels={'com.docker.compose.project':self.cfg['project'],
                'com.docker.compose.service':'worker',
                'com.docker.compose.project.config_files':str(Path(self.cfg['plugin_dir'])/'compose.json')}
            mounts=[{'Type':'bind','Source':self.cfg['profile_dir'],'Destination':'/work/profile-spk'}]
        return {'Id':'container-id','Name':'/old-recorder','State':{'Running':True},
            'Config':{'Image':'real/recorder','Env':env or [],'Labels':labels},'Mounts':mounts}
    def container_runner(self,info):
        def run(args,**kwargs):
            if args[:3]==['docker','ps','-aq']:return 'container-id\n'
            if args[:2]==['docker','inspect']:return json.dumps([info])
            return ''
        return run
    def test_image_probe_shell_arguments_and_workdir(self):
        calls=[]
        p=self.pf(lambda args,**kwargs:calls.append(args) or '')
        p.images()
        cmd=next(c for c in calls if '--entrypoint' in c and '/bin/bash' in c)
        self.assertEqual(cmd[-2:],['preflight',self.cfg['chromium_path']])
        self.assertIn('--pull',cmd);self.assertIn('never',cmd)
        self.assertEqual(cmd[cmd.index('--workdir')+1],'/app')
        self.assertIn('cd /app',cmd[-3]);self.assertIn('await import',cmd[-3])
        subprocess.run(['bash','-n'],input=cmd[-3],text=True,check=True)
        self.assertFalse(p.errors)
    def test_running_duplicate_bot_is_fatal(self):
        p=self.pf(self.container_runner(self.fixture_container(['REC_USER=recorder'])))
        p.conflicts();self.assertIn('BOT-DUPLICATE',[e[0] for e in p.errors])
    def test_pool_workers_json_duplicate_is_fatal(self):
        env=['POOL_WORKERS='+json.dumps([{'user_id':self.cfg['bot_user']}])]
        p=self.pf(self.container_runner(self.fixture_container(env)))
        p.conflicts();self.assertIn('BOT-DUPLICATE',[e[0] for e in p.errors])
    def test_different_homeserver_same_localpart_is_not_duplicate(self):
        p=self.pf(self.container_runner(self.fixture_container(['REC_USER=@recorder:other.example'])))
        p.conflicts();self.assertNotIn('BOT-DUPLICATE',[e[0] for e in p.errors])
    def test_own_repair_container_not_duplicate(self):
        p=self.pf(self.container_runner(self.fixture_container(['REC_USER=recorder'],own=True)),repair=True)
        p.conflicts();self.assertFalse(p.errors);self.assertEqual(len(p.own),1)
    def test_same_project_without_provenance_not_exempt(self):
        info=self.fixture_container(['REC_USER=recorder'],own=True)
        info['Config']['Labels']['com.docker.compose.project.config_files']='/other/compose.json'
        p=self.pf(self.container_runner(info),repair=True);p.conflicts()
        self.assertIn('BOT-DUPLICATE',[e[0] for e in p.errors])
    def test_missing_nginx_is_report_not_traceback(self):
        Path(self.cfg['nginx_server_config']).unlink()
        p=self.pf();p.paths()
        with patch.object(P.shutil,'which',return_value=None):p.nginx()
        self.assertIn('FILE-nginx_server_config',[e[0] for e in p.errors])
    def test_occupied_foreign_port_is_fatal(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));sock.listen()
            self.cfg['worker_port']=sock.getsockname()[1]
            p=self.pf(repair=True);p.ports()
            self.assertIn('PORT-'+str(self.cfg['worker_port']),[e[0] for e in p.errors])
    def test_repair_port_requires_matching_process_id(self):
        info=self.fixture_container(own=True)
        def run(args,**kw):
            if args[0]=='ss':return 'LISTEN 0 10 127.0.0.1:8789 *:* users:(("node",pid=456,fd=1))'
            if args[:2]==['docker','top']:return 'PID\n123\n456\n'
            return ''
        p=self.pf(run,repair=True);p.own=[info]
        self.assertTrue(p.own_listener(self.cfg['worker_port']))
        p.own=[];self.assertFalse(p.own_listener(self.cfg['worker_port']))
    def test_probe_failure_stage_is_visible_no_secret(self):
        result=subprocess.CompletedProcess([],1,'','RECDEPS_FAIL:puppeteer-core\nprivate-secret')
        error=I.CommandError(['docker','run'],result)
        def run(args,**kw):
            if args[:2]==['docker','run']:raise error
            return '[]'
        p=self.pf(run);p.images()
        self.assertTrue(any('puppeteer-core' in msg for _,msg,_ in p.errors))
        self.assertNotIn('private-secret',str(error))
    def test_check_uses_one_probe_not_two_and_logs_out_temporary_bot(self):
        calls=[];http_calls=[]
        def run(args,**kw):
            calls.append(args)
            if args[:3]==['docker','compose','version']:return '2.40.3'
            if args[:3]==['docker','ps','-aq']:return ''
            return ''
        class Response:
            def __init__(self,data):self.data=data
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def read(self):return json.dumps(self.data).encode()
        def urlopen(req,**kw):
            http_calls.append(req.full_url)
            if req.full_url.endswith('/login'):return Response({'user_id':self.cfg['bot_user'],'access_token':'temporary-secret'})
            return Response({})
        with patch.object(I,'run',side_effect=run),patch.object(I.urllib.request,'urlopen',side_effect=urlopen),patch('shutil.which',side_effect=lambda x:'/usr/bin/'+x):
            I.check(self.cfg,False)
        probes=[c for c in calls if c[:2]==['docker','run'] and '/bin/bash' in c]
        self.assertEqual(len(probes),1)
        self.assertEqual(len([u for u in http_calls if u.endswith('/logout')]),1)
    def test_real_entrypoint_missing_config_fails_not_false_success(self):
        args=self.root/'answers.json';args.write_text(json.dumps(self.cfg))
        for command in ([sys.executable,str(ROOT/'installer/preflight.py'),str(args)],
                        ['bash',str(ROOT/'tools/preflight.sh'),str(args)],
                        ['bash',str(ROOT/'install.sh'),'--check',str(args)]):
            result=subprocess.run(command,cwd='/tmp',capture_output=True,text=True,timeout=30)
            self.assertNotEqual(result.returncode,0)
            self.assertNotIn("name 'inspect' is not defined",result.stderr)
            self.assertNotIn('Traceback',result.stderr)
    @unittest.skipUnless(os.geteuid()==0, 'Run suite with sudo to test privileged CLI entrypoints')
    def test_positive_real_cli_all_entrypoints(self):
        import threading
        from http.server import BaseHTTPRequestHandler,HTTPServer
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*a):pass
            def do_POST(handler):
                data=json.loads(handler.rfile.read(int(handler.headers.get('Content-Length',0))))
                if handler.path.endswith('/login'):
                    self.assertTrue(data['device_id'].startswith('PREFLIGHT_'))
                    answer={'user_id':self.cfg['bot_user'],'access_token':'temporary'}
                else:answer={}
                raw=json.dumps(answer).encode();handler.send_response(200);handler.end_headers();handler.wfile.write(raw)
        server=HTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        bins=self.root/'bin';bins.mkdir()
        docker=bins/'docker-real'
        docker.write_text('#!/bin/sh'+chr(10)+'case "$1 $2" in'+chr(10)+
            '"compose version") echo 2.40.3;;'+chr(10)+
            '"ps -aq") :;;'+chr(10)+
            '"run --rm") echo RECDEPS_OK;;'+chr(10)+
            '*) echo available;;'+chr(10)+'esac'+chr(10))
        docker.chmod(0o755)
        for name in ('nginx','openssl'):
            binary=bins/name;binary.write_text('#!/bin/sh'+chr(10)+'exit 0'+chr(10));binary.chmod(0o755)
        self.cfg.update(docker_binary=str(docker),synapse_url='http://127.0.0.1:'+str(server.server_port))
        answers=self.root/'answers.json';answers.write_text(json.dumps(self.cfg))
        env=dict(os.environ,PATH=str(bins)+os.pathsep+os.environ['PATH'])
        try:
            for cmd in ([sys.executable,str(ROOT/'installer/preflight.py'),str(answers)],
                        ['bash',str(ROOT/'tools/preflight.sh'),str(answers)],
                        ['bash',str(ROOT/'install.sh'),'--check',str(answers)]):
                result=subprocess.run(cmd,cwd='/tmp',env=env,capture_output=True,text=True,timeout=30)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertIn('RECORDER-DEPS',result.stdout)
                self.assertIn('temporary device logged out',result.stdout)
                self.assertFalse((Path(self.cfg['plugin_dir'])/'compose.json').exists())
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_no_path_docker_wrapper_used(self):
        result=subprocess.CompletedProcess([],0,'ok','')
        with patch.object(I.subprocess,'run',return_value=result) as call:
            I.DOCKER_BINARY='/usr/bin/docker';I.run(['docker','info'])
            self.assertEqual(call.call_args.args[0][0],'/usr/bin/docker')

if __name__=='__main__':unittest.main(verbosity=2)
