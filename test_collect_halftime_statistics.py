"""CLI checks: no credentials/network for local status and persistent resume."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC=importlib.util.spec_from_file_location('collect_cli',Path(__file__).parent/'scripts/collect_halftime_statistics.py')
CLI=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(CLI)


class CliTests(unittest.TestCase):
    def test_status_missing_has_no_client_network_or_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'missing'/'half.sqlite3'
            out=io.StringIO()
            with patch('halftime_statistics_client.load_key',side_effect=AssertionError('must not load key')):
                with contextlib.redirect_stdout(out):
                    self.assertEqual(CLI.main(['status','--database',str(path)]),0)
            self.assertFalse(json.loads(out.getvalue())['result']['exists'])
            self.assertFalse(path.parent.exists())

    def test_invalid_batch_rejected_before_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'half.sqlite3'
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    CLI.main(['run','--database',str(path),'--batch-size','0'])
            self.assertFalse(path.exists())

    def test_safe_fixed_error_does_not_echo_exception(self):
        with tempfile.TemporaryDirectory() as temp:
            out=io.StringIO()
            with patch('halftime_statistics_collector.seed_audit',side_effect=RuntimeError('secret-example')):
                with contextlib.redirect_stderr(out):
                    self.assertEqual(CLI.main(['import-audit','--database',str(Path(temp)/'half.sqlite3')]),1)
            self.assertNotIn('secret-example',out.getvalue())
            self.assertEqual(json.loads(out.getvalue())['status'],'error')


if __name__=='__main__':unittest.main()
