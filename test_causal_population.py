from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import causal_feature_workspace as features
import causal_population as population
import fixture_temporal_store as temporal
import test_causal_feature_workspace as helpers


class PopulationTests(helpers.CausalWorkspaceTests):
    # Reuse the synthetic SQLite import history fixture, not the inherited tests.
    def policy(self, **kwargs):
        return population.make_policy('2026-10-28', '2026-10-30', outcome_at=helpers.OUTCOME, **kwargs)

    def job(self):
        directory = self.root/'population'
        population.start(self.path, directory, self.policy())
        return directory

    def test_frozen_source_and_census_before_snapshots_then_resumption(self):
        self.settle(mid=100001); self.settle(mid=100002)
        directory = self.job()
        with patch.object(population, '_summary', wraps=population._summary):
            first = population.run(directory, batch_size=1)
        self.assertEqual(first['progress']['processed'], 1)
        census = json.loads((directory/'population.json').read_text())
        self.assertEqual(census['requested'], 2)
        self.assertFalse(census['selection_used_outcomes'])
        self.assertFalse((directory/'COMPLETE.json').exists())
        result_files = list((directory/'results').glob('*.json'))
        first_bytes = result_files[0].read_bytes()
        # Mutating the original source never changes the private fixed copy.
        self.save([], '2026-10-30T00:10:00+00:00')
        summary = population.run(directory, batch_size=1)
        self.assertEqual(summary['snapshots'], 2)
        self.assertEqual(summary['labelled'], 2)
        self.assertEqual(result_files[0].read_bytes(), first_bytes)
        report = population.verify(directory)
        self.assertEqual(report['summary'], summary)
        self.assertEqual(report['population']['days'][0]['candidate_count'], 0)
        self.network.assert_not_called()

    def test_census_is_independent_of_later_results_or_current_fixture_columns(self):
        before = temporal.state_as_of(self.path, '2026-10-29T08:00:00Z')
        first = population.census_day(before, '2026-10-29')
        self.settle()
        after = temporal.state_as_of(self.path, '2026-10-29T08:00:00Z')
        second = population.census_day(after, '2026-10-29')
        self.assertEqual(first['targets'], second['targets'])
        self.assertEqual([row['match_id'] for row in first['targets']], ['100001', '100002'])

    def test_duplicate_days_late_early_status_and_quarantine_are_explicit(self):
        state = temporal.state_as_of(self.path, '2026-10-29T08:00:00Z')
        result = population.census_day(state, '2026-10-29', already_selected={'100001'})
        self.assertEqual(result['candidate_count'], 1)
        self.assertIn(dict(match_id='100001', reason='fixture_already_selected_on_earlier_day'), result['exclusions'])
        changed = deepcopy(state)
        target = next(row for row in changed['selected'] if row['match_id'] == '100001')
        target['raw']['fixture']['date'] = '2026-10-29T08:00:00Z'
        result = population.census_day(changed, '2026-10-29')
        self.assertIn(dict(match_id='100001', reason='kickoff_not_after_decision'), result['exclusions'])
        target['raw']['fixture']['status']['short'] = 'PST'
        result = population.census_day(changed, '2026-10-29')
        self.assertIn(dict(match_id='100001', reason='not_NS_at_decision'), result['exclusions'])

    def test_progress_does_not_read_source_or_feature_matrices(self):
        directory = self.job()
        original = population.fingerprint
        def file_hash(path):
            if Path(path).suffix == '.sqlite3':
                raise AssertionError('No DB hashing')
            return original(path)
        with patch.object(population, 'fingerprint', side_effect=file_hash), \
                patch.object(temporal, 'state_as_of', side_effect=AssertionError('No DB read')):
            status = population.status(directory)
        self.assertEqual(status['progress']['state'], 'awaiting_census')
        self.assertFalse((directory/'population.json').exists())

    def test_modified_source_code_and_completed_files_are_refused(self):
        directory = self.job()
        with patch.object(population, 'implementation', return_value={}):
            with self.assertRaises(ValueError): population.run(directory)
        original = (directory/'source.sqlite3').read_bytes()
        (directory/'source.sqlite3').write_bytes(original+b'invalid')
        with self.assertRaises(ValueError): population.run(directory)
        (directory/'source.sqlite3').write_bytes(original)
        population.run(directory)
        (directory/'summary.json').write_text('{}')
        with self.assertRaises(ValueError): population.status(directory)

    def test_partial_final_write_resumes_without_overwriting_prior_files(self):
        directory = self.job()
        original = population._exclusive_or_equal
        calls = 0
        def interrupted(path, value):
            nonlocal calls
            original(path, value); calls += 1
            if calls == 2:
                raise InterruptedError('Synthetic interruption')
        with patch.object(population, '_exclusive_or_equal', side_effect=interrupted):
            with self.assertRaises(InterruptedError): population.run(directory)
        self.assertFalse((directory/'COMPLETE.json').exists())
        summary = population.run(directory)
        self.assertEqual(summary['snapshots'], 2)
        population.verify(directory)

    def test_changed_denominators_cannot_be_resealed_and_accepted_by_verification(self):
        directory = self.job(); population.run(directory)
        value = json.loads((directory/'population.json').read_text())
        value['requested'] = 999; value.pop('id')
        value = population._sealed(value)
        (directory/'population.json').write_text(json.dumps(value))
        with self.assertRaises(ValueError): population.verify(directory)

    def test_preparation_and_full_verification_are_distinct_and_status_shows_the_proof(self):
        directory = self.job()
        summary = population.run(directory)
        self.assertEqual(summary['state'], 'prepared')
        self.assertNotIn('verification', population.status(directory))
        population.verify(directory)
        self.assertEqual(population.status(directory)['verification']['state'], 'verified')

    def test_final_bundle_symlink_is_rejected_before_any_external_write(self):
        directory = self.job()
        outside = self.root/'outside'; outside.mkdir()
        (directory/'bundle').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError): population.run(directory)
        self.assertEqual(list(outside.iterdir()), [])

    def test_invalid_policy_and_existing_destination_are_refused(self):
        for args in [('2026-10-30', '2026-10-28'), ('2099-01-01', '2099-01-02')]:
            with self.assertRaises(ValueError): population.make_policy(*args)
        for hour in [-1, 24, True, '8']:
            with self.assertRaises(ValueError): self.policy(hour_utc=hour)
        directory = self.job()
        with self.assertRaises(FileExistsError): population.start(self.path, directory, self.policy())
        with self.assertRaises(ValueError): population.run(directory, batch_size=0)


# Inheriting the fixture must not rerun its already-tested methods here.
for _name in dir(helpers.CausalWorkspaceTests):
    if _name.startswith('test_') and _name not in PopulationTests.__dict__:
        setattr(PopulationTests, _name, None)


class FeatureBatchTests(helpers.CausalWorkspaceTests):
    def test_shared_league_history_has_exact_parity_and_one_normalization_pass(self):
        state = temporal.state_as_of(self.path, '2026-10-29T14:00:00Z')
        expected = [features.from_state(state, str(mid)) for mid in (100001, 100002)]
        preparation = features.FeaturePreparation(state)
        with patch.object(api, '_normalize_matches', wraps=api._normalize_matches) as normalize:
            first = preparation.snapshot('100001')
            calls = normalize.call_count
            second = preparation.snapshot('100002')
            self.assertEqual(normalize.call_count, calls)
        self.assertEqual([first, second], expected)
        state['selected'].clear(); first[0]['features']['league_count'] = -999
        self.assertEqual(preparation.snapshot('100001'), expected[0])


for _name in dir(helpers.CausalWorkspaceTests):
    if _name.startswith('test_') and _name not in FeatureBatchTests.__dict__:
        setattr(FeatureBatchTests, _name, None)


if __name__ == '__main__':
    unittest.main()
