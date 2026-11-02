import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('preflight',ROOT/'installer/preflight.py')
P=importlib.util.module_from_spec(spec);spec.loader.exec_module(P)

class PreflightTests(unittest.TestCase):
    def cfg(self,d):
        return dict(domain='matrix.example.org',synapse_url='http://127.0.0.1:18008',
            element_call_url='http://127.0.0.1:8090',livekit_internal='127.0.0.1:17880',
            recorder_image='local/recorder:v1',backend_image='python:3.11',
            bot_user='@recorder:matrix.example.org',profile_dir=str(Path(d)/'profile'),
            element_config=str(Path(d)/'element.json'),nginx_server_config=str(Path(d)/'nginx.conf'),
            plugin_dir=str(Path(d)/'plugin'),worker_port=18789,api_port=18765,
            chromium_path='/usr/bin/chromium',ca_file='',spki_hash='',host_resolver='')
    def test_image_probe_uses_shell_zero_and_chromium_as_one(self):
        with tempfile.TemporaryDirectory() as d:
            cfg=self.cfg(d);Path(cfg['element_config']).write_text('{}')
            Path(cfg['nginx_server_config']).write_text('server { listen 443 ssl; server_name matrix.example.org; }')
            calls=[]
            def fake(args,timeout=120):
                calls.append(args)
                if args[:3]==['docker','run','--rm']: return ''
                return ''
            p=P.Preflight(cfg,fake,lambda n:{},lambda a,b,c:a)
            p.host=lambda:None;p.paths=lambda:None;p.ports=lambda:None;p.nginx=lambda:None;p.conflicts=lambda:None;p.element_routes=lambda:None
            p.images()
            command=next(x for x in calls if x[:3]==['docker','run','--rm'])
            self.assertIn('python3',command[-3])
            self.assertEqual(command[-2:],['preflight','/usr/bin/chromium'])
    def test_running_duplicate_bot_is_fatal(self):
        with tempfile.TemporaryDirectory() as d:
            cfg=self.cfg(d)
            info={'Name':'/old-recorder','State':{'Running':True},'Config':{'Image':'old/recorder','Env':['REC_USER=@recorder:matrix.example.org']},'Mounts':[]}
            def fake(args,timeout=120):
                if args[:3]==['docker','ps','-aq']:return 'id1\n'
                if len(args)>=2 and args[0:2]==['docker','inspect']:return json.dumps([info])
                return ''
            p=P.Preflight(cfg,fake,lambda n:info,lambda a,b,c:a)
            p.conflicts();self.assertTrue(any(code=='BOT-DUPLICATE' for code,_,_ in p.errors))
    def test_old_route_is_warning_not_unconditional_fatal(self):
        with tempfile.TemporaryDirectory() as d:
            cfg=self.cfg(d)
            class Resp:
                status=200
                def __enter__(self):return self
                def __exit__(self,*a):pass
            with patch.object(P.urllib.request,'urlopen',return_value=Resp()):
                p=P.Preflight(cfg,lambda *a,**k:'',lambda n:{},lambda a,b,c:a);p.element_routes()
            self.assertTrue(any(code=='ROUTE-EXISTS' for code,_,_ in p.warnings))

if __name__=='__main__':unittest.main()
