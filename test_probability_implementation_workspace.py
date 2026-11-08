"""The ranking cache must not hide a newly changed model implementation."""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import high_probability_archive as archive
import high_probability_workspace as workspace
import probability_implementation as implementation
from test_high_probability_archive import report
from test_high_probability_engine import NOW


class ProbabilityImplementationWorkspaceTests(unittest.TestCase):
    def test_unchanged_scan_is_cached_but_changed_code_appends_attested_captures(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cache_key = str(root.resolve())
            self.addCleanup(workspace._ARCHIVED_SCANS.pop, cache_key, None)
            scan = report()
            original = deepcopy(scan)
            old_hashes = {name:'a'*64 for name in implementation.SOURCE_FILES}
            new_hashes = {**old_hashes, implementation.SOURCE_FILES[0]:'b'*64}
            with patch.object(api.ApiFootballClient, 'get', side_effect=AssertionError('No API')), \
                    patch.object(implementation, 'code_hashes', return_value=old_hashes) as hashes, \
                    patch.object(archive, 'capture', wraps=archive.capture) as capture:
                first_result = workspace.archive_scan(scan, root=root, clock=lambda:NOW)
                self.assertEqual(first_result, dict(created=2, existing=0, skipped=0))
                first = archive.list_entries(root)
                self.assertEqual(len(first), 2)
                self.assertTrue(all(row['model_code_sha256'] == old_hashes for row in first))

                repeated = workspace.archive_scan(scan, root=root,
                    clock=lambda:NOW+timedelta(minutes=1))
                self.assertEqual(repeated, dict(created=0, existing=2, skipped=0))
                self.assertEqual(capture.call_count, 1)
                self.assertEqual(archive.list_entries(root), first)

                hashes.return_value = new_hashes
                changed = workspace.archive_scan(scan, root=root,
                    clock=lambda:NOW+timedelta(minutes=2))
                self.assertEqual(changed, dict(created=2, existing=0, skipped=0))
                self.assertEqual(capture.call_count, 2)
                saved = archive.list_entries(root)
                self.assertEqual(len(saved), 4)
                for previous in first:
                    self.assertIn(previous, saved)
                    next_capture = next(row for row in saved if row.get('previous_snapshot') == previous['id'])
                    self.assertEqual(next_capture['fixture'], previous['fixture'])
                    self.assertEqual(next_capture['probabilities'], previous['probabilities'])
                    self.assertEqual(next_capture['baseline'], previous['baseline'])
                    self.assertEqual(next_capture['history_fingerprint'], previous['history_fingerprint'])
                    self.assertEqual(next_capture['model_code_sha256'], new_hashes)
                    self.assertNotEqual(next_capture['id'], previous['id'])

                repeated = workspace.archive_scan(scan, root=root,
                    clock=lambda:NOW+timedelta(minutes=3))
                self.assertEqual(repeated, dict(created=0, existing=2, skipped=0))
                self.assertEqual(capture.call_count, 2)
                self.assertEqual(archive.list_entries(root), saved)
            self.assertEqual(scan, original)


if __name__ == '__main__':
    unittest.main()
