"""Exercise the date-first UI with isolated data and a real small forecast model."""
import copy
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import api_football
import api_football_panel
import database
import football_data
import football_data_panel
import scenario_workspace_panel as panel


DAY = date(2026, 10, 18)
NOW = datetime(2026, 10, 18, 8, tzinfo=timezone.utc)
APP_PATH = Path(__file__).resolve().parent / 'app.py'
CACHED_SNAPSHOT = panel._snapshot
CACHED_HISTORY = panel._history
CACHED_READINESS_HISTORY = panel._readiness_history


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


def history_rows():
    rows = []
    for league, code, country, competition, offset in (
        ('39', 'PL', 'England', 'Premier League', 0),
        ('61', 'FL1', 'France', 'Ligue 1', 1000),
    ):
        for i in range(80):
            day = date(2026, 8, 1) + timedelta(days=i // 2)
            role = i % 4 + 1
            ht, ft, value = (
                ('2-0', '2-0', '1st'),
                ('0-0', '2-1', '2nd'),
                ('1-0', '1-1', 'Égal'),
            )[i % 3]
            rows.append(dict(match_id=str(offset + i + 1), league=league,
                             competition_code=code, country=country, competition=competition,
                             home=f'{code} Home {role}', away=f'{code} Away {role}',
                             timestamp=f'{day.isoformat()}T{12+i % 2:02d}:00:00', day=day.isoformat(),
                             ht=ht, ft=ft, value=value, season=2025 if i < 40 else 2026,
                             provider='api-football', source='isolated-test'))
    return rows


def fixture(match_id, day=DAY, *, league='39', role=1, hour=13, status='TIMED'):
    code = 'PL' if league == '39' else 'FL1'
    return dict(match_id=str(match_id), league=league,
                home=f'{code} Home {role}', away=f'{code} Away {role}',
                timestamp=f'{day.isoformat()}T{hour:02d}:00:00', provider='api-football',
                status=status, is_upcoming=status == 'TIMED' and hour > 8,
                is_historical=status == 'FINISHED')


def league_key(day=DAY, source='API-Football'):
    return f'workspace_league_{source}_{day}'


def match_key(match_id, day=DAY, league='39', source='API-Football'):
    return f'workspace_match_{source}_{day}_{league}_{match_id}'


class WorkspaceDayProjectionTests(unittest.TestCase):
    def test_projection_keeps_exact_kinshasa_day_including_previous_utc_evening(self):
        stamps = ['2026-10-17T22:59:59Z', '2026-10-17T23:00:00Z',
                  '2026-10-18T22:59:59Z', '2026-10-18T23:00:00Z']
        fixtures = [dict(fixture(index + 1), timestamp=stamp) for index, stamp in enumerate(stamps)]
        raw = [{'fixture': {'id': index + 1, 'date': stamp, 'status': {'short': 'NS'}},
                'league': {'id': 39, 'name': 'Premier League', 'country': 'England', 'season': 2026},
                'teams': {'home': {'id': 1, 'name': 'Home'}, 'away': {'id': 2, 'name': 'Away'}}}
               for index, stamp in enumerate(stamps)]
        snapshot = {'fixtures': fixtures, 'matches': raw, 'competitions': {'39': {
            'name': 'Premier League', 'country': 'England', 'matches': 4}},
            'day_count': 4, 'usable_count': 123}
        original = copy.deepcopy(snapshot)
        projected = panel._day_snapshot(snapshot, DAY)
        self.assertEqual([row['match_id'] for row in projected['fixtures']], ['2', '3'])
        self.assertEqual([row['fixture']['id'] for row in projected['matches']], [2, 3])
        self.assertEqual(projected['day_count'], 2)
        self.assertEqual(projected['competitions']['39']['matches'], 2)
        self.assertEqual(projected['usable_count'], 123)
        self.assertEqual(snapshot, original)

    def test_calculation_guard_rejects_another_local_date_even_with_today_selection_key(self):
        with patch.object(panel, 'datetime', FrozenDateTime):
            with self.assertRaisesRegex(ValueError, 'autre date'):
                panel._require_today(DAY, [dict(fixture(1), timestamp='2026-10-18T23:00:00Z')])


class ScenarioWorkspacePanelTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.rows = history_rows()
        self.now = NOW
        self.audit = {'expected': len(self.rows), 'matched': len(self.rows), 'issues': []}
        self.extra_match = False
        self._patch(database, 'DB_PATH', self.root / 'coupons.db')
        self._patch(panel.journal, 'DB_PATH', self.root / 'journal.sqlite3')
        self._patch(football_data, 'STORE_PATH', self.root / 'matches.json')
        self.source = self._patch(panel.sources, 'selected_source', return_value='API-Football')
        self.signature = self._patch(panel, '_signature', return_value=(str(self.root), 1, 1))
        self._patch(football_data, 'data_signature', return_value=(str(self.root), 1, 1))
        self._patch(FrozenDateTime, 'now', side_effect=lambda tz=None:
                    self.now.astimezone(tz) if tz else self.now.replace(tzinfo=None))
        self._patch(panel, 'datetime', FrozenDateTime)
        self.ml_report = self._patch(panel.ml, 'load_report', return_value=None)
        self.context_report = self._patch(panel.contextual_validation, 'load_report', return_value=None)
        self.journal_register = self._patch(panel.tracking, 'register', return_value={
            'state':'recorded','id':'synthetic','created':True,'message':'Prévision enregistrée avant match.'})
        self.snapshot = self._patch(panel, '_snapshot', side_effect=self._snapshot)
        self.history = self._patch(panel, '_history', return_value=(self.rows, self.audit))
        self.estimate = self._patch(panel, '_estimate',
                                    side_effect=lambda result, fixtures, source: panel.forecast(result, fixtures))
        self.evaluation = self._patch(panel, '_evaluate', side_effect=lambda result, source: panel.evaluate(result))
        self.refresh = self._patch(panel.sources, 'refresh_day')
        self.ensure = self._patch(panel.sources, 'ensure_history')
        self.missing = self._patch(panel.sources, 'missing_history', return_value=[])
        self.fd_network = self._patch(football_data.FootballDataClient, 'get',
                                      side_effect=AssertionError('Unexpected football-data API request'))
        self.api_network = self._patch(api_football.ApiFootballClient, 'get',
                                       side_effect=AssertionError('Unexpected API-Football request'))
        self._patch(football_data, 'load_key', return_value='synthetic-test-token')
        self._patch(api_football, 'load_key', return_value='synthetic-test-token')
        self._patch(football_data, 'load_matches', return_value=(self.rows, self.audit))
        self._patch(api_football, 'load_matches', return_value=(self.rows, self.audit))
        self._patch(football_data, 'load_fixtures', return_value=[])
        self._patch(api_football, 'load_fixtures', return_value=[])
        for provider_panel in (football_data_panel, api_football_panel):
            self._patch(provider_panel, 'load_key', return_value='synthetic-test-token')
            self._patch(provider_panel, 'load_fixtures', return_value=[])
            self._patch(provider_panel, 'DATA_DIR', self.root)
            self._patch(provider_panel, 'STORE_PATH', self.root / 'provider.json')
            self._patch(provider_panel, 'CHECK_PATH', self.root / 'access_check.json')
        self.at = AppTest.from_file(str(APP_PATH), default_timeout=15).run()
        self.assertFalse(self.at.exception)
        self.at.selectbox(key=league_key()).set_value('39').run()
        self.assertFalse(self.at.exception)

    def _patch(self, target, name, *args, **kwargs):
        patcher = patch.object(target, name, *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def _snapshot(self, day, signature, source):
        fixtures = [fixture(101, day, role=1), fixture(102, day, role=2, hour=15),
                    fixture(103, day, role=3, hour=18),
                    fixture(104, day, role=4, status='FINISHED'),
                    fixture(105, day, role=1, hour=7),
                    fixture(201, day, league='61', role=1),
                    fixture(202, day, league='61', role=2, hour=15)]
        if self.extra_match:
            fixtures.append(fixture(106, day, role=4, hour=20))
        competitions = {
            '39': {'country': 'England', 'name': 'Premier League', 'matches': 6 if self.extra_match else 5,
                     'code': '39', 'current_season': 2026},
            '61': {'country': 'France', 'name': 'Ligue 1', 'matches': 2,
                     'code': '61', 'current_season': 2026},
        }
        if source == 'API-Football':
            league_ids = {'39': '39', '61': '61'}
            for row in fixtures:
                row['league'] = league_ids[row['league']]
                row['provider'] = 'api-football'
            competitions = {league_ids[key]: dict(info, code=league_ids[key])
                            for key, info in competitions.items()}
        return dict(day=day.isoformat(), competitions=competitions, fixtures=fixtures,
                    day_count=len(fixtures), usable_count=len(self.rows), updated_at='2026-10-18T07:00:00Z')

    def _select(self, *ids, day=DAY, league='39', source='API-Football'):
        for mid in ids:
            self.at.checkbox(key=match_key(mid, day, league, source)).check()
        self.at.run()
        self.assertFalse(self.at.exception)

    def _predict(self):
        self.at.button(key='workspace_predict').click().run()
        self.assertFalse(self.at.exception)

    def _scenario_metrics(self):
        return [metric for metric in self.at.metric if metric.label == 'Estimation du scénario complet']

    def _assert_idle_network(self):
        self.fd_network.assert_not_called()
        self.api_network.assert_not_called()

    def test_three_pages_render_without_automatic_api_requests(self):
        self.assertEqual(self.at.title[0].value, 'Préparer mes scénarios')
        self.assertEqual(self.at.date_input(key='workspace_day').value, DAY)
        self.assertTrue(self.at.date_input(key='workspace_day').disabled)
        self.at.switch_page('ui_pages/explore.py').run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.at.title[0].value, 'Explorer les scénarios')
        self.assertFalse(any(item.key == 'league_source' for item in self.at.selectbox))
        self.assertFalse(any(item.key == 'show_contextual_scenarios' for item in self.at.checkbox))
        self.at.switch_page('ui_pages/settings.py').run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.at.title[0].value, 'Données et réglages')
        self.at.switch_page('ui_pages/analyze.py').run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.at.title[0].value, 'Préparer mes scénarios')
        self.refresh.assert_not_called()
        self.ensure.assert_not_called()
        self.missing.assert_not_called()
        self.estimate.assert_not_called()
        self._assert_idle_network()

    def test_two_match_minimum_and_elapsed_or_finished_cards_are_disabled(self):
        self.assertTrue(self.at.button(key='workspace_predict').disabled)
        self.assertTrue(self.at.checkbox(key=match_key(104)).disabled)
        self.assertTrue(self.at.checkbox(key=match_key(105)).disabled)
        self._select(102)
        self.assertTrue(self.at.button(key='workspace_predict').disabled)
        self._select(101)
        self.assertFalse(self.at.button(key='workspace_predict').disabled)
        self.estimate.assert_not_called()
        self.history.assert_not_called()
        self.missing.assert_not_called()
        self._assert_idle_network()

    def test_competition_browser_routes_filtered_id_without_predicting_or_refreshing(self):
        self.at.selectbox(key=league_key()).set_value(panel.calendar.ALL).run()
        self.assertFalse(self.at.exception)
        self.at.selectbox(key='competition_browser_country').set_value('France').run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.at.selectbox(key='competition_browser_selection').value,'61')
        self.at.button(key='competition_browser_analyze').click().run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.at.selectbox(key=league_key()).value,'61')
        self.assertTrue(any(item.key==match_key(201,league='61') for item in self.at.checkbox))
        self.estimate.assert_not_called()
        self.refresh.assert_not_called()
        self.journal_register.assert_not_called()
        self._assert_idle_network()

    def test_real_forecast_uses_ordered_score_free_fixtures_and_persists_on_rerun(self):
        self._select(102, 101)
        self._predict()
        self.assertEqual(len(self._scenario_metrics()), 1)
        self.estimate.assert_called_once()
        self.journal_register.assert_called_once()
        result, fixtures, source = self.estimate.call_args.args
        self.assertEqual(source, 'API-Football')
        self.assertEqual(result['size'], 2)
        self.assertEqual([row['match_id'] for row in fixtures], ['101', '102'])
        self.assertTrue(all('value' not in row and 'ht' not in row and 'ft' not in row for row in fixtures))
        payload = copy.deepcopy(self.at.session_state['workspace_forecast'])
        self.assertTrue(payload['forecast']['eligible'])
        self.assertEqual(payload['forecast']['personalized_count'], 2)
        self.assertTrue(all(len(row['sequence']) == 2 for row in payload['forecast']['ranking']))
        self.assertLess(sum(row['probability'] for row in payload['forecast']['ranking']), 1)
        self.at.run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.at.session_state['workspace_forecast'], payload)
        self.assertEqual(len(self._scenario_metrics()), 1)
        self.estimate.assert_called_once()
        self.ensure.assert_not_called()
        self.missing.assert_called_once_with('API-Football', '39', [2025, 2026])
        self._assert_idle_network()

    def test_journal_failure_is_visible_and_does_not_claim_a_saved_prediction(self):
        self.journal_register.side_effect=panel.journal.JournalError('Un match a commencé pendant le calcul')
        self._select(101,102)
        self._predict()
        self.assertEqual(len(self._scenario_metrics()),1)
        self.assertTrue(any('non enregistrée' in row.value and 'commencé' in row.value for row in self.at.warning))
        self.assertEqual(self.at.session_state['workspace_forecast']['journal']['state'],'failed')
        self._assert_idle_network()

    def test_historical_evaluation_persists_only_for_the_analyzed_group(self):
        self._select(101, 102)
        self._predict()
        self.evaluation.assert_not_called()
        self.at.button(key='workspace_evaluate').click().run()
        self.assertFalse(self.at.exception)
        self.evaluation.assert_called_once()
        report = copy.deepcopy(self.at.session_state['workspace_evaluation'])
        summary = report['evaluation']['summary']
        self.assertGreater(summary['tested'], 0)
        self.assertEqual(set(summary['models']), {'frequency', 'clubs', 'combined'})
        self.assertTrue(all(row['tested'] == summary['tested'] for row in summary['models'].values()))
        tables = [frame.value for frame in self.at.dataframe if 'Méthode' in frame.value.columns]
        self.assertEqual(len(tables), 1)
        self.assertEqual(len(tables[0]), 3)
        self.at.run()
        self.assertFalse(self.at.exception)
        self.evaluation.assert_called_once()
        self.assertEqual(self.at.session_state['workspace_evaluation'], report)
        self.assertEqual(len([frame for frame in self.at.dataframe if 'Méthode' in frame.value.columns]), 1)
        self._select(103)
        self.assertFalse(any('Méthode' in frame.value.columns for frame in self.at.dataframe))
        self._predict()
        self.assertFalse(any('Méthode' in frame.value.columns for frame in self.at.dataframe))
        self.assertNotEqual(self.at.session_state['workspace_forecast']['context'], report['context'])
        self.evaluation.assert_called_once()
        self._assert_idle_network()

    def test_selection_and_source_revision_hide_stale_forecasts_until_recalculated(self):
        self._select(101, 102)
        self._predict()
        self._select(103)
        self.assertFalse(self._scenario_metrics())
        self.assertTrue(any('La sélection ou les données ont changé' in row.value for row in self.at.caption))
        self.estimate.assert_called_once()
        self._predict()
        self.assertEqual(self.estimate.call_count, 2)
        self.assertEqual(len(self._scenario_metrics()), 1)
        self.assertTrue(all(len(row['sequence']) == 3
                            for row in self.at.session_state['workspace_forecast']['forecast']['ranking']))
        self.signature.return_value = (str(self.root), 2, 2)
        self.at.run()
        self.assertFalse(self.at.exception)
        self.assertFalse(self._scenario_metrics())
        self.assertEqual(self.estimate.call_count, 2)
        self._assert_idle_network()

    def test_competition_and_real_local_day_changes_never_show_previous_group_results(self):
        self._select(101, 102)
        self._predict()
        self.at.selectbox(key=league_key()).set_value('61').run()
        self.assertFalse(self.at.exception)
        self.assertFalse(self._scenario_metrics())
        self.assertTrue(self.at.button(key='workspace_predict').disabled)
        self._select(201, 202, league='61')
        self._predict()
        self.assertEqual(self.at.session_state['workspace_forecast']['context'][1], '61')
        self.assertTrue(all(row['league'] == '61'
                            for row in self.at.session_state['workspace_forecast']['fixtures']))
        self.now = NOW + timedelta(days=1)
        self.at.run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.at.date_input(key='workspace_day').value, DAY + timedelta(days=1))
        self.assertNotIn('workspace_forecast', self.at.session_state)
        self.assertFalse(self._scenario_metrics())
        self.assertFalse(self.at.checkbox)
        self.assertEqual(self.estimate.call_count, 2)
        self._assert_idle_network()

    def test_persisted_future_or_past_date_cannot_move_the_analyser_off_today(self):
        for stale in (DAY + timedelta(days=8), DAY - timedelta(days=8)):
            self.at.session_state['workspace_day'] = stale
            self.snapshot.reset_mock()
            self.at.run()
            self.assertFalse(self.at.exception)
            self.assertEqual(self.at.date_input(key='workspace_day').value, DAY)
            self.assertTrue(self.at.date_input(key='workspace_day').disabled)
            self.assertEqual(self.snapshot.call_args.args[0], DAY)
        self.estimate.assert_not_called()
        self.journal_register.assert_not_called()
        self._assert_idle_network()

    def test_kinshasa_midnight_uses_next_local_day_while_utc_is_still_previous_day(self):
        self.now = datetime(2026, 10, 18, 23, 0, tzinfo=timezone.utc)
        self.at.run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.at.date_input(key='workspace_day').value, date(2026, 10, 19))
        self.assertEqual(self.snapshot.call_args.args[0], date(2026, 10, 19))
        self.estimate.assert_not_called()
        self.refresh.assert_not_called()
        self._assert_idle_network()

    def test_stale_snapshot_cannot_offer_yesterday_or_tomorrow_fixtures(self):
        snapshot = self._snapshot(DAY, (), 'API-Football')
        snapshot['fixtures'] += [fixture(901, DAY - timedelta(days=1)), fixture(902, DAY + timedelta(days=1))]
        snapshot['competitions']['39']['matches'] += 2
        snapshot['day_count'] += 2
        self.snapshot.side_effect = None
        self.snapshot.return_value = snapshot
        self.at.run()
        self.assertFalse(self.at.exception)
        self.assertEqual(next(item.value for item in self.at.metric if item.label == 'Matchs à cette date'), '7')
        self.assertFalse(any(item.key and item.key.endswith(('_901', '_902')) for item in self.at.checkbox))
        self.estimate.assert_not_called()
        self._assert_idle_network()

    def test_midnight_during_history_preparation_stops_forecast_and_registration(self):
        self._select(101, 102)
        self.missing.return_value = [2026]
        self.ensure.side_effect = lambda *args: setattr(self, 'now', NOW + timedelta(days=1))
        self._predict()
        self.assertTrue(any('La journée a changé' in item.value for item in self.at.error))
        self.estimate.assert_not_called()
        self.journal_register.assert_not_called()
        self.assertNotIn('workspace_forecast', self.at.session_state)
        self._assert_idle_network()

    def test_midnight_during_forecast_stops_registration_and_old_results_display(self):
        self._select(101, 102)
        def crossing(result, fixtures, source):
            estimate = panel.forecast(result, fixtures)
            self.now = NOW + timedelta(days=1)
            return estimate
        self.estimate.side_effect = crossing
        self._predict()
        self.assertTrue(any('La journée a changé' in item.value for item in self.at.error))
        self.journal_register.assert_not_called()
        self.assertFalse(self._scenario_metrics())
        self.assertNotIn('workspace_forecast', self.at.session_state)
        self._assert_idle_network()

    def test_refresh_is_explicit_updates_visible_snapshot_and_does_not_repeat_on_rerun(self):
        def refreshed(source, day):
            self.assertEqual(source, 'API-Football')
            self.assertEqual(day, DAY)
            self.extra_match = True
            self.signature.return_value = (str(self.root), 2, 2)
            return {'day_count': 8}
        self.refresh.side_effect = refreshed
        self.at.button(key='workspace_refresh').click().run()
        self.assertFalse(self.at.exception)
        self.refresh.assert_called_once_with('API-Football', DAY)
        self.assertTrue(any('8 matchs disponibles' in row.value for row in self.at.success))
        self.assertEqual(next(row.value for row in self.at.metric if row.label == 'Matchs à cette date'), '8')
        self.assertFalse(self.at.checkbox(key=match_key(106)).disabled)
        self.at.run()
        self.assertFalse(self.at.exception)
        self.refresh.assert_called_once()
        self.assertFalse(self.at.success)
        self.ensure.assert_not_called()
        self.missing.assert_not_called()
        self.estimate.assert_not_called()
        self._assert_idle_network()

    def test_refresh_error_keeps_selection_and_does_not_start_history_or_forecast(self):
        self._select(101, 102)
        self.refresh.side_effect = api_football.ApiFootballError('Quota atteint ; réessayez plus tard.')
        self.at.button(key='workspace_refresh').click().run()
        self.assertFalse(self.at.exception)
        self.assertTrue(any('Quota atteint' in row.value for row in self.at.error))
        self.assertTrue(self.at.checkbox(key=match_key(101)).value)
        self.assertTrue(self.at.checkbox(key=match_key(102)).value)
        self.assertFalse(self.at.button(key='workspace_predict').disabled)
        self.refresh.assert_called_once_with('API-Football', DAY)
        self.history.assert_not_called()
        self.ensure.assert_not_called()
        self.missing.assert_not_called()
        self.estimate.assert_not_called()
        self._assert_idle_network()

    def test_missing_history_is_prepared_only_after_submit_and_reloaded_before_forecast(self):
        old_rows = [row for row in self.rows if row['season'] == 2025]
        daily_results = [row for row in self.rows if row['season'] == 2026][:3]
        self.history.side_effect = [(old_rows + daily_results, self.audit), (self.rows, self.audit)]
        self.missing.return_value = [2026]
        self._select(101, 102)
        self.ensure.assert_not_called()
        self.missing.assert_not_called()
        self._predict()
        self.missing.assert_called_once_with('API-Football', '39', [2025, 2026])
        self.ensure.assert_called_once_with('API-Football', '39', [2026])
        self.assertEqual(self.history.call_count, 2)
        self.assertEqual(len(self.estimate.call_args.args[0]['matches']), 80)
        self.assertEqual(len(self._scenario_metrics()), 1)
        self._assert_idle_network()

    def test_failed_history_preparation_preserves_selection_and_avoids_forecast(self):
        self.history.return_value = ([row for row in self.rows if row['season'] == 2025], self.audit)
        self.missing.return_value = [2026]
        self.ensure.side_effect = api_football.ApiFootballError('Historique indisponible pour ce compte.')
        self._select(101, 102)
        self._predict()
        self.missing.assert_called_once_with('API-Football', '39', [2025, 2026])
        self.ensure.assert_called_once_with('API-Football', '39', [2026])
        self.assertTrue(any('Historique indisponible' in row.value for row in self.at.error))
        self.assertTrue(self.at.checkbox(key=match_key(101)).value)
        self.assertTrue(self.at.checkbox(key=match_key(102)).value)
        self.assertFalse(self._scenario_metrics())
        self.estimate.assert_not_called()
        self._assert_idle_network()

    def test_empty_calendar_explains_refresh_without_reading_history_or_calling_api(self):
        self.snapshot.side_effect = None
        self.snapshot.return_value = {
            'competitions': {}, 'fixtures': [], 'day_count': 0,
            'usable_count': len(self.rows), 'updated_at': None,
        }
        self.at.run()
        self.assertFalse(self.at.exception)
        self.assertTrue(any('Aucun match enregistré localement pour cette date' in row.value
                            for row in self.at.info))
        self.assertFalse(self.at.checkbox)
        self.assertFalse(any(button.key == 'workspace_predict' for button in self.at.button))
        self.assertFalse(self.at.button(key='workspace_refresh').disabled)
        self.history.assert_not_called()
        self.missing.assert_not_called()
        self.ensure.assert_not_called()
        self.refresh.assert_not_called()
        self.estimate.assert_not_called()
        self._assert_idle_network()

    def test_export_contains_only_the_api_provider_and_selected_competition(self):
        self._select(101, 102)
        with patch.object(panel.st, 'download_button', wraps=panel.st.download_button) as download:
            self._predict()
        exported = json.loads(download.call_args.args[1])
        self.assertEqual(exported['source'], 'API-Football')
        self.assertTrue(all(row['provider'] == 'api-football' for row in exported['fixtures']))
        self.assertTrue(all(row['league'] == '39' for row in exported['fixtures']))
        self._assert_idle_network()

    def test_api_data_revision_is_part_of_snapshot_and_history_cache_keys(self):
        CACHED_SNAPSHOT.clear()
        CACHED_HISTORY.clear()
        CACHED_READINESS_HISTORY.clear()
        self.addCleanup(CACHED_SNAPSHOT.clear)
        self.addCleanup(CACHED_HISTORY.clear)
        self.addCleanup(CACHED_READINESS_HISTORY.clear)
        with patch.object(panel.sources, 'local_snapshot', return_value={'source':'API-Football'}) as read_day, \
             patch.object(panel.sources, 'load_matches', return_value=([], {})) as read_history:
            for revision in (1, 1, 2):
                signature = (str(self.root), revision, 1)
                self.assertEqual(CACHED_SNAPSHOT(DAY, signature, 'API-Football')['source'], 'API-Football')
                self.assertEqual(CACHED_HISTORY(signature, 'API-Football'), ([], {}))
                self.assertEqual(CACHED_READINESS_HISTORY(signature, 'API-Football'), [])
            self.assertEqual(read_day.call_count, 2)
            self.assertEqual(read_history.call_count, 4)

    def test_selected_group_readiness_is_local_and_shows_exact_missing_history(self):
        rows = [row for row in self.rows if row['league'] == '39'][:39]
        with patch.object(panel, '_readiness_history', return_value=rows) as read:
            self._select(101,102)
        self.assertFalse(self.at.exception)
        self.assertTrue(any(item.label == 'Groupes historiques complets' and item.value == '19 / 20 minimum'
                            for item in self.at.metric))
        self.assertTrue(any('Au moins 1 résultat(s)' in item.value for item in self.at.info))
        self.assertTrue(any('2026-10-18 (UTC)' in item.value for item in self.at.caption))
        self.assertFalse(self.at.button(key='workspace_predict').disabled)
        self.estimate.assert_not_called()
        self.ensure.assert_not_called()
        self.missing.assert_not_called()
        self.api_network.assert_not_called()
        self.assertEqual(read.call_count,2)  # Competition browser and selected group's readiness.

    def test_selected_group_ready_state_does_not_claim_measured_accuracy(self):
        self._select(101,102)
        self.assertTrue(any(item.label == 'Groupes historiques complets' and item.value == '40 / 20 minimum'
                            for item in self.at.metric))
        self.assertTrue(any('vérifie la quantité d’historique' in item.value for item in self.at.caption))
        self.estimate.assert_not_called()
        self.ensure.assert_not_called()
        self.api_network.assert_not_called()

    def test_invalid_source_preference_stops_before_calendar_and_links_to_settings(self):
        self.source.side_effect = panel.sources.WorkspaceSourceError('Le choix de la source est illisible.')
        self.snapshot.reset_mock()
        self.at.run()
        self.assertFalse(self.at.exception)
        self.assertTrue(any('source est illisible' in row.value for row in self.at.error))
        self.assertTrue(self.at.get('page_link'))
        self.snapshot.assert_not_called()
        self.history.assert_not_called()
        self.estimate.assert_not_called()
        self.refresh.assert_not_called()
        self._assert_idle_network()

    def test_api_calendar_error_is_displayed_with_the_active_source(self):
        self.source.return_value = 'API-Football'
        self.snapshot.side_effect = api_football.ApiFootballError('Calendrier API-Football indisponible.')
        self.at.run()
        self.assertFalse(self.at.exception)
        self.assertTrue(any('Calendrier API-Football indisponible' in row.value for row in self.at.error))
        self.assertTrue(any('Source active : API-Football' in row.value for row in self.at.caption))
        self.history.assert_not_called()
        self.estimate.assert_not_called()
        self.refresh.assert_not_called()
        self._assert_idle_network()

    def test_catboost_is_explicit_and_new_generation_invalidates_display(self):
        self.ml_report.return_value = {'deployment': {'generation': 'one', 'trained_through': '2026-10-17'}}
        learned = self._patch(panel, '_ml_estimate', side_effect=lambda rows, fixtures, source, generation:
                              panel.forecast(panel._history_result(rows, fixtures[0]['league'], len(fixtures)), fixtures))
        self.at.run()
        self.assertEqual(self.at.selectbox(key='workspace_engine_API-Football_39').value, 'statistical')
        self.at.selectbox(key='workspace_engine_API-Football_39').select('catboost').run()
        self._select(101, 102)
        self._predict()
        self.assertFalse(self.at.exception)
        learned.assert_called_once()
        self.estimate.assert_not_called()
        self.assertEqual(self.at.session_state['workspace_forecast']['engine'], 'catboost')
        self.assertFalse(any(item.key == 'workspace_evaluate' for item in self.at.button))
        self.assertEqual(len(self._scenario_metrics()), 1)
        self.ml_report.return_value['deployment']['generation'] = 'two'
        self.at.run()
        self.assertFalse(self.at.exception)
        self.assertEqual(len(self._scenario_metrics()), 0)
        self._assert_idle_network()

    def test_context_engine_is_explicit_explains_neighbors_and_tracks_generation(self):
        self.context_report.return_value = {'deployment': {'generation': 'context-one',
                                                          'selection_through': '2026-05-24'}}
        def contextual_estimate(rows, fixtures, source, generation):
            estimate = panel.forecast(panel._history_result(rows, '39', len(fixtures)), fixtures)
            baseline = [item['probabilities'] for item in estimate['club_estimates']]
            from coupon_sizes import top_independent
            estimate['ranking'] = top_independent(baseline, 3)
            sequence = list(estimate['ranking'][0]['sequence'])
            past = dict(sequence=sequence, first_day='2026-08-01', end_day='2026-08-01',
                        season=2026, distance=.1, weight=1., match_ids=['1', '2'], matches=[])
            estimate.update(parameters=dict(k=20, half_life_days=None, mix=0),
                            support=dict(neighbors=1, ess=1., eligible=False), neighbors=[past],
                            distribution=dict(eta=0, baseline=baseline,
                                              local_mass=[dict(sequence=sequence, probability=1.)]),
                            diagnostics=['Support local insuffisant.'])
            return estimate
        engine = self._patch(panel, '_context_estimate', side_effect=contextual_estimate)
        self.at.run()
        self.assertEqual(self.at.selectbox(key='workspace_engine_API-Football_39').value, 'statistical')
        self.at.selectbox(key='workspace_engine_API-Football_39').set_value('context_neighbors').run()
        self._select(101, 102)
        self._predict()
        self.assertFalse(self.at.exception)
        engine.assert_called_once()
        self.estimate.assert_not_called()
        self.assertEqual(self.at.session_state['workspace_forecast']['engine'], 'context_neighbors')
        self.assertTrue(any('poids nul' in item.value for item in self.at.info))
        self.assertTrue(any('Identifiants API des matchs' in item.value.columns for item in self.at.dataframe))
        self.assertEqual(self.journal_register.call_args.args[3], 'context_neighbors')
        self.context_report.return_value['deployment']['generation'] = 'context-two'
        self.at.run()
        self.assertFalse(self.at.exception)
        self.assertFalse(any('Estimation du scénario complet' in item.label for item in self.at.metric))
        self._assert_idle_network()

    def test_missing_context_configuration_disables_prediction_without_silent_fallback(self):
        engine = self._patch(panel, '_context_estimate')
        self.at.selectbox(key='workspace_engine_API-Football_39').set_value('context_neighbors').run()
        self._select(101, 102)
        self.assertTrue(self.at.button(key='workspace_predict').disabled)
        engine.assert_not_called()
        self.estimate.assert_not_called()
        self.assertTrue(any('indisponibles' in item.value for item in self.at.info))
        self._assert_idle_network()

    def test_catboost_error_keeps_selection_and_never_switches_engine(self):
        self.ml_report.return_value = {'deployment': {'generation': 'one', 'trained_through': '2026-10-17'}}
        learned = self._patch(panel, '_ml_estimate', side_effect=panel.ml.ModelError('Modèle périmé : réentraînez-le'))
        self.at.run()
        self.at.selectbox(key='workspace_engine_API-Football_39').select('catboost').run()
        self._select(101, 102)
        self._predict()
        learned.assert_called_once()
        self.estimate.assert_not_called()
        self.assertTrue(any('Modèle périmé' in row.value for row in self.at.error))
        self.assertTrue(self.at.checkbox(key=match_key(101)).value)
        self.assertTrue(self.at.checkbox(key=match_key(102)).value)
        self.assertEqual(len(self._scenario_metrics()), 0)
        self._assert_idle_network()

    def test_missing_catboost_report_blocks_selected_engine_without_switching(self):
        self.ml_report.return_value = {'deployment': {'generation': 'one', 'trained_through': '2026-10-17'}}
        self.at.run()
        self.at.selectbox(key='workspace_engine_API-Football_39').select('catboost').run()
        self._select(101, 102)
        self.ml_report.side_effect = panel.ml.ModelError('Le rapport du modèle est invalide')
        self.at.run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.at.selectbox(key='workspace_engine_API-Football_39').value, 'catboost')
        self.assertTrue(self.at.button(key='workspace_predict').disabled)
        self.estimate.assert_not_called()
        self._assert_idle_network()


if __name__ == '__main__':
    unittest.main()
