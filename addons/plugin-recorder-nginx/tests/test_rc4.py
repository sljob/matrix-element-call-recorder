import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

I = load('rc4_installer', ROOT / 'installer/install.py')
P = load('rc4_preflight', ROOT / 'installer/preflight.py')
STDOUT = (ROOT / 'tests/fixtures/nginx-T.stdout.conf').read_text()
STDERR = (ROOT / 'tests/fixtures/nginx-T.stderr.txt').read_text()
TARGET = '/etc/nginx/sites-enabled/matrix.example.org.conf'

class NginxStreamTests(unittest.TestCase):
    def test_reproduces_rc3_failure_on_captured_dump(self):
        I._nginx.active_target(STDOUT, 'matrix.example.org', TARGET)
        with self.assertRaisesRegex(ValueError, 'Incomplete Nginx configuration'):
            I._nginx.active_target(STDOUT + STDERR, 'matrix.example.org', TARGET)

    def test_runner_keeps_success_diagnostics_separate(self):
        result = subprocess.CompletedProcess(['nginx', '-T'], 0, STDOUT, STDERR)
        with patch.object(I.subprocess, 'run', return_value=result):
            output = I.run(['nginx', '-T'])
        self.assertEqual(str(output), STDOUT)
        self.assertEqual(output.stderr, STDERR)
        I._nginx.active_target(output, 'matrix.example.org', TARGET)

    def test_no_warning_needed_rc3_also_failed_on_success_notice(self):
        notice = 'nginx: configuration file /etc/nginx/nginx.conf test is successful\n'
        result = subprocess.CompletedProcess(['nginx', '-T'], 0, STDOUT, notice)
        with patch.object(I.subprocess, 'run', return_value=result):
            output = I.run(['nginx', '-T'])
        self.assertNotIn('test is successful', str(output))
        I._nginx.active_target(output, 'matrix.example.org', TARGET)

    def preflight(self, diagnostics):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        site = Path(temporary.name) / 'site.conf'
        site.write_text('server { listen 443 ssl; server_name matrix.example.org; }')
        cfg = I.read_config(ROOT / 'answers.example.json')
        cfg['nginx_server_config'] = str(site)
        dump = '# configuration file ' + str(site) + ':\n' + site.read_text() + '\n'
        def run(args, **kwargs):
            if args == ['nginx', '-T']:
                return I.CommandOutput(dump, diagnostics)
            return I.CommandOutput('', '')
        return P.Preflight(cfg, run, I.inject_nginx)

    def test_deprecation_warning_does_not_block_valid_vhost(self):
        pf = self.preflight(STDERR)
        with patch.object(P.shutil, 'which', return_value='/usr/sbin/nginx'):
            pf.nginx()
        self.assertFalse(pf.errors, pf.errors)
        self.assertIn('NGINX-ACTIVE', [code for code, _ in pf.ok])
        self.assertIn('NGINX-WARNING', [code for code, _, _ in pf.warnings])

    def test_duplicate_warning_is_still_fatal_from_stderr(self):
        pf = self.preflight('nginx: [warn] conflicting server name \"matrix.example.org\" on 0.0.0.0:443, ignored\n')
        with patch.object(P.shutil, 'which', return_value='/usr/sbin/nginx'):
            pf.nginx()
        self.assertIn('NGINX-ACTIVE', [code for code, _, _ in pf.errors])
        self.assertIn('ignored duplicate', pf.errors[-1][1])

    def test_command_failure_not_converted_to_empty_success(self):
        result = subprocess.CompletedProcess(['nginx', '-T'], 1, '', 'private-error-body')
        with patch.object(I.subprocess, 'run', return_value=result):
            with self.assertRaises(I.CommandError) as caught:
                I.run(['nginx', '-T'])
        self.assertNotIn('private-error-body', str(caught.exception))

    def test_real_cli_separate_streams(self):
        # Real OS process, not a CompletedProcess double; no Nginx or VM changes.
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'nginx'
            executable.write_text('#!/usr/bin/env python3\nimport sys\nsys.stdout.write(' + repr(STDOUT) + ')\nsys.stderr.write(' + repr(STDERR) + ')\n')
            executable.chmod(0o755)
            with patch.dict(os.environ, {'PATH': directory + os.pathsep + os.environ['PATH']}):
                output = I.run(['nginx', '-T'])
            self.assertEqual(str(output), STDOUT)
            self.assertEqual(output.stderr, STDERR)
            I._nginx.active_target(output, 'matrix.example.org', TARGET)

if __name__ == '__main__':
    unittest.main(verbosity=2)
