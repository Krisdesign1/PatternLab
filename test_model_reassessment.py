"""Candidate isolation, immutable experiment identity and restart integrity."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import catboost_scenarios as ml
import cloud_storage
import model_campaign as campaign
import model_reassessment as reassessment
import seasonal_validation as seasonal
from test_model_campaign import fake_train
from test_seasonal_validation import sample, fake_fit, fake_predict


NOW = datetime(2026, 10, 18, 1, tzinfo=timezone.utc)


class ReassessmentTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(folder)/'provider'
        self.rows = sample()
        self.enterContext(patch.object(api, 'ApiFootballClient', side_effect=AssertionError('No API')))
        self.enterContext(patch.object(cloud_storage, 'configured', return_value=False))
        self.fit = self.enterContext(patch.object(ml, 'train', side_effect=fake_train))
        self.enterContext(patch.object(seasonal, '_fit', side_effect=fake_fit))
        self.enterContext(patch.object(seasonal, '_predict', side_effect=fake_predict))
        self.enterContext(patch.object(seasonal, 'CANDIDATES', ({'depth': 2, 'iterations': 5},)))

    def run_one(self, **kwargs):
        return reassessment.run(self.rows, '39', root=self.root, now=NOW, sizes=(2, 4), **kwargs)

    def test_readonly_plan_never_fits_writes_or_loads_model_bytes(self):
        with patch.object(ml, '_classifier', side_effect=AssertionError('No classifier')):
            planned = reassessment.plan(self.rows, '39', root=self.root, now=NOW)
        self.assertTrue(planned['ready'])
        self.assertEqual(len(planned['seasonal_folds']), 3)
        self.fit.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_candidate_and_reports_are_isolated_from_all_existing_state(self):
        paths = campaign._paths('39', self.root)
        paths['base'].mkdir(parents=True)
        paths['manifest'].write_text('original locked campaign')
        paths['input'].write_text('original input')
        fake_train(self.rows, '39', 'API-Football', self.root/'models', before='2026-10-17')
        journal = self.root/'prediction_journal.jsonl'
        protocol = self.root/'journal_protocol.json'
        journal.write_text('immutable forecasts')
        protocol.write_text('original prospective protocol')
        original = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        output = self.run_one()
        self.assertEqual(output['status'], 'completed')
        self.assertFalse(output['promoted'])
        self.assertNotEqual(output['candidate_generation'], output['incumbent_generation'])
        for path, raw in original.items():
            self.assertEqual(path.read_bytes(), raw, path)
        self.assertFalse(output['report']['policy']['replacement']['automatic'])
        self.assertEqual(output['report']['seasonal']['sizes'], [2, 4])

    def test_input_and_rules_exist_before_first_training_or_test_read(self):
        def inspect(rows, league, source, directory=None, **kwargs):
            path = Path(directory).parent
            frozen = json.loads((path/'input.json').read_text())
            manifest = json.loads((path/'manifest.json').read_text())
            self.assertEqual(manifest['status'], 'building')
            self.assertEqual(frozen['rows'], rows)
            self.assertEqual(frozen['definition']['protocol']['sizes'], [2, 4])
            self.assertFalse(frozen['definition']['protocol']['replacement']['automatic'])
            return fake_train(rows, league, source, directory, **kwargs)
        self.fit.side_effect = inspect
        output = self.run_one()
        self.assertEqual(output['status'], 'completed')

    def test_same_data_later_day_reopens_identical_completed_report_without_retuning(self):
        first = self.run_one()
        self.fit.reset_mock()
        with patch.object(seasonal, 'run', side_effect=AssertionError('No repeated test')):
            again = reassessment.run(self.rows, '39', root=self.root, now=NOW+timedelta(days=1), sizes=(4, 2))
        self.assertEqual(first, again)
        self.fit.assert_not_called()
        self.assertEqual(len(reassessment.list_runs('39', self.root)), 1)

    def test_new_match_creates_new_experiment_preserving_previous(self):
        first = self.run_one()
        previous = reassessment.load_run('39', first['run_id'], self.root)
        self.rows.append(dict(self.rows[-1], match_id='999999', timestamp='2026-10-17T12:00:00'))
        second = self.run_one()
        self.assertNotEqual(first['run_id'], second['run_id'])
        self.assertEqual(previous, reassessment.load_run('39', first['run_id'], self.root))
        self.assertEqual(len(reassessment.list_runs('39', self.root)), 2)

    def test_failure_keeps_input_and_retry_reuses_the_same_native_candidate(self):
        original = seasonal.run
        with patch.object(seasonal, 'run', side_effect=seasonal.SeasonalError('interrupted')):
            with self.assertRaisesRegex(ValueError, 'interrupted'):
                self.run_one()
        pending = reassessment.list_runs('39', self.root)[0]
        frozen = (reassessment._run_path('39', pending['run_id'], self.root)/'input.json').read_bytes()
        self.assertEqual(pending['status'], 'error')
        self.assertTrue(pending['error'])
        self.fit.reset_mock()
        with patch.object(seasonal, 'run', side_effect=original):
            result = self.run_one()
        self.assertEqual(result['run_id'], pending['run_id'])
        self.fit.assert_not_called()
        self.assertEqual(frozen, (reassessment._run_path('39', pending['run_id'], self.root)/'input.json').read_bytes())

    def test_corrupt_failed_input_cannot_be_retrained(self):
        with patch.object(seasonal, 'run', side_effect=ValueError('interrupted')):
            with self.assertRaises(ValueError): self.run_one()
        pending = reassessment.list_runs('39', self.root)[0]
        path = reassessment._run_path('39', pending['run_id'], self.root)/'input.json'
        payload = json.loads(path.read_text())
        payload['rows'][0]['home'] = 'altered'
        path.write_text(json.dumps(payload))
        self.fit.reset_mock()
        with self.assertRaisesRegex(reassessment.ReassessmentError, 'altéré'):
            self.run_one()
        self.fit.assert_not_called()

    def test_input_header_or_manifest_metadata_cannot_change_on_retry_or_reopen(self):
        result = self.run_one()
        path = reassessment._run_path('39', result['run_id'], self.root)
        for filename, field, value in [('input.json', 'cutoff', '2026-10-17'),
                                       ('manifest.json', 'frozen_at', '2020-01-01T00:00:00+00:00'),
                                       ('manifest.json', 'history_matches', 1),
                                       ('manifest.json', 'sizes', [20]),
                                       ('manifest.json', 'promoted', True)]:
            target = path/filename
            original = target.read_bytes()
            payload = json.loads(original)
            payload[field] = value
            target.write_text(json.dumps(payload))
            with self.subTest(field=field), self.assertRaises(reassessment.ReassessmentError):
                reassessment.load_run('39', result['run_id'], self.root)
            target.write_bytes(original)

    def test_notification_error_after_commit_does_not_repeat_or_replace_completed_test(self):
        def notify(fraction, message):
            if fraction == 1:
                raise RuntimeError('UI notification unavailable')
        first = self.run_one(progress=notify)
        self.assertEqual(first['status'], 'completed')
        self.fit.reset_mock()
        with patch.object(seasonal, 'run', side_effect=AssertionError('Test must not repeat')):
            second = self.run_one()
        self.assertEqual(first, second)
        self.fit.assert_not_called()

    def test_corrupt_report_or_candidate_are_rejected_when_reopened(self):
        output = self.run_one()
        path = reassessment._run_path('39', output['run_id'], self.root)
        model = ml._location('API-Football', '39', path/'candidate_models')/output['candidate_generation']/'model.cbm'
        original = model.read_bytes()
        model.write_bytes(b'changed native model')
        with self.assertRaisesRegex(reassessment.ReassessmentError, 'altéré'):
            reassessment.load_run('39', output['run_id'], self.root)
        model.write_bytes(original)
        report = json.loads((path/'report.json').read_text())
        report['promoted'] = True
        (path/'report.json').write_text(json.dumps(report))
        with self.assertRaisesRegex(reassessment.ReassessmentError, 'empreinte'):
            reassessment.load_run('39', output['run_id'], self.root)

    def test_listing_reads_only_small_manifest_without_history_or_model_load(self):
        result = self.run_one()
        with patch.object(ml, 'load_report', side_effect=AssertionError('No model load')), \
             patch.object(ml, '_classifier', side_effect=AssertionError('No native import')):
            listed = reassessment.list_runs('39', self.root)
        self.assertEqual(listed[0]['run_id'], result['run_id'])
        self.assertNotIn('report', listed[0])

    def test_current_day_bad_outcomes_and_other_providers_are_excluded(self):
        before = self.run_one()
        self.rows += [dict(self.rows[0], match_id='99888', timestamp='2026-10-18T00:00:00', value='invalid', ht=None, ft=None),
                      dict(self.rows[0], provider='football-data.org', value='invalid')]
        after = self.run_one()
        self.assertEqual(before, after)

    def test_insufficient_data_invalid_sizes_and_sources_do_not_create_experiments(self):
        for sizes in ((), (True,), (1,), (2, 2), (21,)):
            with self.subTest(sizes=sizes), self.assertRaises(ValueError):
                reassessment.run(self.rows, '39', sizes=sizes, root=self.root)
        with self.assertRaises(ValueError):
            reassessment.run(self.rows, '39', source='other', root=self.root)
        planned = reassessment.plan(self.rows[:12], '39', root=self.root)
        self.assertFalse(planned['ready'])
        with self.assertRaises(ValueError):
            reassessment.run(self.rows[:12], '39', root=self.root)
        self.assertFalse(self.root.exists())

    def test_concurrent_runs_and_shared_storage_fail_before_training(self):
        base, _ = reassessment._paths('39', self.root)
        with reassessment._lock(base):
            with self.assertRaisesRegex(reassessment.ReassessmentError, 'déjà en cours'):
                self.run_one()
        with patch.object(cloud_storage, 'configured', return_value=True):
            with self.assertRaisesRegex(reassessment.ReassessmentError, 'local'):
                self.run_one()
        self.fit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
