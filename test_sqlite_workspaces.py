"""Indexed local screens retain the same predictions and frozen audit inputs."""
from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import api_match_workspace as calendar
import daily_evaluation_store as evaluations
import fixture_store
import high_probability_workspace as workspace
from test_high_probability_engine import fixture, history, NOW
from scripts import migrate_local_fixtures as migration


class IndexedWorkspacesTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / 'fixtures.json'
        self.raw = history() + [fixture(100001, stamp='2026-10-22T09:00:30+00:00'),
                                fixture(100002, stamp='2026-10-22T12:00:00+00:00')]
        api.save_fixtures(self.raw, self.path)
        workspace._cached_scan.cache_clear()
        self.addCleanup(workspace._cached_scan.cache_clear)

    def migrate(self):
        return fixture_store.migrate_json(self.path)

    def test_calendar_and_probabilities_identical_without_reading_json_after_activation(self):
        before = calendar.local_snapshot(NOW.date(), now=NOW, path=self.path)
        forecast = workspace.load_scan(self.root, NOW)
        self.migrate()
        with patch.object(api, '_load_store', side_effect=AssertionError('No JSON reads')), \
                patch.object(api.ApiFootballClient, 'get', side_effect=AssertionError('No API')):
            after = calendar.local_snapshot(NOW.date(), now=NOW, path=self.path)
            indexed = workspace.load_scan(self.root, NOW)
        self.assertEqual(before, after)
        for key in ('rows', 'before', 'history_fingerprint'):
            self.assertEqual(forecast[key], indexed[key], key)

    def test_sqlite_revision_invalidates_probability_cache_and_expires_kickoffs(self):
        self.migrate()
        first = workspace.load_scan(self.root, NOW)
        expired = workspace.load_scan(self.root, NOW + timedelta(seconds=30))
        self.assertEqual([row['fixture']['match_id'] for row in expired['rows']], ['100002'])
        revised = deepcopy(self.raw[0])
        revised['score']['fulltime'] = dict(home=6, away=4)
        api.save_fixtures([revised], self.path)
        again = workspace.load_scan(self.root, NOW)
        self.assertNotEqual(first['history_fingerprint'], again['history_fingerprint'])
        self.assertNotEqual(first['rows'][0]['probabilities'], again['rows'][0]['probabilities'])

    def test_both_midnights_keep_identical_probabilities(self):
        extra = [fixture(100003, stamp='2026-10-22T23:30:00+00:00'),
                 fixture(100004, stamp='2026-10-23T12:00:00+00:00')]
        api.save_fixtures(extra, self.path)
        instants = [datetime.fromisoformat(value) for value in (
            '2026-10-22T22:59:00+00:00', '2026-10-22T23:01:00+00:00', '2026-10-23T00:01:00+00:00')]
        expected = [workspace.load_scan(self.root, current) for current in instants]
        self.migrate()
        for current, original in zip(instants, expected):
            indexed = workspace.load_scan(self.root, current)
            self.assertEqual(original['rows'], indexed['rows'])
            self.assertEqual(original['before'], indexed['before'])

    def test_audit_export_uses_current_sqlite_correction_and_retains_original_json(self):
        original = self.path.read_bytes()
        self.migrate()
        revised = deepcopy(self.raw[0])
        revised['score']['fulltime'] = dict(home=6, away=4)
        api.save_fixtures([revised], self.path)
        value, digest = evaluations._snapshot(self.path)
        self.assertEqual(value['fixtures'][0], revised)
        self.assertEqual(digest, sha256(evaluations._json(value).encode()).hexdigest())
        self.assertEqual(self.path.read_bytes(), original)

    def test_export_command_never_overwrites_original(self):
        self.migrate()
        with patch.object(migration.cloud_storage, 'configured', return_value=False):
            self.assertEqual(migration.main(['--source', str(self.path), '--export', str(self.path)]), 1)
            target = self.root/'export.json'
            self.assertEqual(migration.main(['--source', str(self.path), '--export', str(target)]), 0)
        self.assertEqual(json.loads(target.read_bytes()), api.export_store(self.path))

    def test_migration_command_refuses_shared_configuration_without_creating_database(self):
        with patch.object(migration.cloud_storage, 'configured', return_value=True):
            self.assertEqual(migration.main(['--source', str(self.path), '--activate']), 1)
        self.assertIsNone(api.active_store(self.path))

    def test_filtered_legacy_ids_and_invalid_league_metadata_keep_backend_parity(self):
        valid = deepcopy(self.raw[0])
        valid['fixture']['id'] = '000000123'
        valid['league']['id'] = '000039'
        invalid = deepcopy(self.raw[1])
        invalid['league'] = []
        api.save_fixtures([valid, invalid], self.path)
        expected = api.load_fixtures(self.path, ids=['123'], leagues=['39'])
        self.assertEqual(expected, [valid])
        self.migrate()
        self.assertEqual(api.load_fixtures(self.path, ids=['123'], leagues=['39']), expected)


if __name__ == '__main__':
    unittest.main()
