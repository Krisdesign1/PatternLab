"""Differential daily planning/inference using disposable canonical stores."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import daily_analysis_engine as engine
import daily_analysis_worker as original
import daily_analysis_worker_io as scoped
import fixture_store
import prediction_journal as journal
from test_daily_analysis_engine import history, future, NOW


class DailyScopedIOTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root/'fixtures.json'

    def seed(self, values, sqlite=True):
        api.save_fixtures(values, self.path)
        if sqlite: fixture_store.migrate_json(self.path)

    def test_plan_exactly_matches_reference_for_sizes_mixed_roles_statuses_and_existing(self):
        self.seed(history(60,39)+history(60,61)+future(23,(39,61)))
        raw = api.load_fixtures(self.path)
        expected = engine.plan(raw, now=NOW)
        actual, calendar = scoped.plan_store(self.path, now=NOW)
        self.assertEqual(actual, expected)
        self.assertEqual(len(calendar), 23)
        self.assertEqual({row['size'] for row in actual['candidates']}, set(range(2,21)))
        existing = actual['candidates'][:4]
        self.assertEqual(scoped.plan_store(self.path, now=NOW, existing=existing)[0],
                         engine.plan(raw, now=NOW, existing=existing))

    def test_global_diagnostics_and_tomorrow_cutoff_remain_identical(self):
        invalid = history(1,61,first_id=80000)[0]
        invalid['score']['halftime'] = None
        bad_date = deepcopy(invalid); bad_date['fixture']['id'] += 1
        bad_date['fixture']['date'] = 'bad'
        values = history()+future(4)+[invalid,bad_date]
        tomorrow = future(2, now=NOW+timedelta(days=1))
        for row in tomorrow: row['fixture']['id'] += 100
        values += tomorrow
        self.seed(values)
        raw = api.load_fixtures(self.path)
        for day in (None, NOW.date()+timedelta(days=1)):
            with self.subTest(day=day):
                self.assertEqual(scoped.plan_store(self.path, now=NOW, day=day)[0],
                                 engine.plan(raw, now=NOW, day=day))

    def test_first_plan_streams_without_full_raw_load_and_legacy_json_matches(self):
        self.seed(history()+future(4), sqlite=False)
        expected = engine.plan(api.load_fixtures(self.path), now=NOW)
        self.assertEqual(scoped.plan_store(self.path,now=NOW)[0], expected)
        fixture_store.migrate_json(self.path)
        with patch.object(api,'load_fixtures',side_effect=AssertionError('Full raw load')):
            self.assertEqual(scoped.plan_store(self.path,now=NOW)[0], expected)

    def test_two_passes_share_snapshot_during_concurrent_import(self):
        self.seed(history()+future(4))
        expected = engine.plan(api.load_fixtures(self.path),now=NOW)
        stream, calls = scoped._canonical_rows, []
        def concurrent(connection, diagnostics=None):
            yield from stream(connection,diagnostics)
            if diagnostics is not None:
                changed = history(1)[0]
                changed['fixture']['status']['short'] = 'CANC'
                api.save_fixtures([changed],self.path)
                calls.append(True)
        with patch.object(scoped,'_canonical_rows',side_effect=concurrent):
            self.assertEqual(scoped.plan_store(self.path,now=NOW)[0],expected)
        self.assertEqual(calls,[True])
        self.assertNotEqual(engine.plan(api.load_fixtures(self.path),now=NOW),expected)

    def test_real_frozen_methods_preflight_journal_and_resume_equal_reference(self):
        outputs = []
        with ExitStack() as stack:
            stack.enter_context(patch.object(api,'load_key',side_effect=AssertionError('No credentials')))
            stack.enter_context(patch.object(api.ApiFootballClient,'get',side_effect=AssertionError('No network')))
            import catboost_scenarios as ml
            import prospective_models as models
            stack.enter_context(patch.object(ml,'load_report',return_value=None))
            stack.enter_context(patch.object(models,'_context_neighbors',side_effect=models.SnapshotError('Absent')))
            for worker, name in ((original,'original'),(scoped,'scoped')):
                root = self.root/name
                values = history()+future(4)
                api.save_fixtures(values,root/'fixtures.json')
                fixture_store.migrate_json(root/'fixtures.json')
                calls=[]
                def fetch(**params):
                    calls.append(params)
                    return api.load_fixtures(root/'fixtures.json',ids=params['ids'].split('-'))
                reports=[worker.run(root,lambda:NOW,fetch),worker.run(root,lambda:NOW,fetch)]
                outputs.append(dict(reports=reports,calls=calls,
                    state=json.loads((root/original.STATE_NAME).read_text()),
                    entries=journal.list_entries(root/'predictions.sqlite3')))
        self.assertEqual(outputs[0],outputs[1])
        self.assertGreater(outputs[1]['reports'][0]['created'],0)
        self.assertEqual(outputs[1]['reports'][1]['created'],0)


if __name__ == '__main__': unittest.main()
