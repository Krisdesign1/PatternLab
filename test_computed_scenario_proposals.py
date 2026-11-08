"""Production proposals survive independently of historical threshold evidence.

The recorded set is the producer's actual ranking, never the whole outcome
space and never a set recreated retrospectively after the result is known.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import adaptive_scenario_proposals as proposals
from adaptive_scenario_calibration import VERSION as CALIBRATION_VERSION, digest
import bet_simulation
import group_probability_diagnostics as diagnostics
import manual_analysis_store as manual
import manual_journal_rows as manual_rows
import manual_proposal_archive
import prediction_journal as journal
import prospective_models as models
import scenario_outcomes
import scenario_tables
from test_catboost_scenarios import sample, fixtures as official_fixtures
from test_manual_analysis_store import NOW as MANUAL_NOW, raw as manual_raw
from test_manual_proposal_archive import complete_payload
from test_prediction_journal import BEFORE, AFTER, raw_fixtures


NOW = datetime(2026, 11, 7, 9, tzinfo=timezone.utc)


def fixtures(size):
    return [dict(match_id=str(1000+i), league='39', provider='api-football',
                 home=f'Home {i}', away=f'Away {i}', home_id=str(2000+i*2),
                 away_id=str(2001+i*2), timestamp='2026-11-07T19:00:00+00:00')
            for i in range(size)]


def method(size, count=4):
    # A valid lazy independent distribution even at size 20. Its explicit
    # producer output contains four choices, not a fixture encoded cutoff.
    positions = [dict(zip(('1st', 'Égal', '2nd'), (.51, .28, .21))) for _ in range(size)]
    distribution = models._distribution('independent', size, distributions=positions)
    payload = models._method('computed-test-v1', distribution, {})
    payload['ranking'] = models._rank(models._read_distribution(distribution), count)
    return payload


def entry(count=4):
    core = {'clubs': method(2, count)}
    selected = proposals.capture(core, fixtures(2), now=NOW, artifact={})
    return dict(id='computed-test', source='API-Football', size=2,
                created_at=NOW.isoformat(), fixtures=fixtures(2), methods=core,
                proposal_selection=selected, status='settled',
                checked_at=(NOW+timedelta(days=1)).isoformat(),
                actual=deepcopy(core['clubs']['ranking'][-1]['sequence']))


class ComputedScenarioProposalTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.enterContext(patch.dict(os.environ, {'PATTERNLAB_STORAGE_MODE': 'local'}))

    def test_every_supported_group_size_keeps_all_produced_choices_without_artifact(self):
        for size in range(2, 21):
            core = {'clubs': method(size)}
            original = deepcopy(core)
            with self.subTest(size=size), \
                 patch.object(proposals, 'load_artifact', side_effect=FileNotFoundError()), \
                 patch.object(proposals, 'iter_selected', side_effect=AssertionError('No threshold selection')), \
                 patch.object(models, 'build_methods', side_effect=AssertionError('No retraining')):
                saved = proposals.capture(core, fixtures(size), now=NOW)
                proposals.validate(saved, core, fixtures(size), NOW.isoformat())
                effective = proposals.effective_methods(saved, core)
            self.assertEqual(saved['schema'], proposals.VERSION)
            self.assertEqual(saved['policy']['version'], 'all-computed-proposals-v1')
            self.assertEqual(saved['unavailable'], {})
            self.assertEqual(effective['clubs']['ranking'], core['clubs']['ranking'])
            self.assertEqual(len(effective['clubs']['ranking']), 4)
            self.assertEqual(core, original)
            self.assertLess(len(effective['clubs']['ranking']), 3**size)

    def test_absent_invalid_future_or_unqualified_evidence_never_removes_proposals(self):
        core = {'clubs': method(3)}
        empty = dict(schema=CALIBRATION_VERSION, created_at=(NOW-timedelta(hours=1)).isoformat(), contexts={})
        empty['sha256'] = digest(empty)
        future = dict(empty, created_at=(NOW+timedelta(days=1)).isoformat())
        future['sha256'] = digest({key: value for key, value in future.items() if key != 'sha256'})
        for artifact in ({}, dict(empty, sha256='invalid'), future, empty):
            with self.subTest(artifact=artifact):
                saved = proposals.capture(core, fixtures(3), now=NOW, artifact=artifact)
                self.assertEqual(saved['methods']['clubs']['ranking'], core['clubs']['ranking'])
                self.assertEqual(saved['unavailable'], {})
                self.assertIn(saved['methods']['clubs']['evidence']['status'], ('unavailable', 'not_qualified'))

    def test_fourth_choice_winner_is_identical_in_export_matrix_verdict_and_simulation(self):
        saved = entry()
        original = deepcopy(saved)
        with patch.object(proposals, 'load_artifact', side_effect=AssertionError('No policy lookup on read')), \
             patch.object(proposals, 'iter_selected', side_effect=AssertionError('No generation on read')):
            exported = json.loads(json.dumps(saved))
            effective = proposals.effective_methods(exported)
            matrix = scenario_tables.build_matrix(effective, actual=exported['actual'])
            verdict = scenario_outcomes.summarize(exported)
            ledger = bet_simulation.build([exported], now=NOW+timedelta(days=1))
        expected = saved['methods']['clubs']['ranking']
        self.assertEqual(effective['clubs']['ranking'], expected)
        self.assertEqual([list(row['sequence']) for row in matrix['rows']], [row['sequence'] for row in expected])
        self.assertEqual(verdict['state'], 'hit')
        self.assertEqual(verdict['matches'][0]['rank'], 4)
        self.assertEqual((ledger['summary']['bets'], ledger['summary']['won']), (4, 1))
        self.assertEqual(sorted(ticket['sequence'] for ticket in ledger['tickets']),
                         sorted(row['sequence'] for row in expected))
        self.assertEqual(diagnostics.inspect(effective['clubs'])['top3_probability'],
                         sum(row['probability'] for row in expected[:3]))
        self.assertEqual(saved, original)

    def test_qualified_evidence_is_copied_as_information_without_threshold_filter(self):
        from test_adaptive_scenario_journal import artifact
        core = {'clubs': method(2)}
        calibration = artifact(core, 2, 1, now=NOW)
        before = deepcopy(calibration)
        saved = proposals.capture(core, fixtures(2), now=NOW, artifact=calibration)
        self.assertEqual(saved['methods']['clubs']['ranking'], core['clubs']['ranking'])
        self.assertEqual(saved['methods']['clubs']['evidence']['status'], 'qualified')
        saved['methods']['clubs']['evidence'].clear()
        self.assertEqual(calibration, before)

    def test_read_validator_rejects_consistently_rehashed_truncation_substitution_and_late_capture(self):
        original = entry()
        core = original['methods']
        for change in ('truncate', 'duplicate', 'probability', 'late'):
            saved = deepcopy(original['proposal_selection'])
            if change == 'truncate': saved['methods']['clubs']['ranking'].pop()
            elif change == 'duplicate': saved['methods']['clubs']['ranking'][-1] = deepcopy(saved['methods']['clubs']['ranking'][0])
            elif change == 'probability': saved['methods']['clubs']['ranking'][-1]['probability'] *= .99
            else: saved['selection_at'] = (NOW+timedelta(days=1)).isoformat()
            saved['sha256'] = digest({key: value for key, value in saved.items() if key != 'sha256'})
            with self.subTest(change=change), self.assertRaises(ValueError):
                proposals.validate(saved, core, original['fixtures'])

    def test_no_after_kickoff_capture_or_duplicated_fixture_ids(self):
        for targets in (fixtures(2), [fixtures(2)[0]]*2):
            moment = NOW+timedelta(days=1) if targets[0] != targets[1] else NOW
            with self.assertRaises(ValueError):
                proposals.capture({'clubs': method(2)}, targets, now=moment, artifact={})

    def test_default_official_record_keeps_method_union_and_reference_metrics_without_qualification(self):
        targets = official_fixtures()[:2]
        with patch.object(models, '_catboost', side_effect=models.SnapshotError('absent')), \
             patch.object(models, '_context_neighbors', side_effect=models.SnapshotError('absent')):
            core = models.build_methods(sample(), targets, decision_at=BEFORE)['methods']
        choices = {tuple(item['sequence']) for value in core.values() for item in value['ranking']}
        club_choices = {tuple(item['sequence']) for item in core['clubs']['ranking']}
        actual = next(iter(sorted(choices-club_choices)))
        path = self.root/'journal.sqlite3'
        with patch.object(proposals, 'is_enabled', return_value=True), \
             patch.object(proposals, 'load_artifact', side_effect=FileNotFoundError()):
            saved = journal.record(targets, core, now=BEFORE, path=path, raw_fixtures=raw_fixtures(targets))
        self.assertEqual(saved['proposal_selection']['schema'], proposals.VERSION)
        self.assertGreater(len(choices), 3)
        raw = raw_fixtures(targets, status='FT')
        scores = {'1st': ((1,0),(1,0)), '2nd': ((0,0),(1,0)), 'Égal': ((0,0),(0,0))}
        for fixture, label in zip(raw, actual):
            ht, ft = scores[label]
            fixture['score'] = dict(halftime=dict(home=ht[0], away=ht[1]), fulltime=dict(home=ft[0], away=ft[1]))
        journal.reconcile(raw, now=AFTER, path=path)
        reread = journal.list_entries(path)[0]
        self.assertEqual(reread['methods'], core)
        self.assertFalse(reread['evaluations']['clubs']['top3'])
        self.assertEqual(scenario_outcomes.summarize(reread)['state'], 'hit')
        self.assertEqual({tuple(item['sequence']) for value in proposals.effective_methods(reread).values()
                          for item in value['ranking']}, choices)
        with patch.object(proposals, 'capture', side_effect=AssertionError('Do not replace first capture')):
            again = journal.record(targets, core, now=BEFORE, path=path, raw_fixtures=raw_fixtures(targets))
        self.assertFalse(again['created'])
        self.assertEqual(again['proposal_selection'], saved['proposal_selection'])

    def test_manual_v2_capture_keeps_every_method_and_deduplicates_first_archive(self):
        value = complete_payload()
        value['proposal_selection'] = proposals.capture(value['comparison_methods'], value['fixtures'],
                                                       now=MANUAL_NOW, artifact={})
        path = self.root/'manual.sqlite3'
        saved = manual.record(value, path=path, clock=lambda: MANUAL_NOW, require_complete=True)
        self.assertEqual(saved['proposal_selection']['schema'], proposals.VERSION)
        self.assertEqual(manual_proposal_archive.coverage(saved)['proposal_count'], 8)
        observed = [manual_raw(fixture, ht=(1,0), ft=(1,0)) for fixture in value['fixtures']]
        evaluation = manual.evaluate(saved, observed, now=MANUAL_NOW.replace(hour=20))
        self.assertFalse(evaluation['top3'])
        self.assertEqual(evaluation['proposal_evaluation']['state'], 'hit')
        self.assertEqual(manual_rows.build_rows([saved], observed, MANUAL_NOW.replace(hour=20))[0]
                         ['scenario_outcome']['state'], 'hit')
        later = deepcopy(value)
        later['proposal_selection'] = proposals.capture(value['comparison_methods'], value['fixtures'],
                                                       now=MANUAL_NOW+timedelta(minutes=1), artifact={})
        again = manual.record(later, path=path, clock=lambda: MANUAL_NOW+timedelta(minutes=1), require_complete=True)
        self.assertFalse(again['created'])
        self.assertEqual(again['proposal_selection'], saved['proposal_selection'])

    def test_manual_producer_supplied_fourth_choices_are_archived_and_evaluated(self):
        value = complete_payload()
        # The manual producer may supply additional actual choices beyond its
        # canonical historical three. The wrapper and archive must keep them.
        for method_value in value['comparison_methods'].values():
            method_value['ranking'].append(dict(sequence=['1st', 'Égal'], probability=.05))
        value['proposal_selection'] = proposals.capture(value['comparison_methods'], value['fixtures'],
                                                       now=MANUAL_NOW, artifact={})
        path = self.root/'manual-four.sqlite3'
        saved = manual.record(value, path=path, clock=lambda: MANUAL_NOW, require_complete=True)
        reread = manual.get(saved['id'], path)
        self.assertEqual(reread['comparison_methods'], value['comparison_methods'])
        effective = manual_proposal_archive.methods(reread)
        self.assertEqual({name: len(item['ranking']) for name, item in effective.items()},
                         dict(clubs=4, combined=4, frequency=4))
        observed = [manual_raw(fixture, ht=ht, ft=ft)
                    for fixture, ht, ft in zip(value['fixtures'], ((1,0),(0,0)), ((1,0),(0,0)))]
        evaluation = manual.evaluate(reread, observed, now=MANUAL_NOW.replace(hour=20))
        self.assertFalse(evaluation['top3'])
        self.assertEqual(evaluation['proposal_evaluation']['state'], 'hit')
        self.assertEqual({cell['rank'] for row in evaluation['proposal_evaluation']['rows']
                          if row['actual'] for cell in row['cells'].values()}, {4})

    def test_official_three_and_twenty_match_groups_can_be_recorded_without_evidence(self):
        for size in (3, 20):
            targets = [dict(row, timestamp=row['timestamp'].removesuffix('+00:00')) for row in fixtures(size)]
            with self.subTest(size=size), \
                 patch.object(models, '_catboost', side_effect=models.SnapshotError('absent')), \
                 patch.object(models, '_context_neighbors', side_effect=models.SnapshotError('absent')):
                core = models.build_methods(sample(max(240, size*24)), targets, decision_at=NOW)['methods']
                path = self.root/f'journal-{size}.sqlite3'
                with patch.object(proposals, 'is_enabled', return_value=True), \
                     patch.object(proposals, 'load_artifact', side_effect=FileNotFoundError()):
                    saved = journal.record(targets, core, now=NOW, path=path, raw_fixtures=raw_fixtures(targets))
                self.assertTrue(saved['created'])
                self.assertEqual(saved['proposal_selection']['unavailable'], {})
                self.assertEqual(set(proposals.effective_methods(saved)), set(core))
                self.assertEqual(journal.list_entries(path)[0]['proposal_selection'], saved['proposal_selection'])


if __name__ == '__main__':
    unittest.main()
