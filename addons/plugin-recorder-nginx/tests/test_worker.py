import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT=Path(__file__).resolve().parents[1]

class WorkerLifecycleTests(unittest.TestCase):
    def test_actual_node_worker_start_stop_publish_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);out=root/'out';out.mkdir();public=root/'public';public.mkdir()
            fixture=root/'fixture.mp4'
            subprocess.run(['ffmpeg','-y','-v','error','-f','lavfi','-i','color=c=blue:size=160x120:rate=10','-f','lavfi','-i','sine=frequency=660:sample_rate=48000','-t','1','-c:v','libx264','-c:a','aac','-movflags','+frag_keyframe+empty_moov',str(fixture)],check=True)
            fake=root/'observer.mjs'
            fake.write_text("import fs from 'node:fs'; fs.copyFileSync(process.env.FIXTURE,process.env.OUT_FILE); process.on('SIGINT',()=>process.exit(0)); setInterval(()=>{},100);")
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            env=dict(os.environ,PORT=str(port),OUT_DIR=str(out),PUBLIC_DIR=str(public),RECORDER_TOKEN='test-token',OBSERVER_JS=str(fake),PUBLISHER_PY=str(ROOT/'runtime/publish.py'),FIXTURE=str(fixture))
            url='http://127.0.0.1:'+str(port)
            def request(path,body=None,token='test-token'):
                req=urllib.request.Request(url+path, data=json.dumps(body).encode() if body is not None else None,headers={'X-Recorder-Token':token})
                try:
                    with urllib.request.urlopen(req,timeout=5) as response:return response.status,json.load(response)
                except urllib.error.HTTPError as error:return error.code,json.load(error)
            def wait_ready():
                for _ in range(100):
                    try:
                        if request('/health')[0]==200:return
                    except OSError:pass
                    time.sleep(.05)
                self.fail('worker did not start')
            log=(root/'worker.log').open('w')
            process=subprocess.Popen(['node',str(ROOT/'runtime/worker.mjs')],env=env,stdout=log,stderr=log)
            try:
                wait_ready();self.assertEqual(request('/status',token='bad')[0],403)
                self.assertEqual(request('/start',{'roomId':'!test:example.org'})[0],202)
                self.assertEqual(request('/start',{'roomId':'!other:example.org'})[0],409)
                self.assertEqual(request('/stop',{'roomId':'!other:example.org'})[0],409)
                for _ in range(100):
                    status=request('/status')[1]
                    if status['state']=='recording':break
                    time.sleep(.05)
                self.assertEqual(status['state'],'recording')
                self.assertEqual(request('/stop',{'roomId':'!test:example.org'})[0],202)
                for _ in range(200):
                    status=request('/status')[1]
                    if status['state'] in ('finalized','error'):break
                    time.sleep(.05)
                self.assertEqual(status['state'],'finalized',status)
                self.assertEqual(len(list(public.glob('*/*.json'))),1)
                self.assertEqual(len(list(public.glob('*/*.mp3'))),1)
                process.terminate();process.wait(timeout=10)
                process=subprocess.Popen(['node',str(ROOT/'runtime/worker.mjs')],env=env,stdout=log,stderr=log)
                wait_ready()
                self.assertEqual(request('/status')[1]['state'],'finalized')
                self.assertEqual(len(list(public.glob('*/*.json'))),1)
            finally:
                if process.poll() is None:process.terminate();process.wait(timeout=10)
                log.close()

if __name__=='__main__':unittest.main()
