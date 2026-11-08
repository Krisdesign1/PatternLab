"""Manual cross-competition UI: isolated calendars, real forecasts and local archives."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
import streamlit as st
from streamlit.testing.v1 import AppTest

import analysis_date
import api_football
import api_match_workspace
import cloud_storage
import manual_analysis_store as archive
import manual_mixed_panel as panel
import manual_proposal_archive as proposals
import scenario_workspace_panel as workspace
from test_mixed_models import mixed_fixtures, mixed_history


DAY = date(2025, 3, 20)
NOW = datetime(2025, 3, 20, 10, tzinfo=timezone.utc)
SOURCE = 'API-Football'


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


def raw_calendar(day=DAY):
    raw = []
    for fixture in mixed_fixtures():
        stamp = fixture['timestamp'].replace(DAY.isoformat(), day.isoformat()) + '+00:00'
        league = int(fixture['league'])
        raw.append({
            'fixture': {'id': int(fixture['match_id']), 'date': stamp,
                        'status': {'short': 'NS', 'elapsed': None}},
            'league': {'id': league, 'name': 'Premier League' if league == 39 else 'Ligue 1',
                       'country': 'England' if league == 39 else 'France', 'season': 2025},
            'teams': {'home': {'id': int(fixture['home_id']), 'name': fixture['home']},
                      'away': {'id': int(fixture['away_id']), 'name': fixture['away']}},
            'goals': {'home': None, 'away': None},
            'score': {'halftime': {'home': None, 'away': None},
                      'fulltime': {'home': None, 'away': None}},
        })
    return raw


def snapshot(raw, day):
    fixtures = api_match_workspace.fixtures_in_day(raw, day, now=NOW)
    competitions = {}
    for item in raw:
        info = item['league']; code = str(info['id'])
        value = competitions.setdefault(code, dict(id=code, league=code, code=code,
            name=info['name'], country=info['country'], matches=0, upcoming=0,
            usable=0, history_matches=40, current_season=2025))
        value['matches'] += 1
    return dict(day=day.isoformat(), timezone='Africa/Kinshasa', matches=deepcopy(raw),
        fixtures=fixtures, upcoming=[row for row in fixtures if row['is_upcoming']],
        competitions=competitions, day_count=len(fixtures), stored_count=len(raw),
        usable_count=80, day_usable_count=0, source_label=SOURCE,
        updated_at=NOW.isoformat(), calendar_updated_at=NOW.isoformat(), history_counts={})


def selectable_grid(data, **kwargs):
    """AppTest has no native selection event API; supply rows on the displayed frame."""
    frame = pd.DataFrame(data).copy()
    if st.session_state.get('test_reverse_calendar'):
        frame = frame.iloc[::-1]
    st.dataframe(frame, hide_index=True, key='mixed_test_calendar')
    event = SimpleNamespace(selection=SimpleNamespace(rows=st.session_state.get('test_selected_rows', [])))
    return frame, event


def readonly_grid(data, **kwargs):
    return st.dataframe(pd.DataFrame(data), hide_index=True, key=kwargs.get('table_id'))


class ManualMixedPanelTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'manual.sqlite3'
        self._patch(cloud_storage, '_options', return_value={'PATTERNLAB_STORAGE_MODE': 'local'})
        self.raw = raw_calendar()
        self._patch(panel, '_now', return_value=NOW)
        self._patch(workspace, 'datetime', FrozenDateTime)
        self._patch(analysis_date, 'datetime', FrozenDateTime)
        self._patch(panel.admin_access, 'can_admin', return_value=False)
        self._patch(workspace.sources, 'selected_source', return_value=SOURCE)
        self._patch(workspace, '_signature', return_value=('mixed-panel-isolated', 1))
        self._patch(workspace, '_snapshot', side_effect=lambda day, *args: snapshot(self.raw, day))
        self._patch(panel.table_views, 'selectable_dataframe', side_effect=selectable_grid)
        self._patch(panel.table_views, 'dataframe', side_effect=readonly_grid)
        self._patch(st, 'page_link')  # Standalone test script does not register app navigation.
        rerun = st.rerun
        # AppTest runs whole scripts, even for fragment-owned widget events.
        self._patch(st, 'rerun', side_effect=lambda **kwargs: rerun(scope='app'))
        self.history = self._patch(panel, '_history', return_value=mixed_history(40))
        self._patch(workspace.ml, 'load_report', return_value=None)
        import prospective_models
        self._patch(prospective_models, '_context_neighbors',
                    side_effect=prospective_models.SnapshotError('Configuration absente.'))
        record = archive.record
        self.saved = self._patch(archive, 'record', side_effect=lambda payload, **kwargs:
            record(payload, path=self.path, clock=lambda: NOW, **kwargs))
        self.network = self._patch(api_football.ApiFootballClient, 'get',
            side_effect=AssertionError('UI tests must not call the API'))
        self.credentials = self._patch(api_football, 'load_key',
            side_effect=AssertionError('UI tests must not load credentials'))
        self.refresh = self._patch(workspace.sources, 'refresh_day',
            side_effect=AssertionError('Viewing/selecting does not refresh the API'))
        self.at = AppTest.from_string(
            "import manual_mixed_panel as panel\npanel.render('API-Football')", default_timeout=20).run()
        self.assert_clean()

    def _patch(self, obj, name, *args, **kwargs):
        item = patch.object(obj, name, *args, **kwargs)
        mock = item.start(); self.addCleanup(item.stop)
        return mock

    def tearDown(self):
        self.network.assert_not_called()
        self.credentials.assert_not_called()
        self.refresh.assert_not_called()

    def assert_clean(self):
        self.assertFalse(self.at.exception, [str(item.value) for item in self.at.exception])

    def choose(self, *indices):
        self.at.session_state['test_selected_rows'] = list(indices)
        self.at.run()
        self.at.button(key='mixed_manual_add').click().run()
        self.assert_clean()

    def draft(self, day=DAY):
        return dict(self.at.session_state[panel._draft_key(SOURCE, day)])

    def table(self, column):
        return next(item.value for item in self.at.dataframe if column in item.value.columns)

    def test_initial_table_includes_both_competitions_without_running_a_model(self):
        table = self.table('ID match')
        self.assertEqual(len(table), 4)
        self.assertEqual(set(table['Compétition']), {'Premier League', 'Ligue 1'})
        self.assertTrue(self.at.button(key='mixed_manual_add').disabled)
        self.assertTrue(self.at.button(key='mixed_manual_analyze').disabled)
        self.assertTrue(self.at.button(key='mixed_manual_refresh').disabled)
        self.history.assert_not_called(); self.saved.assert_not_called()

    def test_stale_upcoming_status_is_visible_in_all_but_not_selectable_future_view(self):
        self.raw[0]['fixture']['date'] = DAY.isoformat() + 'T08:00:00+00:00'
        self.at.run(); self.assert_clean()
        self.assertEqual(len(self.table('ID match')), 3)
        self.assertTrue(any('horaire passé' in item.value for item in self.at.caption))
        self.at.button_group(key='mixed_manual_status').set_value('Tous').run()
        self.assert_clean()
        self.assertEqual(len(self.table('ID match')), 4)
        self.choose(0)
        self.assertFalse(self.draft())
        self.history.assert_not_called(); self.saved.assert_not_called()

    def test_add_across_competitions_search_and_rerun_keep_the_group(self):
        self.choose(0, 1)
        self.assertEqual(set(self.draft()), {'9000', '9001'})
        self.at.multiselect(key=f'mixed_manual_leagues_{SOURCE}_{DAY}').set_value(['61']).run()
        self.at.text_input(key='mixed_manual_query').set_value('H3').run()
        self.assertEqual(list(self.table('ID match')['ID match']), ['9003'])
        self.choose(0)
        self.assertEqual(set(self.draft()), {'9000', '9001', '9003'})
        self.at.text_input(key='mixed_manual_query').set_value('no such team').run()
        self.assert_clean()
        self.assertEqual(set(self.draft()), {'9000', '9001', '9003'})
        self.assertFalse(self.at.button(key='mixed_manual_analyze').disabled)
        self.history.assert_not_called(); self.saved.assert_not_called()

    def test_selection_uses_displayed_sorted_positions_not_original_indices(self):
        self.at.session_state['test_reverse_calendar'] = True
        self.choose(0, 2, 99, -1, True)
        self.assertEqual(set(self.draft()), {'9003', '9001'})
        self.assertEqual(list(self.table('État')['Domicile']), ['H1', 'H3'])

    def test_remove_is_explicit_and_does_not_remove_other_competitions(self):
        self.choose(0, 1, 2)
        self.at.multiselect(key=f'mixed_manual_remove_{DAY}').set_value(['9001']).run()
        self.at.button(key='mixed_manual_remove_action').click().run()
        self.assert_clean()
        self.assertEqual(set(self.draft()), {'9000', '9002'})
        self.assertFalse(self.at.button(key='mixed_manual_analyze').disabled)
        self.history.assert_not_called()

    def test_missing_or_changed_identity_keeps_snapshot_and_blocks_recalculation(self):
        self.choose(0, 1)
        original = self.draft()
        self.raw = self.raw[1:]
        self.raw[0]['teams']['home']['id'] = 55555
        self.at.run(); self.assert_clean()
        self.assertEqual(self.draft(), original)
        table = self.table('État')
        self.assertEqual(list(table['État']), ['À revérifier', 'À revérifier'])
        self.assertTrue(self.at.button(key='mixed_manual_analyze').disabled)
        self.history.assert_not_called(); self.saved.assert_not_called()

    def test_started_fixture_is_retained_but_cannot_be_analyzed_again(self):
        self.choose(0, 1)
        self.raw[0]['fixture']['status']['short'] = '1H'
        self.raw[0]['fixture']['status']['elapsed'] = 12
        self.at.run(); self.assert_clean()
        self.assertEqual(set(self.draft()), {'9000', '9001'})
        self.assertTrue(self.at.button(key='mixed_manual_analyze').disabled)
        self.history.assert_not_called()

    def test_drafts_are_separate_by_day_and_restore_when_returning(self):
        self.choose(0, 1)
        tomorrow = DAY + timedelta(days=1)
        self.raw = raw_calendar(tomorrow)
        self.at.session_state[analysis_date.DAY_KEY] = tomorrow
        self.at.run(); self.assert_clean()
        self.assertTrue(self.at.button(key='mixed_manual_analyze').disabled)
        self.choose(2, 3)
        self.assertEqual(set(self.draft(tomorrow)), {'9002', '9003'})
        self.raw = raw_calendar()
        self.at.session_state[analysis_date.DAY_KEY] = DAY
        self.at.run(); self.assert_clean()
        self.assertEqual(set(self.draft()), {'9000', '9001'})
        self.assertFalse(self.at.button(key='mixed_manual_analyze').disabled)

    def test_analyze_uses_real_mixed_backend_saves_full_archive_and_deduplicates(self):
        self.choose(0, 1)
        self.at.button(key='mixed_manual_analyze').click().run()
        self.assert_clean()
        self.assertFalse(self.at.error)
        self.assertTrue(self.at.success, [item.value for item in self.at.warning])
        payload = self.at.session_state['mixed_manual_forecast']
        self.assertTrue(payload['forecast']['eligible'])
        self.assertNotIn('save_error', payload)
        saved = payload['saved']
        self.assertEqual(saved['engine'], 'mixed_clubs')
        self.assertEqual([row['league'] for row in saved['fixtures']], ['39', '61'])
        self.assertTrue(proposals.coverage(saved)['complete'])
        self.assertEqual(saved['forecast'], payload['forecast'])
        self.assertEqual(saved['comparison_methods'], payload['comparison_methods'])
        self.assertTrue(all(not {'ht', 'ft', 'score', 'goals'} & row.keys() for row in saved['fixtures']))
        self.assertEqual(self.history.call_args.args[-1], ('39', '61'))
        self.at.run(); self.assert_clean()
        self.saved.assert_called_once()
        self.at.button(key='mixed_manual_analyze').click().run(); self.assert_clean()
        again = self.at.session_state['mixed_manual_forecast']['saved']
        self.assertEqual(saved['id'], again['id'])
        self.assertFalse(again['created'])

    def test_workspace_mode_reaches_mixed_panel_without_loading_single_league_forecast(self):
        self.at = AppTest.from_string(
            'import scenario_workspace_panel as workspace\nworkspace.render_workspace()', default_timeout=20)
        self.at.session_state['workspace_group_mode'] = 'mixed'
        with patch.object(workspace, '_render_workspace', side_effect=AssertionError('Single mode must not run')):
            self.at.run(); self.assert_clean()
        self.assertEqual(self.at.button_group(key='workspace_group_mode').value, 'mixed')
        self.assertEqual(len(self.table('ID match')), 4)
        self.history.assert_not_called()


if __name__ == '__main__':
    unittest.main()
