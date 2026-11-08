"""Mixed manual groups preserve exact competition provenance in real SQLite."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from itertools import product
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import catboost_scenarios as ml
import cloud_storage
from league_model import VALUES
import manual_analysis_store as archive
import manual_mixed_analysis as mixed
import manual_proposal_archive as proposals
import prospective_models as models
from test_manual_analysis_store import raw
from test_mixed_models import mixed_fixtures, mixed_history


NOW = datetime(2025, 3, 20, 9, tzinfo=timezone.utc)
AFTER = datetime(2025, 3, 20, 20, tzinfo=timezone.utc)


def payload(rows=None, fixtures=None, *, decision_at=NOW):
    built = mixed.build(mixed_history() if rows is None else rows,
                        mixed_fixtures() if fixtures is None else fixtures,
                        decision_at=decision_at)
    result = {key: built[key] for key in (
        'source', 'engine', 'fixtures', 'forecast', 'comparison_methods',
        'history_cutoff_exclusive', 'model_generation')}
    result['competition'] = dict(name='Compétitions multiples', competitions={
        '39': dict(name='Premier League', country='England'),
        '61': dict(name='Ligue 1', country='France')})
    return result


class MixedManualArchiveTests(unittest.TestCase):
    def setUp(self):
        options = patch.object(cloud_storage, '_options', return_value={'PATTERNLAB_STORAGE_MODE': 'local'})
        options.start(); self.addCleanup(options.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'manual.sqlite3'
        for task in (patch.dict(os.environ, {'PATTERNLAB_STORAGE_MODE': 'local'}),
                     patch.object(ml, 'load_report', return_value=None),
                     patch.object(models, '_context_neighbors',
                                  side_effect=models.SnapshotError('Configuration absente.')),
                     patch.object(archive.api, 'urlopen', side_effect=AssertionError('No network'))):
            self.enterContext(task)

    def record(self, value, *, current=NOW):
        return archive.record(value, path=self.path, clock=lambda: current, require_complete=True)

    def test_backend_payload_round_trips_all_methods_distributions_and_metadata(self):
        value = payload()
        original = deepcopy(value)
        saved = self.record(value)
        reread = archive.get(saved['id'], self.path)
        self.assertEqual(reread['engine'], 'mixed_clubs')
        self.assertEqual(reread['comparison_methods'], value['comparison_methods'])
        self.assertEqual(reread['forecast'], value['forecast'])
        self.assertEqual(reread['fixtures'], value['fixtures'])
        self.assertEqual(reread['competition'], value['competition'])
        self.assertEqual(set(reread['comparison_methods']), {'clubs', 'frequency', 'combined'})
        coverage = proposals.coverage(reread)
        self.assertTrue(coverage['complete'])
        self.assertEqual(coverage['selected_method'], 'clubs')
        self.assertEqual(value, original)
        # Repeated clicks reuse the immutable original; the next day's reads
        # neither fit current models nor request updated provider data.
        self.assertFalse(self.record(value, current=NOW + timedelta(minutes=1))['created'])
        with patch.object(mixed, 'build', side_effect=AssertionError('No retrospective fit')):
            self.assertEqual(archive.list_entries(self.path)[0], reread)
        self.assertEqual(archive.days(self.path), {'2025-03-20': 1})

    def test_singleton_competitions_and_same_competition_remain_supported(self):
        for single in (False, True):
            fixtures = mixed_fixtures(3 if single else 2)
            if single:
                for fixture in fixtures:
                    fixture['league'] = '39'
            value = payload(mixed_history(40), fixtures)
            saved = self.record(value)
            self.assertEqual(saved['forecast']['leagues'], ['39'] if single else ['39', '61'])
            self.assertEqual(set(saved['comparison_methods']), {'clubs'})
            self.assertTrue(proposals.coverage(saved)['complete'])
        self.assertEqual(len(archive.list_entries(self.path)), 2)

    def test_twenty_match_archive_has_every_ordered_position_without_group_threshold(self):
        value = payload(mixed_history(30), mixed_fixtures(20))
        saved = self.record(value)
        self.assertEqual(len(saved['fixtures']), 20)
        clubs = saved['comparison_methods']['clubs']
        self.assertEqual(clubs['ordered_match_ids'], [row['match_id'] for row in saved['fixtures']])
        self.assertEqual(clubs['distribution']['size'], 20)
        self.assertEqual(saved['comparison_methods'], value['comparison_methods'])

    def test_every_saved_proposal_beyond_three_and_its_winning_verdict_survives(self):
        value = payload()
        method = value['comparison_methods']['clubs']
        canonical = deepcopy(method['ranking'])
        ranked = sorted((dict(sequence=list(seq), probability=models.probability_for_sequence(method, seq))
                         for seq in product(VALUES, repeat=4)),
                        key=lambda row: (-row['probability'], tuple(row['sequence'])))
        self.assertEqual(ranked[:3], canonical)
        # Full ranking is deliberately larger than the selected model's three
        # canonical references. Archive readers must never truncate it.
        method['ranking'] = ranked
        saved = self.record(value)
        reread = archive.get(saved['id'], self.path)
        self.assertEqual(len(reread['comparison_methods']['clubs']['ranking']), 81)
        self.assertEqual(reread['forecast']['ranking'], canonical)
        actual = ranked[3]['sequence']
        observation = {'1st': ((1, 0), (1, 0)), 'Égal': ((1, 0), (2, 0)),
                       '2nd': ((0, 0), (1, 0))}
        raw_results = [raw(fixture, ht=observation[label][0], ft=observation[label][1])
                       for fixture, label in zip(reread['fixtures'], actual)]
        report = archive.evaluate(reread, raw_results, now=AFTER)
        self.assertEqual(report['status'], 'settled')
        self.assertEqual(report['actual'], actual)
        self.assertFalse(report['top3'])
        self.assertEqual(report['proposal_evaluation']['state'], 'hit')
        hit = next(row for row in report['proposal_evaluation']['rows'] if row['actual'])
        self.assertEqual(hit['cells']['clubs']['rank'], 4)
        self.assertEqual(reread['comparison_methods'], value['comparison_methods'])

    def test_scores_are_bound_to_each_archived_competition_and_club(self):
        saved = self.record(payload())
        for issue in ('league', 'home', 'duplicate'):
            observations = [raw(fixture) for fixture in saved['fixtures']]
            if issue == 'league':
                observations[0]['league']['id'] = 140
            elif issue == 'home':
                observations[0]['teams']['home']['id'] = 99999
            else:
                observations.append(deepcopy(observations[0]))
            with self.subTest(issue=issue):
                result = archive.evaluate(saved, observations, now=AFTER)
                self.assertEqual(result['status'], 'non_evaluable')
                self.assertIsNone(result['actual'])
                self.assertIsNone(result['proposal_evaluation']['actual_in_choices'])

    def test_provenance_distribution_and_decision_tampering_are_rejected_before_write(self):
        original = payload()
        for issue in ('cutoff', 'indices', 'index_out_of_range', 'fixture', 'leagues',
                      'binding', 'fingerprint', 'decision_missing', 'decision_future',
                      'distribution_missing', 'forecast_leagues', 'forecast_independence', 'comparison_missing'):
            value = deepcopy(original)
            method = value['comparison_methods']['clubs']
            if issue == 'cutoff':
                method['distribution']['components'][1]['method']['training_before'] = '2025-03-21'
            elif issue == 'indices':
                method['distribution']['components'][0]['league'] = '61'
            elif issue == 'index_out_of_range':
                method['distribution']['components'][0]['indices'] = [0, 99]
            elif issue == 'fixture':
                method['fixtures'][0]['home_id'] = '99999'
            elif issue == 'leagues':
                method['leagues'] = ['39', '61', '140']
            elif issue == 'binding':
                method['ordered_match_ids'].reverse()
            elif issue == 'fingerprint':
                method['fixture_fingerprint'] = '0' * 64
            elif issue == 'decision_missing':
                method.pop('decision_at')
            elif issue == 'decision_future':
                method['decision_at'] = (NOW + timedelta(minutes=1)).isoformat()
            elif issue == 'distribution_missing':
                method.pop('distribution')
            elif issue == 'forecast_leagues':
                value['forecast']['leagues'] = ['39']
            elif issue == 'forecast_independence':
                value['forecast']['assumes_block_independence'] = False
            else:
                value.pop('comparison_methods')
            with self.subTest(issue=issue), self.assertRaises(archive.ManualAnalysisError):
                self.record(value)
            self.assertFalse(self.path.exists())

    def test_mixed_fixtures_cannot_enter_an_ordinary_engine_even_without_declared_league(self):
        for league in ('mixed', '39', None):
            value = payload()
            value['engine'] = 'statistical'
            if league is None:
                value['forecast'].pop('league')
            else:
                value['forecast']['league'] = league
            with self.subTest(league=league), self.assertRaises(archive.ManualAnalysisError):
                self.record(value)
            self.assertFalse(self.path.exists())

    def test_tomorrow_capture_keeps_actual_exclusive_cutoff_and_rejects_late_save(self):
        current = NOW - timedelta(days=1)
        value = payload(decision_at=current)
        saved = self.record(value, current=current)
        self.assertEqual(saved['day'], '2025-03-20')
        self.assertEqual(saved['history_cutoff_exclusive'], '2025-03-19')
        self.assertTrue(saved['created_at'].startswith('2025-03-19T09:00:00'))
        with self.assertRaises(archive.ManualAnalysisError):
            self.record(value, current=AFTER)


if __name__ == '__main__':
    unittest.main()
