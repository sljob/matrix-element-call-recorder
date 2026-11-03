import importlib.util,json,os,socket,ssl,subprocess,tempfile,time,unittest,urllib.request
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
def load(name,path):
 s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
I=load('rc3_installer',ROOT/'installer/install.py');N=I._nginx
P=load('rc3_preflight',ROOT/'installer/preflight.py')
class StructuralTests(unittest.TestCase):
 def test_inline_comments_quotes_variables_nested_scopes(self):
  text='''# comment mentioning server { is not a real block
server { listen 443 ssl; server_name other.example; location /recordings/ { return 404; } }
server { # this is the selected TLS block
 listen 443 ssl; server_name matrix.example.org;
 set $quoted "brace } # not comment";
 location / { proxy_set_header X-Test ${request_id}; if ($request_method = POST) { return 405; } }
}
'''
  out=N.inject(text,'matrix.example.org','/opt/recorder/nginx/recorder.conf')
  selected=N.servers(out,'matrix.example.org')[0]
  include=[n for n in selected.children if n.words[0]=='include']
  self.assertEqual(len(include),1);self.assertEqual(out,N.inject(out,'matrix.example.org','/opt/recorder/nginx/recorder.conf'))
 def test_malformed_and_duplicate_rejected(self):
  for text in ['server { listen 443 ssl; server_name matrix.example.org;', 'server { listen 443 ssl; server_name matrix.example.org; }'*2]:
   with self.assertRaises(ValueError):N.inject(text,'matrix.example.org','/opt/include')
 def test_inactive_and_duplicate_vhost_rejected(self):
  text='server { listen 443 ssl; server_name matrix.example.org; }'
  target='/tmp/target.conf';dump='# configuration file '+target+':'+chr(10)+text
  N.active_target(dump,'matrix.example.org',target)
  with self.assertRaises(ValueError):N.active_target(dump,'matrix.example.org','/tmp/inactive.conf')
  with self.assertRaises(ValueError):N.active_target(dump+chr(10)+'# configuration file /tmp/duplicate.conf:'+chr(10)+text,'matrix.example.org',target)
 def test_different_federation_port_not_duplicate(self):
  text='server { listen 443 ssl; server_name matrix.example.org; } server { listen 8448 ssl; server_name matrix.example.org; }'
  self.assertEqual(len(N.servers(text,'matrix.example.org')),1)
 def test_endpoint_mocks_fail_closed(self):
  cfg=I.read_config(ROOT/'answers.example.json');p=P.Preflight(cfg,lambda *a,**k:'',N.inject);p.context=ssl.create_default_context()
  class Resp:
   def __enter__(self):return self
   def __exit__(self,*a):pass
   def read(self,*a):return b'{"ok":true}'
  with patch.object(P.urllib.request,'urlopen',return_value=Resp()),patch.object(P.socket,'create_connection',side_effect=OSError):p.endpoints()
  self.assertEqual({e[0] for e in p.errors},{'SYNAPSE-ENDPOINT','CALL-ENDPOINT','LIVEKIT-ENDPOINT'})
 def test_release_world_traversable_under_real_umask(self):
  with tempfile.TemporaryDirectory() as d:
   base=Path(d);cfg=I.read_config(ROOT/'answers.example.json');cfg.update(plugin_dir=str(base/'plugin'),profile_dir=str(base/'profile'),element_config=str(base/'element.json'),nginx_server_config=str(base/'site.conf'))
   Path(cfg['element_config']).write_text('{}');Path(cfg['nginx_server_config']).write_text('server { listen 443 ssl; server_name matrix.example.org; }')
   class Health:
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def read(self):return b'{"ok":true}'
   mask=os.umask(0o077)
   try:
    with patch.object(I,'check'),patch.object(I,'run',return_value=''),patch.object(I,'verify_deployment'),patch.object(I.urllib.request,'urlopen',return_value=Health()):I.install(cfg,False)
   finally:os.umask(mask)
   release=(base/'plugin/current').resolve()
   self.assertEqual(release.stat().st_mode&0o777,0o755)
   self.assertEqual((release/'ui/index.js').stat().st_mode&0o777,0o644)
 def test_deployment_probe_failure_rolls_back(self):
  with tempfile.TemporaryDirectory() as d:
   base=Path(d);cfg=I.read_config(ROOT/'answers.example.json');cfg.update(plugin_dir=str(base/'plugin'),profile_dir=str(base/'profile'),element_config=str(base/'element.json'),nginx_server_config=str(base/'site.conf'))
   for k,v in [('element_config','{}'),('nginx_server_config','server { listen 443 ssl; server_name matrix.example.org; }')]:Path(cfg[k]).write_text(v)
   before={k:Path(cfg[k]).read_bytes() for k in ['element_config','nginx_server_config']}
   class Health:
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def read(self):return b'{"ok":true}'
   with patch.object(I,'check'),patch.object(I,'run',return_value=''),patch.object(I,'verify_deployment',side_effect=ValueError('wrong vhost')),patch.object(I.urllib.request,'urlopen',return_value=Health()):
    with self.assertRaisesRegex(ValueError,'wrong vhost'):I.install(cfg,False)
   for k,v in before.items():self.assertEqual(Path(cfg[k]).read_bytes(),v)
   self.assertFalse((base/'plugin/compose.json').exists())

class DeploymentChecks(unittest.TestCase):
 def test_mock_health_cannot_be_success(self):
  with tempfile.TemporaryDirectory() as d:
   release=Path(d);(release/'ui').mkdir();(release/'ui/index.js').write_bytes(b'actual-module')
   cfg=I.read_config(ROOT/'answers.example.json')
   class Response:
    def __init__(self,data):self.data=data
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def read(self):return self.data
   def request(req,**kw):
    url=req if isinstance(req,str) else req.full_url
    if url.endswith('/index.js'):return Response(b'actual-module')
    if url.endswith('/config.json'):return Response(json.dumps({'modules':['/modules/recorder-nginx/index.js']}).encode())
    return Response(b'{"ok":true,"busy":false}')
   with patch.object(I.urllib.request,'urlopen',side_effect=request):
    with self.assertRaisesRegex(ValueError,'identity'):I.verify_deployment(cfg,release)
 def test_incorrect_module_bytes_cannot_be_success(self):
  with tempfile.TemporaryDirectory() as d:
   release=Path(d);(release/'ui').mkdir();(release/'ui/index.js').write_bytes(b'actual-module')
   cfg=I.read_config(ROOT/'answers.example.json')
   class Response:
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def read(self):return b'<html>default vhost</html>'
   with patch.object(I.urllib.request,'urlopen',return_value=Response()):
    with self.assertRaisesRegex(ValueError,'module differs'):I.verify_deployment(cfg,release)

class LiveNginxTests(unittest.TestCase):
 @unittest.skipUnless(os.geteuid()==0,'Run with sudo to verify Nginx worker file access')
 def test_real_module_get_and_real_served_element_config(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);root.chmod(0o755);plugin=root/'plugin';release=plugin/'releases/v3';(release/'ui').mkdir(parents=True)
   for p in [plugin,plugin/'releases',release,release/'ui']:p.chmod(0o755)
   (plugin/'current').symlink_to(release)
   js=(ROOT/'ui/index.js').read_bytes();(release/'ui/index.js').write_bytes(js)
   element=root/'element.json';element.write_text(json.dumps({'modules':['/modules/recorder-nginx/index.js']}))
   cert=root/'cert.pem';key=root/'key.pem'
   subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost','-days','1'],check=True,capture_output=True)
   with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
   cfg=I.read_config(ROOT/'answers.example.json');cfg['plugin_dir']=str(plugin)
   snippet=root/'recorder.conf';snippet.write_text(I.nginx_snippet(cfg))
   # Parse production 443 block, then remap only test listener to free high port.
   site=f'server {{ # inline-comment regression'+chr(10)+f' listen 443 ssl; server_name localhost; ssl_certificate {cert}; ssl_certificate_key {key}; location = /config.json {{ alias {element}; }} }}'
   site=N.inject(site,'localhost',snippet).replace('listen 443 ssl;',f'listen 127.0.0.1:{port} ssl;')
   conf=root/'nginx.conf';conf.write_text(f'user www-data; pid {root}/pid; error_log {root}/error.log; events {{}} http {{ access_log off; '+''.join(x+'_temp_path '+str(root)+'/'+x+';' for x in ['client_body','proxy','fastcgi','uwsgi','scgi'])+site+'}')
   binary='/usr/sbin/nginx';subprocess.run([binary,'-t','-p',str(root),'-c',str(conf)],check=True,capture_output=True)
   process=subprocess.Popen([binary,'-p',str(root),'-c',str(conf),'-g','daemon off;'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
   try:
    context=ssl.create_default_context(cafile=str(cert));base=f'https://localhost:{port}'
    for _ in range(50):
     try:
      with urllib.request.urlopen(base+'/modules/recorder-nginx/index.js',context=context,timeout=2) as r:self.assertEqual(r.read(),js)
      break
     except OSError:time.sleep(.1)
    else:self.fail((root/'error.log').read_text())
    with urllib.request.urlopen(base+'/config.json',context=context) as r:self.assertIn('/modules/recorder-nginx/index.js',json.load(r)['modules'])
   finally:process.terminate();process.wait(timeout=10)
if __name__=='__main__':unittest.main()
