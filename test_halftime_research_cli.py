"""CLI protocol integrity and no implicit model evaluation."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC=importlib.util.spec_from_file_location('research_cli',Path(__file__).parent/'scripts/halftime_research.py')
CLI=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(CLI)


class CliTests(unittest.TestCase):
    def test_status_does_not_prepare_or_evaluate(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp);(path/'readiness.json').write_text(json.dumps(dict(ready=False)))
            with patch('halftime_research_experiment.prepare',side_effect=AssertionError('no prepare')):
                with patch('halftime_research_experiment.evaluate',side_effect=AssertionError('no evaluate')):
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(CLI.main(['status','--directory',str(path)]),0)
    def test_changed_protocol_fails_before_new_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);protocol=root/'plan.json';protocol.write_text('{}')
            with patch('halftime_research_experiment.verify_code',side_effect=ValueError('mismatch')):
                with patch('halftime_research_experiment.prepare',side_effect=AssertionError('must not prepare')):
                    with contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(CLI.main(['prepare','--directory',str(root/'new'),'--protocol',str(protocol)]),1)
            self.assertFalse((root/'new').exists())


if __name__=='__main__':unittest.main()
