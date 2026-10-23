import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from device_snapshot import DeviceSnapshot
from credentials import publisher_config
from staff_directory import APIError, render

class Rules(unittest.TestCase):
 def test_snapshot_rules(self):
  with tempfile.TemporaryDirectory() as d:
   path=Path(d,'devices.json'); cfg={'state_dir':d}; obj=DeviceSnapshot(cfg)
   normal=lambda p,b,c,e: ('red','busy',0) if b else ('normal',p,1)
   def save(stamp,error=False,checked=1000): path.write_text(json.dumps({'checked_at':checked,'users':{'@u:x':{'stamp':stamp,'error':error}}}))
   with patch('device_snapshot.time.time',return_value=1000):
    save(935);self.assertIn('Недавно в сети',obj.status('@u:x','offline',False,True,True,normal)[1])
    save(373);self.assertEqual(obj.status('@u:x','offline',False,True,True,normal)[1],'Не в сети (по Matrix)')
    save(935,True);self.assertIn('недоступен',obj.status('@u:x','offline',False,True,True,normal)[1])
    save(935,False,700);self.assertIn('недоступен',obj.status('@u:x','offline',False,True,True,normal)[1])
    self.assertEqual(obj.status('@u:x','offline',True,True,True,normal)[1],'busy')
    self.assertEqual(obj.status('@u:x','online',False,True,True,normal)[1],'online')
    self.assertEqual(obj.status('@u:x','unavailable',False,True,True,normal)[1],'unavailable')
 def test_credentials_separation(self):
  with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{'CREDENTIALS_DIRECTORY':d}):
   Path(d,'bot').write_text('bot-test')
   Path(d,'livekit').write_text(json.dumps({'api_key':'key','api_secret':'secret'}))
   cfg=publisher_config({'livekit':{'url':'docker-adapter'}})
   self.assertEqual(cfg['access_token'],'bot-test');self.assertNotIn('admin',cfg)
 def test_names_no_pills(self):
  self.assertNotIn('href=',render([('@u:x','<User>','x','status',0)])['formatted_body'])

class ProvisionMock(unittest.TestCase):
 def run_install(self,fail=None):
  temp=tempfile.TemporaryDirectory(); root=Path(temp.name)
  source=Path(__file__).parent/'provision.py';code=source.read_text()
  for name,target in [('ROOT',root/'etc'),('STATE',root/'state'),('APP',root/'app')]:
   code=code.replace(name+'=Path('+{'ROOT':"'/etc/matrix-staff-directory'",'STATE':"'/var/lib/matrix-staff-directory'",'APP':"'/opt/matrix-staff-directory'"}[name]+')',name+'=Path('+repr(str(target))+')')
  code=code.replace("Path('/etc/systemd/system')", 'Path(' + repr(str(root/'units')) + ')')
  (root/'units').mkdir()
  stack=root/'stack';(stack/'certs').mkdir(parents=True);(stack/'certs/ca.crt').write_text('mock CA');(stack/'compose.json').write_text('{"services":{}}')
  calls=[]; events={}; bot_uid=''; room=False
  answers=iter([str(stack),'cloud.example.org','cloud.example.org'])
  def command(args,data=None):
   calls.append(args)
   if args[0]=='ps':return b'systemd\n'
   if args[:2]==['systemctl','--version']:return b'systemd 255\n'
   if args[0]=='systemd-creds':
    if args[1]=='encrypt': return base64.b64encode(data)
    return base64.b64decode(Path(args[-2]).read_bytes())
   if args[0]=='docker':
    if args[1]=='compose':return (args[-1]+'-id\n').encode()
    return json.dumps([{'State':{'Running':True},'Name':'/'+args[-1],'Config':{'Env':['LIVEKIT_KEY=mock-key','LIVEKIT_SECRET=mock-secret']}}]).encode()
   if args[:2]==['systemctl','start']:
    if fail=='start':raise ValueError('simulated start failure')
    (root/'state/identities.json').write_text('{}');(root/'state/devices.json').write_text('{}')
   return b''
  class FakeAPI:
   def __init__(self,url,token='',ca=None,timeout=20):self.token=token
   def call(self,method,path,data=None,token=None):
    calls.append([method,path])
    if path.endswith('/versions'):
     if fail=='health':raise ValueError('simulated health failure')
     return {'versions':['v1.1']}
    if path.endswith('/account/whoami'):return {'user_id':'@admin:cloud.example.org'}
    if path.endswith('/admin'):return {'admin':True}
    if path.endswith('/devices'):return {'devices':[]}
    if '/_synapse/admin/v2/users/' in path:
     if method=='GET':raise APIError(404,'NOT_FOUND')
     return {}
    if path.endswith('/login'):return {'access_token':'mock-bot-token'}
    if path.endswith('/joined_rooms'):return {'joined_rooms':[]}
    if path.endswith('/createRoom'):
     levels=data['power_level_content_override']
     assert levels['kick']==99 and levels['users']['@admin:cloud.example.org']==99
     assert levels['invite']==50 and levels['events']['m.room.message']==100 and levels['events']['m.reaction']==100
     self.levels=levels;return {'room_id':'!staff:cloud.example.org'}
    if '/state/m.room.power_levels/' in path:return self.levels
    if '/join/' in path:return {'room_id':'!staff:cloud.example.org'}
    raise AssertionError(path)
  # Inject fake boundary operations before main flow, leaving installation code itself intact.
  code=code.replace('started=False\nowned=[]', 'command = injected_command\nAPI = injected_api\nask = injected_ask\nstarted=False\nowned=[]')
  ns={'__file__':str(source),'injected_command':command,'injected_api':FakeAPI,'injected_ask':lambda *a:next(answers)}
  with patch('getpass.getpass',return_value='mock-admin-token'),patch('subprocess.run') as stop,patch('shutil.which',return_value='/usr/bin/mock'):
   try:exec(compile(code,str(source),'exec'),ns)
   except SystemExit as exc:
    if fail is None:raise
    self.assertEqual(exc.code,1)
  return temp,root,calls,stop
 def test_complete_provision_no_stack_mutation(self):
  temp,root,calls,stop=self.run_install()
  try:
   cfg=json.loads((root/'etc/config.json').read_text())
   self.assertEqual(cfg['poll_seconds'],5);self.assertEqual(cfg['server_directory_version'],'0.1.6-staff')
   self.assertNotIn('access_token',cfg);self.assertNotIn('api_secret',cfg['livekit'])
   self.assertEqual((root/'stack/compose.json').read_text(),'{"services":{}}')
   units=list((root/'units').glob('*.service'));self.assertEqual(len(units),3)
   pub=(root/'units/matrix-staff-directory.service').read_text()
   self.assertNotIn('LoadCredentialEncrypted=admin:',pub)
   collector=(root/'units/matrix-staff-directory-collector.service').read_text()
   self.assertIn('LoadCredentialEncrypted=admin:',collector)
   self.assertTrue((root/'app/disable-addon.sh').exists())
   for c in calls:
    if c[0]=='docker':self.assertIn(c[1],('compose','inspect'))
   self.assertFalse(any('restart' in c for c in calls))
  finally:temp.cleanup()
 def test_failed_preflight_no_bot_creation(self):
  temp,root,calls,stop=self.run_install('health')
  try:self.assertFalse(any(c[0]=='PUT' for c in calls))
  finally:temp.cleanup()
 def test_start_failure_disables_only_addon(self):
  temp,root,calls,stop=self.run_install('start')
  try:
   self.assertTrue(stop.called)
   args=stop.call_args.args[0]
   self.assertEqual(args[:3],['systemctl','disable','--now'])
   self.assertTrue(all('matrix-staff-directory' in s for s in args[3:]))
  finally:temp.cleanup()

if __name__=='__main__':unittest.main()
