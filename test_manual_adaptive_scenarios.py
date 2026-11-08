"""Manual selections preserve every qualified choice and keep Top3 as reference."""
from test_table_view_support import static_frames
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import adaptive_scenario_calibration as calibration
import adaptive_scenario_proposals as adaptive
import adaptive_scenario_selection as selector
import manual_analysis_store as archive
import manual_analysis_panel as panel
import manual_journal_rows as browser
import manual_proposal_archive as proposals
import manual_scenario_progress as progress
import prospective_models as models
import scenario_presentation as presentation
from test_manual_analysis_store import payload, raw, NOW
from test_adaptive_scenario_selection import method, brute


def input_value(count=4):
    value = payload()
    original = method('independent', 2)
    original.update(source='API-Football', league='39', training_before='2026-11-06')
    value['comparison_methods'] = {name:deepcopy(original) for name in ('frequency','clubs','combined')}
    value['forecast']['ranking'] = deepcopy(original['ranking'])
    chances = brute(original, 1e-9)
    threshold = chances[count-1]['probability']
    assert len(selector.select(original, threshold)) == count
    evidence = dict(status='qualified', threshold=threshold,
        tuning_groups=30, test_groups=20, tuning_days=3, test_days=3,
        tuning=dict(groups=30,hits=10,proposals=100,precision=.1,f05=.1),
        test=dict(groups=20,hits=10,proposals=80,precision=.125,f05=.15),
        reference_top3=dict(groups=20,hits=3,proposals=60,precision=.05,f05=.06),
        test_first_day='2026-11-01', tuning_labels_before='2026-11-01T00:00:00+00:00')
    artifact = dict(schema=calibration.VERSION, created_at=(NOW-timedelta(hours=1)).isoformat(),
                    contexts={calibration.context_key(name, original, 2):deepcopy(evidence)
                              for name in value['comparison_methods']})
    artifact['sha256'] = calibration.digest(artifact)
    value['proposal_selection'] = adaptive.legacy_capture(value['comparison_methods'], value['fixtures'], now=NOW, artifact=artifact)
    return value, artifact


def observed_for(entry, sequence):
    scores = {'1st':((1,0),(1,0)), 'Égal':((1,0),(2,0)), '2nd':((0,0),(1,0))}
    return [raw(fixture, ht=scores[label][0], ft=scores[label][1])
            for fixture,label in zip(entry['fixtures'], sequence)]


class ManualAdaptiveScenariosTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)/'manual.sqlite3'
        self.enterContext(patch.dict(os.environ, {'PATTERNLAB_STORAGE_MODE':'local'}))

    def record(self,value,current=NOW):
        return archive.record(value, path=self.path, clock=lambda:current, require_complete=True)

    def test_more_than_three_exact_choices_roundtrip_and_deduplicate_first_capture(self):
        value, artifact = input_value()
        before = deepcopy(value)
        saved = self.record(value)
        reread = archive.get(saved['id'], self.path)
        self.assertEqual(reread['version'], archive.SELECTION_VERSION)
        self.assertEqual(reread['proposal_selection'], value['proposal_selection'])
        self.assertEqual(reread['comparison_methods'], value['comparison_methods'])
        self.assertEqual(proposals.coverage(reread)['proposal_count'],4)
        self.assertTrue(proposals.coverage(reread)['complete'])
        next_value = deepcopy(value)
        next_value['proposal_selection'] = adaptive.legacy_capture(value['comparison_methods'],value['fixtures'],
                                                          now=NOW+timedelta(minutes=1), artifact=artifact)
        again = self.record(next_value,NOW+timedelta(minutes=1))
        self.assertFalse(again['created'])
        self.assertEqual(again['proposal_selection'], saved['proposal_selection'])
        self.assertEqual(len(archive.list_entries(self.path)),1)
        self.assertEqual(value,before)

    def test_one_two_and_five_choices_are_not_coerced_to_three(self):
        for count in (1,2,5):
            with self.subTest(count=count):
                value,_ = input_value(count)
                saved = self.record(value)
                self.assertEqual(proposals.coverage(saved)['proposal_count'],count)
                self.assertEqual(len(proposals.methods(saved)['combined']['reference_ranking']),3)
                self.assertEqual(len(proposals.methods(saved)['combined']['ranking']),count)

    def test_fourth_choice_winner_counts_in_detail_browser_and_all_proposals_not_top3(self):
        value,_ = input_value(4)
        saved = self.record(value)
        actual = saved['proposal_selection']['methods']['combined']['ranking'][3]['sequence']
        observed = observed_for(saved,actual)
        evaluation = archive.evaluate(saved,observed,now=NOW.replace(hour=20))
        self.assertFalse(evaluation['top3'])
        self.assertEqual(evaluation['proposal_evaluation']['state'],'hit')
        projected = progress.project(saved,evaluation)
        self.assertEqual(len(projected['rows']),4)
        self.assertEqual([row['rank'] for row in projected['rows'] if row['state']=='hit'],[4])
        row = browser.build_rows([saved],observed,NOW.replace(hour=20))[0]
        self.assertEqual(row['scenario_outcome']['state'],'hit')
        self.assertEqual({item['rank'] for item in row['scenario_outcome']['matches']},{4})
        with patch.object(adaptive,'load_artifact',side_effect=AssertionError('Never reselect saved choices')):
            at=AppTest.from_string('import streamlit as st\nfrom manual_analysis_panel import _detail\n_detail(st.session_state["entry"],st.session_state["result"])')
            at.session_state['entry'],at.session_state['result']=saved,evaluation
            at.run()
        self.assertFalse(at.exception)
        self.assertEqual(len(static_frames(at)[-1]),4)
        self.assertTrue(any('Réalisé' in cell for cell in static_frames(at)[-1]['Suivi du scénario']))

    def test_unqualified_is_archived_abstention_and_never_a_false_loss_or_top3_fallback(self):
        value,_=input_value()
        value['proposal_selection']=adaptive.legacy_capture(value['comparison_methods'],value['fixtures'],now=NOW,
                                                    artifact=dict(schema='invalid'))
        saved=self.record(value)
        pending=archive.evaluate(saved,[],now=NOW)
        self.assertEqual(pending['status'],'pending')
        self.assertEqual(pending['proposal_evaluation']['state'],'abstained')
        actual=saved['forecast']['ranking'][0]['sequence']
        observed=observed_for(saved,actual)
        evaluation=archive.evaluate(saved,observed,now=NOW.replace(hour=20))
        self.assertTrue(evaluation['top3'])  # This historical reference is unchanged.
        self.assertEqual(evaluation['proposal_evaluation']['state'],'abstained')
        self.assertIsNone(evaluation['proposal_evaluation']['actual_in_choices'])
        self.assertEqual(proposals.methods(saved),{})
        projected=progress.project(saved,evaluation)
        self.assertEqual(projected['rows'],[])
        self.assertIn('Aucun scénario proposé',projected['summary'])
        row=browser.build_rows([saved],observed,NOW.replace(hour=20))[0]
        self.assertEqual(row['scenario_outcome']['state'],'unavailable')
        self.assertEqual(row['scenario_outcome']['label'],'Aucun scénario proposé')
        self.assertIsNone(row['primary_sequence'])
        at=AppTest.from_string('import streamlit as st\nfrom manual_analysis_panel import _detail\n_detail(st.session_state["entry"],st.session_state["result"])')
        at.session_state['entry'],at.session_state['result']=saved,evaluation
        at.run()
        self.assertFalse(at.exception)
        self.assertTrue(any('Aucun scénario proposé' in item.value for item in at.info))

    def test_tampering_downgrade_late_capture_and_oversize_fail_as_whole_archives(self):
        value,_=input_value()
        for change in ('version','drop','probability','late','oversize'):
            corrupt=deepcopy(value)
            if change=='version':corrupt['version']=archive.COMPARISON_VERSION
            elif change=='drop':corrupt.pop('comparison_methods')
            elif change=='probability':corrupt['proposal_selection']['methods']['combined']['ranking'][0]['probability']*=.99
            elif change=='late':corrupt['proposal_selection']['selection_at']=(NOW+timedelta(hours=8)).isoformat()
            else:corrupt['proposal_selection']['extra']='x'*(archive.MAX_BYTES+1)
            with self.subTest(change=change),self.assertRaises(archive.ManualAnalysisError):self.record(corrupt)
            self.assertFalse(self.path.exists())
        with self.assertRaises(archive.ManualAnalysisError):self.record(value,NOW+timedelta(hours=8))
        self.assertFalse(self.path.exists())

    def test_ranking_only_learned_reference_cannot_be_used_as_selected_top3_fallback(self):
        value,artifact=input_value()
        value['engine']='catboost'
        value['comparison_methods']={'catboost':dict(version='test-source-v1',
            ranking=deepcopy(value['forecast']['ranking']))}
        value['proposal_selection']=adaptive.legacy_capture(value['comparison_methods'],value['fixtures'],now=NOW,artifact=artifact)
        saved=self.record(value)
        self.assertEqual(saved['version'],archive.SELECTION_VERSION)
        self.assertEqual(proposals.methods(saved),{})
        self.assertTrue(proposals.coverage(saved)['abstained'])

    def test_initial_matrix_and_export_payload_use_exact_archived_selection_without_refitting(self):
        value,_=input_value(4)
        saved=self.record(value)
        render_payload=dict(source=saved['source'],engine=saved['engine'],fixtures=saved['fixtures'],
            forecast=saved['forecast'],comparison_methods=saved['comparison_methods'],proposal_selection=saved['proposal_selection'])
        with patch.object(presentation,'fit_sequences',side_effect=AssertionError('No model rebuild')), \
             patch.object(adaptive,'load_artifact',side_effect=AssertionError('No reselect')):
            at=AppTest.from_string('import streamlit as st\nfrom scenario_presentation import render_analysis_matrix\nrender_analysis_matrix(st.session_state["payload"])')
            at.session_state['payload']=render_payload
            at.run()
        self.assertFalse(at.exception)
        self.assertTrue(any('4 scénario' in cell.value for cell in at.caption))


if __name__=='__main__':unittest.main()
