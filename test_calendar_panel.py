"""Calendar UI coverage using isolated API-Football records and no real requests."""
import copy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import api_football
import api_match_workspace
import calendar_panel as calendar
import scenario_workspace_panel as workspace


DAY = date(2026, 10, 18)
NOW = datetime(2026, 10, 18, 10, tzinfo=timezone.utc)
APP_PATH = Path(__file__).resolve().parent / 'app.py'


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


def raw_fixture(mid, *, league=39, name='Premier League', country='England',
                home=None, away=None, status='NS', hour=14, elapsed=None, goals=(None, None)):
    return {
        'fixture': {'id': mid, 'date': f'{DAY.isoformat()}T{hour:02d}:00:00+00:00',
                    'status': {'short': status, 'elapsed': elapsed, 'extra': None}},
        'league': {'id': league, 'name': name, 'country': country, 'season': 2026},
        'teams': {'home': {'id': mid * 2, 'name': home or f'Home {mid}'},
                  'away': {'id': mid * 2 + 1, 'name': away or f'Away {mid}'}},
        'goals': {'home': goals[0], 'away': goals[1]},
        'score': {'halftime': {'home': 1, 'away': 0}, 'fulltime': {'home': 2, 'away': 1}},
    }


def calendar_sample():
    rows = [raw_fixture(i, home=f'English Club {i}') for i in range(1, 7)]
    rows += [raw_fixture(7, league=61, name='Ligue 1', country='France', home='Paris FC',
                         status='2H', hour=9, elapsed=67, goals=(2, 1)),
             raw_fixture(8, league=61, name='Ligue 1', country='France', home='Marseille',
                         status='HT', hour=9, elapsed=45, goals=(0, 0)),
             raw_fixture(9, league=78, name='Bundesliga', country='Germany', home='Berlin',
                         status='FT', hour=7, elapsed=90, goals=(2, 1)),
             raw_fixture(10, league=999, name='World Cup', country='World', status='TBD')]
    rows[-1]['teams'] = {'home': {'id': None, 'name': None}, 'away': {'id': None, 'name': 'TBD'}}
    return rows


def snapshot_for(raw, day=DAY):
    fixtures = api_match_workspace.fixtures_in_day(raw, day, now=NOW)
    competitions = {}
    for row in raw:
        league = row['league']
        key = str(league['id'])
        info = competitions.setdefault(key, dict(id=key, league=key, code=key,
            name=league['name'], country=league['country'], matches=0, upcoming=0,
            usable=0, history_matches=80, current_season=2026))
        info['matches'] += 1
    return dict(day=day.isoformat(), timezone='Africa/Kinshasa', matches=raw, fixtures=fixtures,
                upcoming=[row for row in fixtures if row['is_upcoming']], competitions=competitions,
                stored_count=len(raw), usable_count=80, day_count=len(raw), day_usable_count=1,
                updated_at='2026-10-18T09:55:00+00:00', calendar_updated_at='2026-10-18T09:55:00+00:00',
                source_label='API-Football', history_counts={'39': 80})


class CalendarPanelTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.raw = calendar_sample()
        import prediction_journal
        self._patch(prediction_journal, 'DB_PATH', self.root / 'journal.sqlite3')
        self._patch(api_football, 'STORE_PATH', self.root / 'fixtures.json')
        self._patch(workspace.sources, 'selected_source', return_value='API-Football')
        self._patch(workspace, 'datetime', FrozenDateTime)
        self._patch(calendar, 'datetime', FrozenDateTime)
        self.clock = self._patch(calendar, 'monotonic', return_value=1000.0)
        self.signature = self._patch(workspace, '_signature', return_value=('isolated-calendar', 1, 1))
        self.snapshot = self._patch(workspace, '_snapshot',
            side_effect=lambda day, signature, source: snapshot_for(copy.deepcopy(self.raw), day))
        self.refresh = self._patch(workspace.sources, 'refresh_day',
            return_value={'day_count': len(self.raw)})
        self.history = self._patch(workspace, '_history', return_value=([], {'issues': []}))
        self.missing = self._patch(workspace.sources, 'missing_history', return_value=[])
        self.ensure = self._patch(workspace.sources, 'ensure_history')
        self.estimate = self._patch(workspace, '_estimate',
            return_value={'eligible': False, 'training_groups': 0})
        self.ml_estimate = self._patch(workspace, '_ml_estimate')
        self._patch(workspace.ml, 'load_report', return_value=None)
        self.key = self._patch(api_football, 'load_key',
                              side_effect=AssertionError('Calendar render must not read credentials'))
        self.network = self._patch(api_football.ApiFootballClient, 'get',
                                  side_effect=AssertionError('No real API calls in calendar UI tests'))
        self.at = AppTest.from_file(str(APP_PATH), default_timeout=15).run()
        self.assertFalse(self.at.exception)

    def _patch(self, target, name, *args, **kwargs):
        patcher = patch.object(target, name, *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def _table(self):
        tables = [frame.value for frame in self.at.dataframe
                  if {'Compétition', 'Domicile', 'Score', 'Minute'}.issubset(frame.value.columns)]
        self.assertEqual(len(tables), 1)
        return tables[0]

    def _status(self, value):
        self.at.button_group(key='calendar_status').set_value(value).run()
        self.assertFalse(self.at.exception)

    def _competition(self, league):
        self.at.selectbox(key=f'workspace_league_API-Football_{DAY}').set_value(league).run()
        self.assertFalse(self.at.exception)

    def _select(self, *ids, league='39'):
        for mid in ids:
            self.at.checkbox(key=f'workspace_match_API-Football_{DAY}_{league}_{mid}').check()
        self.at.run()
        self.assertFalse(self.at.exception)

    def _assert_no_prediction_or_real_network(self):
        self.estimate.assert_not_called()
        self.ml_estimate.assert_not_called()
        self.history.assert_not_called()
        self.ensure.assert_not_called()
        self.missing.assert_not_called()
        self.key.assert_not_called()
        self.network.assert_not_called()

    def test_initial_view_shows_every_competition_and_unconfirmed_match_without_a_four_match_cap(self):
        self.assertEqual(self.at.selectbox(key=f'workspace_league_API-Football_{DAY}').value, calendar.ALL)
        table = self._table()
        self.assertEqual(len(table), 10)
        self.assertEqual(set(table['Compétition']), {'Premier League', 'Ligue 1', 'Bundesliga', 'World Cup'})
        self.assertEqual(sum(table['Compétition'] == 'Premier League'), 6)
        self.assertEqual(sum(table['Domicile'] == 'Club à confirmer'), 1)
        unknown = table[table['Compétition'] == 'World Cup'].iloc[0]
        self.assertEqual(unknown['Score'], '—')
        self.assertEqual(unknown['Statut'], 'Horaire à confirmer')
        self.assertFalse(self.at.checkbox)
        self.assertFalse(any(button.key == 'workspace_predict' for button in self.at.button))
        self.assertFalse(self.at.toggle(key='workspace_auto').value)
        self.refresh.assert_not_called()
        self._assert_no_prediction_or_real_network()

    def test_first_refresh_after_empty_cloud_start_selects_all_competitions(self):
        populated = copy.deepcopy(self.raw)
        self.raw = []
        self.at = AppTest.from_file(str(APP_PATH), default_timeout=15).run()
        selection_key = f'workspace_league_API-Football_{DAY}'
        self.assertFalse(self.at.exception)
        self.assertIsNone(self.at.selectbox(key=selection_key).value)

        def refreshed(source, day):
            self.raw = populated
            return {'day_count': len(self.raw)}

        self.refresh.side_effect = refreshed
        self.at.button(key='workspace_refresh').click().run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.at.selectbox(key=selection_key).value, calendar.ALL)
        self.assertEqual(len(self._table()), len(populated))
        self.refresh.assert_called_once_with('API-Football', DAY)
        self.at.run()
        self.assertFalse(self.at.exception)
        self.assertEqual(len(self._table()), len(populated))
        self.refresh.assert_called_once()
        self._competition('39')
        self.assertEqual(len(self.at.checkbox), 6)
        self._assert_no_prediction_or_real_network()

    def test_refresh_removing_selected_competition_returns_to_all_matches(self):
        self._competition('39')

        def refreshed(source, day):
            self.raw = [row for row in self.raw if row['league']['id'] != 39]
            return {'day_count': len(self.raw)}

        self.refresh.side_effect = refreshed
        self.at.button(key='workspace_refresh').click().run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.at.selectbox(key=f'workspace_league_API-Football_{DAY}').value, calendar.ALL)
        self.assertEqual(len(self._table()), 4)
        self.assertNotIn('Premier League', set(self._table()['Compétition']))
        self.refresh.assert_called_once()
        self._assert_no_prediction_or_real_network()

    def test_invalid_competition_widget_return_keeps_the_full_calendar_visible(self):
        original = workspace.st.selectbox
        for invalid in (None, 'England · Premier League (5 matchs)', '999999'):
            with self.subTest(selection=invalid):
                def selectbox(label, *args, **kwargs):
                    value = original(label, *args, **kwargs)
                    return invalid if label == 'Compétition' else value

                with patch.object(workspace.st, 'selectbox', side_effect=selectbox):
                    self.at.run()
                self.assertFalse(self.at.exception)
                self.assertEqual(len(self._table()), len(self.raw))
                self.assertFalse(self.at.checkbox)
        self.refresh.assert_not_called()
        self._assert_no_prediction_or_real_network()

    def test_live_filter_includes_half_time_and_displays_current_scores_and_minutes(self):
        self._status('En cours')
        table = self._table()
        self.assertEqual(set(table['Domicile']), {'Paris FC', 'Marseille'})
        paris = table[table['Domicile'] == 'Paris FC'].iloc[0]
        paused = table[table['Domicile'] == 'Marseille'].iloc[0]
        self.assertEqual((paris['Score'], paris['Minute'], paris['Statut']), ('2–1', '67′', 'Seconde mi-temps'))
        self.assertEqual((paused['Score'], paused['Minute'], paused['Statut']), ('0–0', '45′', 'Mi-temps'))
        self.assertEqual(paris['À 90 minutes'], '—')
        self.refresh.assert_not_called()
        self._assert_no_prediction_or_real_network()

    def test_search_matches_club_country_and_competition_without_requesting_api_data(self):
        for query, expected in (('Paris', {'Paris FC'}), ('FRANCE', {'Paris FC', 'Marseille'}),
                                ('Bundesliga', {'Berlin'}), ('World', {'Club à confirmer'})):
            with self.subTest(query=query):
                self.at.text_input(key='calendar_query').set_value(query).run()
                self.assertFalse(self.at.exception)
                self.assertEqual(set(self._table()['Domicile']), expected)
        self.refresh.assert_not_called()
        self._assert_no_prediction_or_real_network()

    def test_competition_selection_exposes_all_six_cards_and_prediction_uses_one_league(self):
        self._competition('39')
        self.assertEqual(len(self.at.checkbox), 6)
        self.assertTrue(self.at.button(key='workspace_predict').disabled)
        self._select(1)
        self.assertTrue(self.at.button(key='workspace_predict').disabled)
        self._select(2)
        self.assertFalse(self.at.button(key='workspace_predict').disabled)
        self._assert_no_prediction_or_real_network()
        self.at.button(key='workspace_predict').click().run()
        self.assertFalse(self.at.exception)
        self.estimate.assert_called_once()
        _, fixtures, source = self.estimate.call_args.args
        self.assertEqual(source, 'API-Football')
        self.assertEqual([row['match_id'] for row in fixtures], ['1', '2'])
        self.assertEqual({row['league'] for row in fixtures}, {'39'})
        self.assertTrue(all('score' not in row and 'goals' not in row and 'value' not in row for row in fixtures))
        self._competition('61')
        self.assertEqual(len(self.at.checkbox), 2)
        self.assertTrue(all(checkbox.disabled for checkbox in self.at.checkbox))
        self.assertTrue(self.at.button(key='workspace_predict').disabled)
        self.estimate.assert_called_once()
        self.network.assert_not_called()

    def test_unconfirmed_teams_remain_visible_in_their_competition_and_cannot_be_selected(self):
        self._competition('999')
        self.assertFalse(self.at.checkbox)
        unknown_tables = [frame.value for frame in self.at.dataframe if 'Domicile' in frame.value.columns]
        self.assertEqual(len(unknown_tables), 1)
        self.assertEqual(unknown_tables[0].iloc[0]['Domicile'], 'Club à confirmer')
        self.assertTrue(self.at.button(key='workspace_predict').disabled)
        self.refresh.assert_not_called()
        self._assert_no_prediction_or_real_network()

    def test_manual_refresh_updates_scores_once_and_never_launches_a_prediction(self):
        def refreshed(source, day):
            self.assertEqual((source, day), ('API-Football', DAY))
            self.raw[6]['goals']['home'] = 3
            self.signature.return_value = ('isolated-calendar', 2, 2)
            return {'day_count': len(self.raw)}
        self.refresh.side_effect = refreshed
        self.at.button(key='workspace_refresh').click().run()
        self.assertFalse(self.at.exception)
        self.refresh.assert_called_once_with('API-Football', DAY)
        self.assertEqual(self._table().query("Domicile == 'Paris FC'").iloc[0]['Score'], '3–1')
        self.assertTrue(any('10 matchs disponibles' in message.value for message in self.at.success))
        self.at.run()
        self.assertFalse(self.at.exception)
        self.refresh.assert_called_once()
        self._assert_no_prediction_or_real_network()

    def test_auto_follow_is_opt_in_and_filters_do_not_repeat_requests_within_sixty_seconds(self):
        self.refresh.assert_not_called()
        self.at.toggle(key='workspace_auto').set_value(True).run()
        self.assertFalse(self.at.exception)
        self.refresh.assert_called_once_with('API-Football', DAY)
        self._status('En cours')
        self.at.text_input(key='calendar_query').set_value('France').run()
        self.assertFalse(self.at.exception)
        self.at.run()
        self.assertFalse(self.at.exception)
        self.refresh.assert_called_once()
        self.clock.return_value = 1061.0
        self.at.run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.refresh.call_count, 2)
        self.at.toggle(key='workspace_auto').set_value(False).run()
        self.clock.return_value = 1122.0
        self.at.run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.refresh.call_count, 2)
        self._assert_no_prediction_or_real_network()

    def test_manual_refresh_when_auto_is_due_makes_one_request_and_reserves_the_next_minute(self):
        self.at.toggle(key='workspace_auto').set_value(True).run()
        self.assertFalse(self.at.exception)
        self.refresh.assert_called_once()
        self.clock.return_value = 1061.0
        self.at.button(key='workspace_refresh').click().run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.refresh.call_count, 2)
        self.at.run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.refresh.call_count, 2)
        self._assert_no_prediction_or_real_network()

    def test_auto_error_pauses_until_explicit_retry_and_preserves_the_local_calendar(self):
        self.refresh.side_effect = api_football.ApiFootballError('Quota atteint')
        self.at.toggle(key='workspace_auto').set_value(True).run()
        self.assertFalse(self.at.exception)
        self.refresh.assert_called_once()
        self.assertEqual(len(self._table()), 10)
        self.assertTrue(any('Suivi automatique en pause' in warning.value for warning in self.at.warning))
        self.clock.return_value = 1121.0
        self.at.run()
        self.assertFalse(self.at.exception)
        self.refresh.assert_called_once()
        self.refresh.side_effect = None
        self.at.button(key='workspace_refresh').click().run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.refresh.call_count, 2)
        self.assertNotIn('calendar_auto_error', self.at.session_state)
        self.assertEqual(len(self._table()), 10)
        self._assert_no_prediction_or_real_network()

    def test_auto_follow_resets_a_persisted_past_date_and_requests_only_today(self):
        self.at.session_state['workspace_day'] = DAY - timedelta(days=1)
        self.at.run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.at.date_input(key='workspace_day').value, DAY)
        self.assertTrue(self.at.date_input(key='workspace_day').disabled)
        self.at.toggle(key='workspace_auto').set_value(True).run()
        self.assertFalse(self.at.exception)
        self.refresh.assert_called_once_with('API-Football', DAY)
        self._assert_no_prediction_or_real_network()


if __name__ == '__main__':
    unittest.main()
