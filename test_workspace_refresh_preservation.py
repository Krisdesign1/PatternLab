"""Score refreshes preserve user work without recomputing frozen predictions.

Real Streamlit widget/session behavior with the existing isolated fixture and
archive setup. All network clients are mocked to reject unexpected requests.
"""
import ast
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_scenario_workspace_panel as fixture_tests


class WorkspaceRefreshPreservationTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture_tests.ScenarioWorkspacePanelTests(
            'test_real_forecast_uses_ordered_score_free_fixtures_and_persists_on_rerun')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        original_snapshot = self.case._snapshot

        def with_raw_matches(day, signature, source):
            snapshot = original_snapshot(day, signature, source)
            raw = []
            for row in snapshot['fixtures']:
                competition = snapshot['competitions'][row['league']]
                finished = row['status'] == 'FINISHED'
                raw.append({
                    'fixture': {'id': int(row['match_id']),
                                'date': row['timestamp'] + '+00:00',
                                'status': {'short': 'FT' if finished else 'NS',
                                           'elapsed': 90 if finished else None}},
                    'league': {'id': int(row['league']), 'name': competition['name'],
                               'country': competition['country'], 'season': 2026},
                    'teams': {'home': {'id': int(row['match_id']) * 2, 'name': row['home']},
                              'away': {'id': int(row['match_id']) * 2 + 1, 'name': row['away']}},
                    'goals': {'home': 1 if finished else None, 'away': 0 if finished else None},
                    'score': {'halftime': {'home': 0 if finished else None,
                                           'away': 0 if finished else None},
                              'fulltime': {'home': 1 if finished else None,
                                           'away': 0 if finished else None}},
                    'provider': 'api-football',
                })
            return dict(snapshot, matches=raw)

        self.case.snapshot.side_effect = with_raw_matches
        self.case.at.run()
        self.assertFalse(self.case.at.exception)

    def forecast(self):
        self.case._select(101, 102)
        self.case._predict()
        self.assertEqual(len(self.case._scenario_metrics()), 1)
        return deepcopy(self.case.at.session_state['workspace_forecast'])

    def assert_frozen(self, expected):
        case = self.case
        self.assertFalse(case.at.exception)
        self.assertEqual(case.at.session_state['workspace_forecast'], expected)
        self.assertEqual(len(case._scenario_metrics()), 1)
        case.estimate.assert_called_once()
        case.manual_save.assert_called_once()
        case.journal_register.assert_called_once()
        case._assert_idle_network()

    def test_background_data_revision_keeps_exact_scenarios_without_new_inference_or_archive(self):
        before = self.forecast()
        self.case.signature.return_value = (str(self.case.root), 2, 2)
        self.case.at.run()
        self.assert_frozen(before)
        self.assertTrue(self.case.at.checkbox(key=fixture_tests.match_key(101)).value)
        self.assertTrue(self.case.at.checkbox(key=fixture_tests.match_key(102)).value)

    def test_search_filter_hides_cards_without_clearing_the_selected_group(self):
        case = self.case
        case._select(101, 102)
        case.at.text_input(key='calendar_query').set_value('no-such-football-team').run()
        self.assertFalse(case.at.exception)
        self.assertFalse(any(item.key in (fixture_tests.match_key(101), fixture_tests.match_key(102))
                             for item in case.at.checkbox))
        self.assertFalse(case.at.button(key='workspace_predict').disabled)
        case.at.text_input(key='calendar_query').set_value('').run()
        self.assertTrue(case.at.checkbox(key=fixture_tests.match_key(101)).value)
        self.assertTrue(case.at.checkbox(key=fixture_tests.match_key(102)).value)
        case.estimate.assert_not_called()
        case.manual_save.assert_not_called()
        case._assert_idle_network()

    def test_calendar_status_and_query_do_not_hide_an_existing_frozen_result(self):
        before = self.forecast()
        case = self.case
        case.at.segmented_control(key='calendar_status').set_value('Terminés').run()
        self.assertFalse(any(item.key in (fixture_tests.match_key(101), fixture_tests.match_key(102))
                             for item in case.at.checkbox))
        self.assert_frozen(before)
        case.at.text_input(key='calendar_query').set_value('no-such-football-team').run()
        self.assert_frozen(before)
        case.at.segmented_control(key='calendar_status').set_value('Tous').run()
        case.at.text_input(key='calendar_query').set_value('').run()
        self.assertTrue(case.at.checkbox(key=fixture_tests.match_key(101)).value)
        self.assertTrue(case.at.checkbox(key=fixture_tests.match_key(102)).value)
        self.assert_frozen(before)

    def test_started_selected_matches_keep_the_frozen_forecast_but_prevent_new_prediction(self):
        before = self.forecast()
        self.case.now = fixture_tests.NOW.replace(hour=16)
        self.case.at.run()
        self.assert_frozen(before)
        self.assertTrue(self.case.at.button(key='workspace_predict').disabled)

    def test_added_fixture_and_changed_competition_counts_preserve_user_work(self):
        before = self.forecast()
        self.case.extra_match = True
        self.case.signature.return_value = (str(self.case.root), 2, 2)
        self.case.at.run()
        self.assertEqual(self.case.at.selectbox(key=fixture_tests.league_key()).value, '39')
        self.assertTrue(self.case.at.checkbox(key=fixture_tests.match_key(101)).value)
        self.assertTrue(self.case.at.checkbox(key=fixture_tests.match_key(102)).value)
        self.assert_frozen(before)

    def test_temporary_empty_calendar_does_not_discard_or_hide_saved_scenarios(self):
        before = self.forecast()
        case = self.case
        original_snapshot = case.snapshot.side_effect
        def empty(day, signature, source):
            value = original_snapshot(day, signature, source)
            return dict(value, fixtures=[], matches=[], competitions={}, day_count=0)
        case.snapshot.side_effect = empty
        case.signature.return_value = (str(case.root), 2, 2)
        case.at.run()
        self.assert_frozen(before)
        case.snapshot.side_effect = original_snapshot
        case.signature.return_value = (str(case.root), 3, 3)
        case.at.run()
        self.assertEqual(case.at.selectbox(key=fixture_tests.league_key()).value, '39')
        self.assertTrue(case.at.checkbox(key=fixture_tests.match_key(101)).value)
        self.assertTrue(case.at.checkbox(key=fixture_tests.match_key(102)).value)
        self.assert_frozen(before)

    def test_temporarily_missing_fixture_keeps_the_group_and_frozen_result_until_it_returns(self):
        before = self.forecast()
        case = self.case
        original_snapshot = case.snapshot.side_effect

        def missing_one(day, signature, source):
            snapshot = original_snapshot(day, signature, source)
            return dict(snapshot,
                        fixtures=[row for row in snapshot['fixtures'] if row['match_id'] != '101'],
                        matches=[row for row in snapshot['matches'] if row['fixture']['id'] != 101])

        case.snapshot.side_effect = missing_one
        case.signature.return_value = (str(case.root), 2, 2)
        case.at.run()
        self.assertFalse(any(item.key == fixture_tests.match_key(101) for item in case.at.checkbox))
        self.assert_frozen(before)
        self.assertTrue(case.at.button(key='workspace_predict').disabled)
        case.snapshot.side_effect = original_snapshot
        case.signature.return_value = (str(case.root), 3, 3)
        case.at.run()
        self.assertTrue(case.at.checkbox(key=fixture_tests.match_key(101)).value)
        self.assertTrue(case.at.checkbox(key=fixture_tests.match_key(102)).value)
        self.assertFalse(case.at.button(key='workspace_predict').disabled)
        self.assert_frozen(before)

    def test_temporarily_missing_competition_restores_its_selection_when_it_returns(self):
        before = self.forecast()
        case = self.case
        original_snapshot = case.snapshot.side_effect

        def without_selected_competition(day, signature, source):
            snapshot = original_snapshot(day, signature, source)
            return dict(snapshot,
                        fixtures=[row for row in snapshot['fixtures'] if row['league'] != '39'],
                        matches=[row for row in snapshot['matches'] if row['league']['id'] != 39],
                        competitions={key: info for key, info in snapshot['competitions'].items()
                                      if key != '39'})

        case.snapshot.side_effect = without_selected_competition
        case.signature.return_value = (str(case.root), 2, 2)
        case.at.run()
        self.assertFalse(case.at.exception)
        self.assertEqual(case.at.session_state['workspace_forecast'], before)
        case.snapshot.side_effect = original_snapshot
        case.signature.return_value = (str(case.root), 3, 3)
        case.at.run()
        self.assertEqual(case.at.selectbox(key=fixture_tests.league_key()).value, '39')
        self.assertTrue(case.at.checkbox(key=fixture_tests.match_key(101)).value)
        self.assertTrue(case.at.checkbox(key=fixture_tests.match_key(102)).value)
        self.assert_frozen(before)

    def test_explicit_new_competition_wins_when_the_previous_competition_returns(self):
        before = self.forecast()
        case = self.case
        original_snapshot = case.snapshot.side_effect

        def without_selected_competition(day, signature, source):
            snapshot = original_snapshot(day, signature, source)
            return dict(snapshot,
                        fixtures=[row for row in snapshot['fixtures'] if row['league'] != '39'],
                        matches=[row for row in snapshot['matches'] if row['league']['id'] != 39],
                        competitions={key: info for key, info in snapshot['competitions'].items()
                                      if key != '39'})

        case.snapshot.side_effect = without_selected_competition
        case.signature.return_value = (str(case.root), 2, 2)
        case.at.run()
        case.at.selectbox(key=fixture_tests.league_key()).set_value('61').run()
        self.assertFalse(case.at.exception)
        self.assertFalse(case._scenario_metrics())
        case.snapshot.side_effect = original_snapshot
        case.signature.return_value = (str(case.root), 3, 3)
        case.at.run()
        self.assertFalse(case.at.exception)
        self.assertEqual(case.at.selectbox(key=fixture_tests.league_key()).value, '61')
        self.assertFalse(case._scenario_metrics())
        self.assertEqual(case.at.session_state['workspace_forecast'], before)
        case.estimate.assert_called_once()
        case.manual_save.assert_called_once()
        case.journal_register.assert_called_once()
        case._assert_idle_network()

    def test_changed_kickoff_order_preserves_the_original_frozen_sequence(self):
        before = self.forecast()
        case = self.case
        original_snapshot = case.snapshot.side_effect

        def rescheduled(day, signature, source):
            snapshot = original_snapshot(day, signature, source)
            for row in snapshot['fixtures']:
                if row['match_id'] == '102':
                    row['timestamp'] = day.isoformat() + 'T12:00:00'
            for row in snapshot['matches']:
                if row['fixture']['id'] == 102:
                    row['fixture']['date'] = day.isoformat() + 'T12:00:00+00:00'
            return snapshot

        case.snapshot.side_effect = rescheduled
        case.signature.return_value = (str(case.root), 2, 2)
        case.at.run()
        self.assert_frozen(before)
        self.assertEqual([row['match_id'] for row in before['fixtures']], ['101', '102'])
        self.assertEqual([row['match_id'] for row in case.at.session_state['workspace_forecast']['fixtures']],
                         ['101', '102'])

    def test_changing_the_actual_group_or_engine_does_not_relabel_an_old_forecast(self):
        before = self.forecast()
        case = self.case
        case._select(103)
        self.assertFalse(case._scenario_metrics())
        self.assertEqual(case.at.session_state['workspace_forecast'], before)
        case.at.checkbox(key=fixture_tests.match_key(103)).uncheck().run()
        self.assert_frozen(before)
        case.at.selectbox(key='workspace_engine_API-Football_39').set_value('catboost').run()
        self.assertFalse(case.at.exception)
        self.assertFalse(case._scenario_metrics())
        case.estimate.assert_called_once()
        case.manual_save.assert_called_once()
        case._assert_idle_network()

    def test_empty_calendar_does_not_revive_a_forecast_for_a_deliberately_changed_group(self):
        before = self.forecast()
        case = self.case
        case._select(103)
        self.assertFalse(case._scenario_metrics())
        original_snapshot = case.snapshot.side_effect

        def empty(day, signature, source):
            return dict(original_snapshot(day, signature, source),
                        fixtures=[], matches=[], competitions={}, day_count=0)

        case.snapshot.side_effect = empty
        case.signature.return_value = (str(case.root), 2, 2)
        case.at.run()
        self.assertFalse(case.at.exception)
        self.assertFalse(case._scenario_metrics())
        self.assertEqual(case.at.session_state['workspace_forecast'], before)
        case.estimate.assert_called_once()
        case.manual_save.assert_called_once()
        case.journal_register.assert_called_once()
        case._assert_idle_network()

    def test_navigation_keeps_the_draft_and_its_competition(self):
        before = self.forecast()
        case = self.case
        case.at.switch_page('ui_pages/explore.py').run()
        self.assertFalse(case.at.exception)
        case.at.switch_page('ui_pages/analyze.py').run()
        self.assertEqual(case.at.selectbox(key=fixture_tests.league_key()).value, '39')
        self.assertTrue(case.at.checkbox(key=fixture_tests.match_key(101)).value)
        self.assertTrue(case.at.checkbox(key=fixture_tests.match_key(102)).value)
        self.assert_frozen(before)


class WorkspaceRefreshTimerTests(unittest.TestCase):
    def test_only_opted_in_live_workspace_has_a_periodic_rerun(self):
        tree = ast.parse(Path(fixture_tests.panel.__file__).read_text())
        definitions = {node.name:node for node in tree.body if isinstance(node, ast.FunctionDef)}
        def fragment(name):
            return [node for node in definitions[name].decorator_list
                    if isinstance(node, (ast.Call, ast.Attribute))
                    and ((isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                          and node.func.attr == 'fragment')
                         or isinstance(node, ast.Attribute) and node.attr == 'fragment')]
        manual = fragment('_manual_workspace')
        live = fragment('_live_workspace')
        self.assertEqual(len(manual), 1)
        self.assertFalse(isinstance(manual[0], ast.Call) and
                         any(item.arg == 'run_every' for item in manual[0].keywords))
        self.assertEqual(len(live), 1)
        self.assertIsInstance(live[0], ast.Call)
        self.assertEqual(next(ast.literal_eval(item.value) for item in live[0].keywords
                              if item.arg == 'run_every'), 60)


class WorkspaceReadinessScopeTests(unittest.TestCase):
    def test_readiness_reads_only_the_selected_competition_and_caches_its_revision(self):
        read = fixture_tests.CACHED_READINESS_HISTORY
        read.clear()
        self.addCleanup(read.clear)
        provider = SimpleNamespace(
            STORE_PATH=Path('/isolated/workspace-refresh-fixtures.json'),
            load_matches=Mock(return_value=([{'match_id': 'one'}], {'issues': []})))
        signature = ('isolated-workspace-refresh-scope', 1, 1)
        with patch.object(fixture_tests.panel.sources, '_provider', return_value=provider), \
             patch.object(fixture_tests.panel.sources, 'load_matches',
                          side_effect=AssertionError('An unscoped history read is forbidden')):
            first = read(signature, 'API-Football', '39')
            second = read(signature, 'API-Football', '39')
            self.assertEqual(first, second)
            provider.load_matches.assert_called_once_with(provider.STORE_PATH, leagues=['39'])
            read(signature, 'API-Football', '61')
            self.assertEqual(provider.load_matches.call_count, 2)
            self.assertEqual(provider.load_matches.call_args.kwargs, {'leagues': ['61']})
            read(('isolated-workspace-refresh-scope', 2, 2), 'API-Football', '39')
            self.assertEqual(provider.load_matches.call_count, 3)
            self.assertEqual(provider.load_matches.call_args.kwargs, {'leagues': ['39']})


if __name__ == '__main__':
    unittest.main()
