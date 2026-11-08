"""The history is reachable in-place, local-only, and preserves whole groups."""
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
import tempfile
import unittest
import json
from math import prod
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import api_football as api
import admin_access
import catboost_scenarios as ml
import high_probability_history_panel as panel
import high_probability_panel as ranking
import high_probability_workspace as workspace
import prediction_journal as journal
import result_explanations as reasons
from test_high_probability_history import saved, LATER
from test_probability_scenario_store import P
from test_scenario_tables import independent


def sized_saved(size):
    entry = saved()
    entry.update(id=f'saved-group-{size}', size=size)
    fixture = entry['fixtures'][0]
    entry['fixtures'] = [dict(fixture, match_id=str(size * 1000 + index), league='39',
        home=f'Group {size} Home {index}', away=f'Group {size} Away {index}',
        timestamp=f'2026-10-22T14:{index:02}:00') for index in range(size)]
    probabilities = [deepcopy(P[index % 2]) for index in range(size)]
    sequence = ['1st' if index % 2 == 0 else '2nd' for index in range(size)]
    entry['methods'] = {'clubs': dict(independent(probabilities), source='API-Football',
        league='39', training_before='2026-10-22')}
    entry['metadata']['selection'].update(
        requested_scenario=dict(sequence=sequence,
            probability=prod(p[choice] for p, choice in zip(probabilities, sequence))),
        match_probabilities=[dict(match_id=item['match_id'], probabilities=p, search_outcome=choice)
            for item, p, choice in zip(entry['fixtures'], probabilities, sequence)])
    return entry


class HighProbabilityHistoryPanelTests(unittest.TestCase):
    def setUp(self):
        self.entries = [saved('settled')]
        self.clock = self.patch(ranking, '_now', return_value=LATER)
        self.load = self.patch(panel, '_load', side_effect=lambda *args: (deepcopy(self.entries), []))
        self.explain = self.patch(panel, 'annotate_entries', side_effect=lambda entries, **kwargs:
            [dict(entry, result_explanation=reasons.describe_entry(entry, now=LATER)) for entry in entries])
        self.scan = self.patch(workspace, 'load_scan', return_value=None)
        self.refresh = self.patch(panel.tracking, 'refresh_entry', return_value={})
        self.api = self.patch(api.ApiFootballClient, 'get', side_effect=AssertionError('No hidden API'))
        self.local = self.patch(api, 'load_fixtures', side_effect=AssertionError('No production fixture read in isolated UI tests'))
        self.patch(admin_access, 'can_admin', return_value=True)
        self.write = self.patch(journal, 'record_mixed', side_effect=AssertionError('No new forecast'))
        self.train = self.patch(ml, '_fit', side_effect=AssertionError('No training'))

    def patch(self, target, name, **kwargs):
        p = patch.object(target, name, **kwargs)
        result = p.start()
        self.addCleanup(p.stop)
        return result

    def open(self, view='groups', day=None):
        at = AppTest.from_string('from high_probability_panel import render_high_probabilities\n'
                                 'render_high_probabilities()', default_timeout=20)
        at.session_state['hp_view'] = view
        if day is not None:
            at.session_state['analysis_selected_day']=day
            at.session_state['analysis_follow_today']=False
        at.run()
        self.assertFalse(at.exception)
        return at

    def table(self, at, *, detail=False):
        frames = [node.value for node in at.dataframe if 'Verdict du choix' in node.value.columns]
        return frames[-1 if detail else 0]

    def readonly(self):
        for mock in (self.scan, self.api, self.write, self.train, self.refresh, self.local):
            mock.assert_not_called()

    def test_history_opens_without_today_calendar_and_displays_saved_percentages(self):
        before = deepcopy(self.entries)
        at = self.open()
        table = self.table(at)
        self.assertEqual(table['1st'].tolist(), [60, 20])
        self.assertEqual(table['Probabilité du choix (%)'].tolist(), [60, 50])
        self.assertEqual(table['Verdict du choix'].tolist(), [['Réalisé'], ['Réalisé']])
        self.assertEqual(table['Résultat réel'].tolist(), [['1st'], ['2nd']])
        self.assertEqual(at.selectbox(key='hp_history_day').value, 'all')
        self.assertEqual(at.selectbox(key='hp_history_page_size').value, 50)
        self.assertEqual(self.entries, before)
        self.readonly()

    def test_switch_from_empty_ranking_opens_history_without_new_scan(self):
        at = self.open('ranking')
        self.load.assert_not_called()
        self.assertTrue(any('Aucun calendrier local' in node.value for node in at.info))
        self.scan.reset_mock()
        at.segmented_control(key='hp_view').set_value('groups').run()
        self.assertFalse(at.exception)
        self.assertEqual(len(self.table(at)), 2)
        self.readonly()

    def test_match_filter_preserves_original_order_and_entire_detail_group(self):
        at = self.open()
        at.selectbox(key='hp_history_outcome').set_value('2nd').run()
        self.assertFalse(at.exception)
        table = self.table(at)
        self.assertEqual(table['Ordre'].tolist(), [2])
        self.assertEqual(len(self.table(at, detail=True)), 2)
        at.selectbox(key='hp_history_verdict').set_value('miss').run()
        self.assertFalse(at.exception)
        self.assertTrue(any('Aucun choix de match' in node.value for node in at.info))
        self.assertFalse(any('Ouvrir une analyse' == node.label for node in at.selectbox))
        self.readonly()

    def test_size_filter_matches_complete_groups_before_filtering_matches(self):
        self.entries = [sized_saved(size) for size in (2, 3, 4, 20)]
        before = deepcopy(self.entries)
        at = self.open()
        self.assertEqual(at.selectbox(key='hp_history_group_size').options,
                         ['Toutes les tailles', *[f'{size} matchs' for size in range(2, 21)]])
        for size in (2, 3, 4, 20):
            with self.subTest(size=size):
                at.selectbox(key='hp_history_group_size').set_value(size).run()
                self.assertFalse(at.exception)
                self.assertEqual(len(self.table(at)), size)
                self.assertEqual(len(self.table(at, detail=True)), size)
                self.assertEqual(at.selectbox(key='hp_history_analysis').value, f'saved-group-{size}')
        at.selectbox(key='hp_history_group_size').set_value(3).run()
        at.selectbox(key='hp_history_outcome').set_value('2nd').run()
        self.assertEqual(self.table(at)['Ordre'].tolist(), [2])
        self.assertEqual(len(self.table(at, detail=True)), 3)
        at.selectbox(key='hp_history_group_size').set_value(5).run()
        self.assertFalse(at.exception)
        self.assertTrue(any('Aucune analyse ne correspond' in node.value for node in at.info))
        self.assertFalse(at.dataframe)
        self.assertEqual(at.selectbox(key='hp_history_group_size').value, 5)
        self.assertEqual(len(at.selectbox(key='hp_history_group_size').options), 20)
        self.assertEqual(self.entries, before)
        self.readonly()

    def test_size_change_resets_history_pagination(self):
        self.entries = [dict(sized_saved(size), id=f'group-{size}-{index}')
                        for size in (2, 3) for index in range(21)]
        at = self.open()
        at.selectbox(key='hp_history_page').set_value(2).run()
        at.selectbox(key='hp_history_group_size').set_value(3).run()
        self.assertFalse(at.exception)
        self.assertEqual(at.selectbox(key='hp_history_page').value, 0)
        self.assertTrue(any('sur 63 lignes' in node.value for node in at.caption))
        self.assertEqual(len(self.table(at)), 50)
        self.readonly()

    def test_day_status_and_search_filters_do_not_create_history(self):
        at = self.open()
        at.selectbox(key='hp_history_day').set_value('2026-10-22').run()
        at.text_input(key='hp_history_search').set_value('Home 0').run()
        self.assertEqual(len(self.table(at)), 2)
        at.selectbox(key='hp_history_state').set_value('pending').run()
        self.assertTrue(any('Aucune analyse ne correspond' in node.value for node in at.info))
        at.selectbox(key='hp_history_state').set_value('all').run()
        self.assertFalse(at.exception)
        self.assertEqual(len(self.table(at)), 2)
        self.readonly()

    def test_shared_future_date_lists_pending_groups_and_manual_date_filter_survives_reruns(self):
        future=saved()
        future['id']='future-group'
        for fixture in future['fixtures']:
            fixture['timestamp']=fixture['timestamp'].replace('2026-10-22','2026-10-26')
        future['methods']['clubs']['fixtures']=deepcopy(future['fixtures'])
        self.entries.append(future)
        at=self.open(day=date(2026,10,26))
        self.assertEqual(at.selectbox(key='hp_history_day').value,'2026-10-26')
        self.assertEqual(self.table(at)['Verdict du choix'].tolist(),[['En attente'],['En attente']])
        at.selectbox(key='hp_history_day').set_value('2026-10-22').run()
        at.text_input(key='hp_history_search').set_value('Home').run()
        self.assertEqual(at.selectbox(key='hp_history_day').value,'2026-10-22')
        self.assertEqual(self.table(at)['Verdict du choix'].tolist(),[['Réalisé'],['Réalisé']])
        self.readonly()

    def test_pending_and_partial_results_are_visible_without_false_failure(self):
        self.entries = [saved('settled')]
        self.entries[0].update(status='pending', actual=None, observed=self.entries[0]['observed'][:1])
        at = self.open()
        self.assertEqual(self.table(at)['Verdict du choix'].tolist(), [['Réalisé'], ['En attente']])
        self.assertEqual([list(value) for value in self.table(at)['Résultat réel']], [['1st'], []])
        self.readonly()

    def test_match_specific_postponement_reason_preserves_partial_saved_results(self):
        self.entries[0].update(status='unresolved', actual=None,
                               observed=self.entries[0]['observed'][:1])
        second=str(self.entries[0]['fixtures'][1]['match_id'])
        before=deepcopy(self.entries)
        def explain(entries,**kwargs):
            output=deepcopy(entries)
            detail={'match_id':second,'label':'Reporté','reason':'Match reporté par le fournisseur.',
                    'state':'unresolved','raw_status':'PST'}
            for entry in output:
                entry['result_explanation']={'label':'Reporté','summary':'Une rencontre est reportée.',
                    'matches':[detail], 'blocking':[detail]}
            return output
        self.explain.side_effect=explain
        at=self.open()
        table=self.table(at)
        self.assertEqual(table['État du match'].iloc[1],'Reporté')
        self.assertIn('reporté',table['Motif'].iloc[1])
        self.assertEqual(table['Verdict du choix'].iloc[1],['Non évalué'])
        self.assertEqual(list(table['Résultat réel'].iloc[0]),['1st'])
        self.assertEqual(list(table['Résultat réel'].iloc[1]),[])
        self.assertEqual(self.entries,before)
        self.readonly()

    def test_reason_is_visible_next_to_match_in_list_and_detail(self):
        at=self.open()
        frames=[node for node in at.dataframe if 'Verdict du choix' in node.value.columns]
        self.assertEqual(len(frames),2)
        for frame in frames:
            self.assertEqual(list(frame.proto.column_order)[:5],
                ['Match','État du match','Motif','Verdict du choix','Probabilité du choix (%)'])
            self.assertEqual(json.loads(frame.proto.columns)['Motif']['width'],'large')
            self.assertTrue(json.loads(frame.proto.columns)['Match']['pinned'])
        self.assertTrue(any('pas une défaite' in node.value for node in at.caption))
        self.readonly()

    def test_empty_or_unreadable_history_is_explicit_and_never_reconstructed(self):
        self.entries = []
        at = self.open()
        self.assertTrue(any('Aucune analyse de fortes probabilités' in node.value for node in at.info))
        self.assertTrue(any('simplement consultés' in node.value for node in at.caption))
        self.assertEqual(len(at.selectbox(key='hp_history_group_size').options), 20)
        at.selectbox(key='hp_history_group_size').set_value(20).run()
        self.assertFalse(at.exception)
        self.assertFalse(at.dataframe)
        self.load.side_effect = journal.JournalError('Journal invalide')
        at.run()
        self.assertFalse(at.exception)
        self.assertTrue(any('Historique local indisponible' in node.value for node in at.error))
        self.readonly()

    def test_only_explicit_refresh_updates_results_and_invalidates_cache(self):
        at = self.open()
        self.readonly()
        at.button(key='hp_history_refresh').click().run()
        self.assertFalse(at.exception)
        self.refresh.assert_called_once()
        self.load.clear.assert_called_once()
        self.api.assert_not_called()
        self.write.assert_not_called()

    def test_many_choices_are_paginated_without_losing_any_rows(self):
        self.entries = []
        for index in range(31):
            entry = saved('settled')
            entry['id'] = f'{index:064d}'
            self.entries.append(entry)
        at = self.open()
        self.assertEqual(len(self.table(at)), 50)
        at.selectbox(key='hp_history_page').set_value(1).run()
        self.assertFalse(at.exception)
        self.assertEqual(len(self.table(at)), 12)
        at.selectbox(key='hp_history_page_size').set_value(100).run()
        self.assertEqual(at.selectbox(key='hp_history_page').value, 0)
        self.assertEqual(len(self.table(at)), 62)
        self.readonly()

    def test_recorded_group_opens_real_history_detail_in_place(self):
        self.clock.return_value = datetime(2026, 10, 22, 11, tzinfo=timezone.utc)
        entry = saved()
        rows = [dict(fixture=fixture, probabilities=p, max_probability=max(p.values()),
                     choice=max(p, key=p.get), training=120, history_days=30,
                     home_profile={'matches': 12}, away_profile={'matches': 12})
                for fixture, p in zip(entry['fixtures'], P)]
        self.scan.return_value = dict(day='2026-10-22', before='2026-10-22', rows=rows, excluded=[],
            diagnostics=dict(day_count=2, upcoming_count=2, eligible_count=2), version='test')
        self.entries = []
        self.patch(workspace, 'prepare_group', return_value=deepcopy(entry))
        self.patch(workspace, 'archive_scan', return_value=dict(created=2,existing=0,skipped=0))
        def record(prepared):
            self.entries.append(deepcopy(entry))
            return dict(entry, created=True)
        record_call = self.patch(workspace, 'record_group', side_effect=record)
        at = self.open('ranking')
        at.button(key='hp_analyze_top').click().run()
        at.button(key='hp_record').click().run()
        self.assertFalse(at.exception)
        self.assertFalse(any(node.key == 'hp_history_matches' for node in at.dataframe))
        at.button(key='hp_open_journal').click().run()
        self.assertFalse(at.exception)
        self.assertEqual(at.segmented_control(key='hp_view').value, 'groups')
        self.assertEqual(at.selectbox(key='hp_history_analysis').value, entry['id'])
        self.assertEqual(len(self.table(at, detail=True)), 2)
        self.assertEqual(self.table(at, detail=True)['Verdict du choix'].tolist(), [['En attente'], ['En attente']])
        record_call.assert_called_once()
        self.api.assert_not_called()
        self.refresh.assert_not_called()
        self.train.assert_not_called()


class HistorySourceTests(unittest.TestCase):
    def test_loading_absent_local_storage_creates_nothing(self):
        panel._load.clear()
        self.addCleanup(panel._load.clear)
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)/'absent'
            journal_path, intent_path = directory/'predictions.sqlite3', directory/'probability_scenarios.sqlite3'
            with patch.object(api.ApiFootballClient, 'get', side_effect=AssertionError('No API')):
                self.assertEqual(panel._load(str(journal_path), str(intent_path), (), (), '2026-10-25', 'test'), ([], []))
            self.assertFalse(directory.exists())


if __name__ == '__main__':
    unittest.main()
