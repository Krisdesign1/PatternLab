"""Regression: a winning choice outside the selected engine must survive history."""
from test_table_view_support import static_frames
from copy import deepcopy
from datetime import timedelta
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import manual_analysis_store as archive
import manual_analysis_panel as panel
import manual_journal_rows as browser_rows
import manual_scenario_progress as progress
import manual_proposal_archive as proposals
import journal_browser_data as filters
import scenario_tables
from test_manual_analysis_store import payload, raw, NOW


def complete_payload():
    value = payload()
    def method(sequences, probabilities):
        return dict(version='comparison-test-v1', source='API-Football', league='39',
                    training_before=value['forecast']['before'],
                    ranking=[dict(sequence=sequence, probability=chance)
                             for sequence, chance in zip(sequences, probabilities)])
    value['comparison_methods'] = {
        'combined': method([item['sequence'] for item in value['forecast']['ranking']], (.3,.2,.1)),
        'clubs': method([['1st','1st'], ['Égal','1st'], ['2nd','1st']], (.25,.2,.1)),
        'frequency': method([['1st','1st'], ['Égal','Égal'], ['2nd','Égal']], (.2,.15,.1))}
    return value


class ManualProposalArchiveTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'manual.sqlite3'
        self.enterContext(patch.dict(os.environ, {'PATTERNLAB_STORAGE_MODE': 'local'}))

    def record(self, value=None, *, complete=True):
        return archive.record(complete_payload() if value is None else value, path=self.path,
                              clock=lambda: NOW, require_complete=complete)

    def evaluate(self, entry):
        observed = [raw(fixture, ht=(1,0), ft=(1,0)) for fixture in entry['fixtures']]
        return archive.evaluate(entry, observed, now=NOW.replace(hour=20)), observed

    def test_all_distinct_proposals_and_each_method_rank_survive_real_sqlite(self):
        value = complete_payload()
        before = deepcopy(value)
        saved = self.record(value)
        reread = archive.get(saved['id'], self.path)
        self.assertEqual(reread['version'], 'manual-analysis-v2')
        self.assertEqual(reread['comparison_methods'], value['comparison_methods'])
        coverage = proposals.coverage(reread)
        self.assertTrue(coverage['complete'])
        self.assertEqual(coverage['proposal_count'], 8)
        self.assertEqual(value, before)
        self.assertFalse(self.record(value)['created'])
        changed = deepcopy(value)
        changed['comparison_methods']['clubs']['ranking'][0]['probability'] = .26
        self.assertNotEqual(self.record(changed)['id'], saved['id'])
        self.assertEqual(len(archive.list_entries(self.path)), 2)

    def test_winner_omitted_from_selected_three_is_a_hit_in_all_views(self):
        saved = self.record()
        evaluation, observed = self.evaluate(saved)
        self.assertFalse(evaluation['top1'])
        self.assertFalse(evaluation['top3'])
        self.assertEqual(evaluation['proposal_evaluation']['state'], 'hit')
        follow = progress.project(saved, evaluation)
        self.assertEqual(len(follow['rows']), 8)
        hit = next(item for item in follow['rows'] if item['state'] == 'hit')
        self.assertEqual(hit['sequence'], ['1st','1st'])
        self.assertEqual(hit['cells']['clubs']['rank'], 1)
        self.assertEqual(hit['cells']['frequency']['rank'], 1)
        self.assertIsNone(hit['rank'])
        row = browser_rows.build_rows([saved], observed, NOW.replace(hour=20))[0]
        self.assertEqual(row['scenario_outcome']['state'], 'hit')
        self.assertEqual({match['method'] for match in row['scenario_outcome']['matches']}, {'clubs', 'frequency'})

    def test_legacy_negative_has_unknown_global_verdict_not_miss(self):
        saved = self.record(payload(), complete=False)
        original = self.path.read_bytes()
        evaluation, observed = self.evaluate(saved)
        self.assertFalse(evaluation['top3'])  # Valid only for the chosen engine.
        self.assertEqual(evaluation['proposal_evaluation']['state'], 'incomplete')
        row = browser_rows.build_rows([saved], observed, NOW.replace(hour=20))[0]
        self.assertEqual(row['journal_status'], 'settled')
        self.assertEqual(row['scenario_outcome']['state'], 'unavailable')
        self.assertEqual(row['scenario_outcome']['label'], 'Comparaison incomplète')
        self.assertEqual(filters.filter_rows([row], verdict='miss'), [])
        self.assertIn('incomplète', progress.project(saved, evaluation)['summary'])
        self.assertEqual(self.path.read_bytes(), original)

    def test_legacy_positive_remains_proven_but_outside_complete_comparison_denominator(self):
        value = payload()
        saved = self.record(value, complete=False)
        actual = saved['forecast']['ranking'][0]['sequence']
        report = proposals.evaluate(saved, actual)
        self.assertEqual(report['state'], 'hit')
        self.assertFalse(report['coverage']['complete'])

    def test_full_comparison_without_hit_can_be_a_proven_miss(self):
        saved = self.record()
        report = proposals.evaluate(saved, ['1st','Égal'])
        self.assertEqual(report['state'], 'miss')
        self.assertTrue(report['coverage']['complete'])
        self.assertFalse(report['actual_in_choices'])

    def test_cancelled_or_unverifiable_match_does_not_become_pending_or_a_loss(self):
        saved = self.record()
        for status in ('CANC', 'WO'):
            observed = [raw(fixture, status=status) for fixture in saved['fixtures']]
            report = archive.evaluate(saved, observed, now=NOW.replace(hour=20))
            self.assertEqual(report['status'], 'void')
            self.assertEqual(report['proposal_evaluation']['state'], 'void')
            self.assertIsNone(report['proposal_evaluation']['actual_in_choices'])
        observed = [raw(fixture) for fixture in saved['fixtures']]
        observed[0]['score']['halftime']['home'] = None
        report = archive.evaluate(saved, observed, now=NOW.replace(hour=20))
        self.assertEqual(report['proposal_evaluation']['state'], 'non_evaluable')
        self.assertIsNone(report['proposal_evaluation']['actual_in_choices'])

    def test_strict_capture_rejects_missing_truncated_or_mismatched_methods(self):
        cases = []
        cases.append(payload())
        missing = complete_payload(); missing['comparison_methods'].pop('clubs'); cases.append(missing)
        truncated = complete_payload(); truncated['comparison_methods']['clubs']['ranking'].pop(); cases.append(truncated)
        disagree = complete_payload(); disagree['comparison_methods']['combined']['ranking'][0]['probability'] = .29; cases.append(disagree)
        future = complete_payload(); future['comparison_methods']['clubs']['training_before'] = '2026-11-07'; cases.append(future)
        corrupt = complete_payload(); corrupt['comparison_methods']['clubs']['ranking'][0] = None; cases.append(corrupt)
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(archive.ManualAnalysisError): self.record(value)
        self.assertFalse(self.path.exists())

    def test_rehashed_tampering_or_downgrade_cannot_drop_comparison(self):
        saved = self.record()
        for mutation in ('drop', 'version', 'probability'):
            value = {key:item for key,item in deepcopy(saved).items() if key != 'created'}
            if mutation == 'drop': value.pop('comparison_methods')
            elif mutation == 'version': value['version'] = 'manual-analysis-v1'
            else: value['comparison_methods']['clubs']['ranking'][0]['probability'] /= 2
            with self.assertRaises(archive.ManualAnalysisError): archive._validate_entry(value)

    def test_late_capture_is_rejected_and_existing_legacy_payload_unchanged(self):
        legacy = self.record(payload(), complete=False)
        with sqlite3.connect(self.path) as con:
            before = con.execute('SELECT id,payload,digest FROM manual_analyses').fetchall()
        self.record()
        with sqlite3.connect(self.path) as con:
            after = con.execute('SELECT id,payload,digest FROM manual_analyses WHERE id=?',(legacy['id'],)).fetchall()
        self.assertEqual(before, after)
        with self.assertRaises(archive.ManualAnalysisError):
            archive.record(complete_payload(), path=self.path, clock=lambda: NOW + timedelta(days=1), require_complete=True)

    def test_detail_displays_complete_matrix_and_winner_without_refitting_or_saving(self):
        saved = self.record()
        evaluation, _ = self.evaluate(saved)
        with patch.object(panel.archive, 'record', side_effect=AssertionError('No new archive')), \
             patch('scenario_presentation.fit_sequences', side_effect=AssertionError('No historical reconstruction')), \
             patch.object(panel.api.ApiFootballClient, 'get', side_effect=AssertionError('No API')):
            at = AppTest.from_string('import streamlit as st\nfrom manual_analysis_panel import _detail\n_detail(st.session_state["entry"],st.session_state["result"])')
            at.session_state['entry'], at.session_state['result'] = saved, evaluation
            at.run()
        self.assertFalse(at.exception)
        matrix = scenario_tables.build_matrix(proposals.methods(saved), actual=evaluation['actual'])
        self.assertEqual(len(matrix['rows']), 8)
        table = static_frames(at)[-1]
        self.assertEqual(len(table), 8)
        self.assertTrue(any('Réalisé' in value for value in table['Suivi du scénario']))
        self.assertFalse(at.warning)

    def test_legacy_detail_explicitly_warns_without_global_failure_claim(self):
        saved = self.record(payload(), complete=False)
        evaluation, _ = self.evaluate(saved)
        at = AppTest.from_string('import streamlit as st\nfrom manual_analysis_panel import _detail\n_detail(st.session_state["entry"],st.session_state["result"])')
        at.session_state['entry'], at.session_state['result'] = saved, evaluation
        at.run()
        self.assertFalse(at.exception)
        self.assertTrue(any('Comparaison initiale incomplète' in item.value for item in at.warning))
        summary = next(item.value for item in at.dataframe if 'Suivi des scénarios' in item.value.columns)
        self.assertNotIn('Aucun scénario proposé réalisé', summary.iloc[0]['Suivi des scénarios'])


if __name__ == '__main__': unittest.main()
