from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

import service_restoration as restore
import local_recovery as recovery


class ServiceRestorationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.control=self.root/'.restoration';self.control.mkdir()

    def test_child_environment_scrubs_secrets_and_python_path_without_home_override(self):
        with patch.dict(os.environ,dict(API_FOOTBALL_KEY='synthetic',PATTERNLAB_DATABASE_URL='synthetic',
                PYTHONPATH='/wrong',HOME='/unchanged'),clear=True):
            value=restore.clean_environment()
            self.assertEqual(os.environ['HOME'],'/unchanged')
        self.assertEqual(value['PATTERNLAB_STORAGE_MODE'],'local')
        self.assertNotIn('HOME',value);self.assertNotIn('PYTHONPATH',value)
        self.assertNotIn('API_FOOTBALL_KEY',value);self.assertNotIn('PATTERNLAB_DATABASE_URL',value)

    def test_live_and_existing_directories_refused(self):
        with patch.object(restore,'PROJECT',self.root):
            for value in (self.root/'data',self.root):
                with self.assertRaises(ValueError):restore._destination(value)
            chosen=self.root/'.local_backups/new'
            self.assertEqual(restore._destination(chosen),chosen)
            chosen.mkdir(parents=True)
            with self.assertRaises(ValueError):restore._destination(chosen)

    def test_symlink_private_area_refused(self):
        (self.root/'.local_backups').symlink_to(self.control,target_is_directory=True)
        with patch.object(restore,'PROJECT',self.root):
            with self.assertRaises(ValueError):restore._destination(self.root/'.local_backups/new')

    def test_wrong_point_hash_prevents_copy_and_execution(self):
        point=self.root/'point';point.mkdir();(point/'manifest.json').write_text('{}')
        with patch.object(restore,'PROJECT',self.root),patch.object(restore.recovery,'verify_recovery_point') as verify:
            with self.assertRaises(ValueError):restore.restore_files(point,self.root/'.local_backups/new','0'*64)
        verify.assert_not_called();self.assertFalse((self.root/'.local_backups/new').exists())

    def test_failed_subprocess_retains_controlled_diagnostic(self):
        with patch.object(restore.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout='out',stderr='reason')):
            with self.assertRaises(ValueError):restore._run(['new-python','probe'],self.root,label='worker')
        self.assertEqual((self.control/'worker.log').read_text(),'out\nreason')

    def test_timeout_retains_diagnostic(self):
        failure=subprocess.TimeoutExpired('probe',1,output=b'timed',stderr=b'failure')
        with patch.object(restore.subprocess,'run',side_effect=failure):
            with self.assertRaises(subprocess.TimeoutExpired):restore._run(['new-python'],self.root,label='timeout')
        self.assertIn('TIMEOUT',(self.control/'timeout.log').read_text())

    def test_health_failure_terminates_only_owned_child(self):
        child=Mock();child.poll.return_value=None
        reserve=Mock();reserve.getsockname.return_value=('127.0.0.1',18000)
        reserve.__enter__=Mock(return_value=reserve);reserve.__exit__=Mock(return_value=None)
        with patch.object(restore.socket,'socket',return_value=reserve), \
             patch.object(restore.subprocess,'Popen',return_value=child), \
             patch.object(restore.time,'monotonic',side_effect=[0,2]):
            with self.assertRaises(ValueError):restore.server_probe(self.root,Path('new-python'),timeout=1)
        child.terminate.assert_called_once();child.wait.assert_called_once_with(timeout=5)

    def test_external_connect_and_dns_are_refused_before_socket_access(self):
        original_connect=socket.socket.connect;original_resolve=socket.getaddrinfo
        # Restore global socket functions even if the injected guard fails.
        with ExitStack() as stack:
            stack.enter_context(patch.object(socket.socket,'connect',original_connect))
            stack.enter_context(patch.object(socket.socket,'connect_ex',socket.socket.connect_ex))
            stack.enter_context(patch.object(socket,'getaddrinfo',original_resolve))
            exec(compile(restore.GUARD,'guard','exec'),{})
            with socket.socket() as client:
                with self.assertRaisesRegex(RuntimeError,'external'):client.connect(('203.0.113.1',443))
            with self.assertRaisesRegex(RuntimeError,'DNS'):socket.getaddrinfo('example.invalid',443)

    def test_probe_scripts_compile(self):
        compile(restore.GUARD,'guard','exec');compile(restore.APP_PROBE,'app-probe','exec')


if __name__=='__main__':unittest.main()
