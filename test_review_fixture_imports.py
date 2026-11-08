"""The terminal inspector stays read-only unless a guarded choice is supplied."""
from contextlib import redirect_stdout, redirect_stderr
import io
from pathlib import Path
import unittest
from unittest.mock import patch

import scripts.review_fixture_imports as cli


class ReviewImportCliTests(unittest.TestCase):
    def setUp(self):
        self.patch(cli.cloud_storage, 'configured', return_value=False)
        self.review = self.patch(cli.api, 'review_import', return_value=dict(id='reviewed', review=dict(choice='prior')))
        self.listing = self.patch(cli.api, 'import_quarantines', return_value=[])
        self.read = self.patch(cli.api, 'load_fixtures', return_value=[])
        self.events = self.patch(cli.api, 'load_import_observations', return_value=[])
        self.patch(cli.api, 'load_key', side_effect=AssertionError('No credentials'))
        self.patch(cli.api.ApiFootballClient, 'get', side_effect=AssertionError('No network'))

    def patch(self, target, name, **kwargs):
        item = patch.object(target, name, **kwargs)
        result = item.start(); self.addCleanup(item.stop)
        return result

    def run_cli(self, args):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return cli.main(['--path', '/tmp/isolated-import-cli.json', *args])

    def test_default_only_lists_conflicts(self):
        self.assertEqual(self.run_cli([]), 0)
        self.listing.assert_called_once_with(Path('/tmp/isolated-import-cli.json'))
        self.review.assert_not_called(); self.read.assert_not_called(); self.events.assert_not_called()

    def test_match_only_reads_current_and_events(self):
        self.assertEqual(self.run_cli(['--match', '123']), 0)
        self.read.assert_called_once_with(Path('/tmp/isolated-import-cli.json'), ids=['123'])
        self.events.assert_called_once_with(Path('/tmp/isolated-import-cli.json'), ids=['123'])
        self.review.assert_not_called()

    def test_choice_needs_expected_snapshot_and_reason(self):
        with self.assertRaises(SystemExit) as error:
            self.run_cli(['--choose', 'received', '--match', '123'])
        self.assertEqual(error.exception.code, 2)
        self.review.assert_not_called()

    def test_explicit_review_forwards_exact_observation(self):
        self.assertEqual(self.run_cli(['--choose', 'prior', '--match', '123', '--expected', 'a'*64,
                                      '--reason', 'Score confirmé']), 0)
        self.review.assert_called_once_with('123', path=Path('/tmp/isolated-import-cli.json'),
            choice='prior', expected_observation='a'*64, reason='Score confirmé')

    def test_cloud_is_not_changed_by_local_tool(self):
        with patch.object(cli.cloud_storage, 'configured', return_value=True):
            with self.assertRaises(SystemExit) as error:
                self.run_cli([])
        self.assertEqual(error.exception.code, 2)
        self.review.assert_not_called(); self.listing.assert_not_called()


if __name__ == '__main__':
    unittest.main()
