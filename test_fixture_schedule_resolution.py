from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import fixture_schedule_resolution as schedules
import prediction_journal as journal
import prospective_models as models
import probability_reliability as reliability
import journal_uncertainty as uncertainty
from test_prediction_journal import raw_fixtures, BEFORE, AFTER
from test_catboost_scenarios import fixtures, sample


class ScheduleResolutionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)/'journal.sqlite3'
        with patch('catboost_scenarios.load_report', return_value=None), patch.object(models, '_context_neighbors',
                side_effect=models.SnapshotError('No context')):
            methods = models.build_methods(sample(), fixtures())['methods']
        self.entry = journal.record(fixtures(), methods, now=BEFORE, path=self.path, raw_fixtures=raw_fixtures())

    def changed(self, delta=timedelta(hours=2), status='FT'):
        raw = raw_fixtures(status=status)
        original = journal._stamp(self.entry['fixtures'][0]['timestamp'])
        raw[0]['fixture']['date'] = (original+delta).isoformat()
        return raw

    def test_final_schedule_only_settles_in_recorded_order_and_appends_audit_without_rewriting(self):
        original = deepcopy(self.entry)
        raw = self.changed(timedelta(days=1))
        result = journal.reconcile(raw, now=AFTER+timedelta(days=1), path=self.path)
        self.assertEqual(result['settled'], 1)
        entry = journal.list_entries(self.path)[0]
        self.assertEqual(entry['fixtures'], original['fixtures'])
        self.assertEqual(entry['methods'], original['methods'])
        self.assertEqual(entry['actual'], ['Égal']*4)
        audit = entry['observed'][0]['schedule_resolution']
        self.assertEqual(audit['scenario_order'], 'recorded_order_unchanged')
        self.assertEqual(audit['version'], schedules.VERSION)
        self.assertEqual(reliability._result(entry, entry['fixtures'][0])['state'], 'evaluated')
        self.assertEqual(journal.reconcile(raw, now=AFTER+timedelta(days=1), path=self.path)['changed'], 0)
        raw[0]['score']['halftime'] = dict(home=0, away=0)
        journal.reconcile(raw, now=AFTER+timedelta(days=1), path=self.path)
        corrected = journal.list_entries(self.path)[0]
        self.assertEqual(len(corrected['result_history']), 2)
        self.assertEqual(corrected['actual'][0], '2nd')
        self.assertEqual(corrected['methods'], original['methods'])

    def test_identity_swaps_wrong_id_unknown_scores_and_nonfinal_changes_are_not_resolved(self):
        cases = []
        swap = self.changed(); swap[0]['teams']['home'], swap[0]['teams']['away'] = swap[0]['teams']['away'], swap[0]['teams']['home']
        cases.append(swap)
        league = self.changed(); league[0]['league']['id'] += 1; cases.append(league)
        wrong_id = self.changed(); wrong_id[0]['fixture']['id'] += 9999; cases.append(wrong_id)
        scores = self.changed(); scores[0]['score']['halftime']['home'] = None; cases.append(scores)
        cases.extend(self.changed(status=status) for status in ('NS','PST','CANC','HT'))
        for raw in cases:
            with self.subTest(raw=raw[0]['fixture']):
                journal.reconcile(raw, now=AFTER, path=self.path)
                expected = 'void' if raw[0]['fixture']['status']['short'] == 'CANC' else 'unresolved'
                self.assertEqual(journal.list_entries(self.path)[0]['status'], expected)

    def test_advance_before_forecast_or_future_announced_result_is_not_resolved(self):
        for delta, now in ((timedelta(days=-2),AFTER), (timedelta(days=4),AFTER)):
            journal.reconcile(self.changed(delta), now=now, path=self.path)
            self.assertEqual(journal.list_entries(self.path)[0]['status'], 'unresolved')

    def test_advancing_to_day_covered_by_training_cutoff_is_rejected(self):
        fixture = self.entry['fixtures'][0]
        actual = dict(fixture, timestamp='2025-03-19T15:00:00')
        with self.assertRaises(ValueError):
            schedules.resolve(self.entry, fixture, actual, status='FT', checked_at=AFTER.isoformat())

    def test_tampered_audit_cannot_enter_reliability_and_dependencies_cover_effective_week(self):
        journal.reconcile(self.changed(timedelta(days=10)), now=AFTER+timedelta(days=11), path=self.path)
        entry = journal.list_entries(self.path)[0]
        self.assertEqual(entry['status'], 'settled')
        metadata = uncertainty._metadata(entry, journal._stamp(entry['created_at']))
        self.assertGreater(len(metadata['weeks']), 1)
        original_week = uncertainty._week(journal._stamp(entry['fixtures'][0]['timestamp']))
        self.assertIn(original_week, metadata['weeks'])
        entry['observed'][0]['schedule_resolution']['home_id'] = '999'
        self.assertEqual(reliability._result(entry, entry['fixtures'][0])['state'], 'excluded')
        with self.assertRaises(ValueError):
            uncertainty._metadata(entry, journal._stamp(entry['created_at']))


if __name__ == '__main__':
    unittest.main()
