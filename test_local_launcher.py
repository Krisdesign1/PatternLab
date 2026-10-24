"""Exercise the launcher with a fake interpreter: no pip, server or network."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class LocalLauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.command = self.root / 'Lancer_PatternLab.command'
        shutil.copy(Path(__file__).parent / self.command.name, self.command)
        self.root.joinpath('requirements.txt').write_text('test-only-dependency>=1\n')
        self.root.joinpath('.venv', 'bin').mkdir(parents=True)
        self.log = self.root / 'interpreter-calls.jsonl'
        stub = self.root / '.venv' / 'bin' / 'python'
        stub.write_text('#!' + sys.executable + '\n' + r'''
import json,os,pathlib,subprocess,sys
arguments=sys.argv[1:]
if arguments[:1]==['-c']:
    if 'find_spec' in arguments[1]:
        raise SystemExit(1 if os.environ.get('TEST_MISSING')=='true' else 0)
    raise SystemExit(subprocess.run([sys.executable,*arguments]).returncode)
with open(os.environ['TEST_LOG'],'a') as handle:
    handle.write(json.dumps({'args':arguments,'storage':os.environ.get('PATTERNLAB_STORAGE_MODE')})+'\n')
if arguments[:2]==['-m','pip']:
    raise SystemExit(1 if os.environ.get('TEST_INSTALL_FAIL')=='true' else 0)
if arguments[:2]==['-m','streamlit']:
    raise SystemExit(0)
raise SystemExit(99)
''')
        stub.chmod(0o700)

    def launch(self, **kwargs):
        env = {**os.environ, 'TEST_LOG': str(self.log), **kwargs}
        result = subprocess.run(['/bin/bash', str(self.command)], capture_output=True, text=True,
                                timeout=15, env=env)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return result, calls

    def test_daily_launch_skips_install_and_stays_on_loopback_local_mode(self):
        first, first_calls = self.launch(PATTERNLAB_STORAGE_MODE='shared')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(len(first_calls), 2)
        self.log.unlink()
        daily, calls = self.launch(PATTERNLAB_STORAGE_MODE='shared')
        self.assertEqual(daily.returncode, 0, daily.stderr)
        self.assertEqual(calls, [{'args': ['-m', 'streamlit', 'run', 'app.py', '--server.address', '127.0.0.1'], 'storage': 'local'}])

    def test_changed_requirements_or_missing_dependency_installs_once(self):
        self.assertEqual(self.launch()[0].returncode, 0)
        for change in ('requirements', 'missing'):
            self.log.unlink()
            if change == 'requirements':
                self.root.joinpath('requirements.txt').write_text('test-only-dependency>=2\n')
            result, calls = self.launch(TEST_MISSING='true' if change == 'missing' else 'false')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(calls[0]['args'], ['-m', 'pip', 'install', '-r', 'requirements.txt'])
            self.assertEqual(len(calls), 2)

    def test_failed_install_never_starts_server_or_records_success(self):
        result, calls = self.launch(TEST_INSTALL_FAIL='true')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['args'][:2], ['-m', 'pip'])
        self.assertFalse(self.root.joinpath('.venv', '.patternlab-requirements.sha256').exists())

    def test_missing_venv_interpreter_stops_before_any_operation(self):
        self.root.joinpath('.venv', 'bin', 'python').unlink()
        result, calls = self.launch()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
