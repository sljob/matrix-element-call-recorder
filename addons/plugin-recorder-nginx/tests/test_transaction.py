"""Installer file transaction tests; Docker/HTTP execution is mocked explicitly."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('installer_transaction',ROOT/'installer/install.py')
I=importlib.util.module_from_spec(spec);spec.loader.exec_module(I)

class Response:
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,*args):return b'{"ok":true}'

class TransactionTests(unittest.TestCase):
    def fixture(self,d):
        root=Path(d);element=root/'element.json';nginx=root/'matrix.conf'
        element.write_text('{"modules":["/existing.js"]}')
        nginx.write_text('server { listen 443 ssl; server_name matrix.example.org; }')
        cfg=I.read_config(ROOT/'answers.example.json')
        cfg.update(plugin_dir=str(root/'plugin'),profile_dir=str(root/'profile'),element_config=str(element),nginx_server_config=str(nginx))
        return cfg,element,nginx
    def test_install_repair_preserves_identity_and_profile(self):
        with tempfile.TemporaryDirectory() as d:
            cfg,element,nginx=self.fixture(d)
            profile=Path(cfg['profile_dir']);profile.mkdir();(profile/'important-key').write_bytes(b'keep')
            with patch.object(I,'check'),patch.object(I,'run',return_value=''),patch.object(I.urllib.request,'urlopen',return_value=Response()):
                I.install(cfg,False)
                first=json.loads((Path(cfg['plugin_dir'])/'compose.json').read_text())
                token=first['services']['worker']['environment']['RECORDER_TOKEN']
                I.install(cfg,True)
            result=json.loads(element.read_text())
            self.assertEqual(result['modules'].count('/modules/recorder-nginx/index.js'),1)
            self.assertIn('/existing.js',result['modules'])
            self.assertEqual(nginx.read_text().count('BEGIN MATRIX'),1)
            self.assertEqual((profile/'important-key').read_bytes(),b'keep')
            second=json.loads((Path(cfg['plugin_dir'])/'compose.json').read_text())
            self.assertEqual(second['services']['worker']['environment']['RECORDER_TOKEN'],token)
            self.assertTrue((Path(cfg['plugin_dir'])/'current').is_symlink())
    def test_failed_reload_rolls_back_existing_configuration(self):
        with tempfile.TemporaryDirectory() as d:
            cfg,element,nginx=self.fixture(d)
            old_element=element.read_bytes();old_nginx=nginx.read_bytes()
            def run(args,**kwargs):
                if args==['nginx','-s','reload']:raise RuntimeError('simulated reload failure')
                return ''
            with patch.object(I,'check'),patch.object(I,'run',side_effect=run),patch.object(I.urllib.request,'urlopen',return_value=Response()):
                with self.assertRaises(RuntimeError):I.install(cfg,False)
            self.assertEqual(element.read_bytes(),old_element)
            self.assertEqual(nginx.read_bytes(),old_nginx)
            self.assertFalse((Path(cfg['plugin_dir'])/'compose.json').exists())
            self.assertTrue(Path(cfg['profile_dir']).is_dir())

if __name__=='__main__':unittest.main()
