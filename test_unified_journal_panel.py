"""Saved manual calculations use the regular journal browser without joining cohorts."""
from test_table_view_support import static_frames, badge_text
import copy
from datetime import date, datetime, timedelta, timezone
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import analysis_date
import api_football as api
import catboost_scenarios as ml
import journal_panel as panel
import manual_analysis_store as archive
import prediction_journal as journal
import prospective_models as models
import test_journal_panel as helpers
from test_manual_analysis_store import raw as manual_raw


class UnifiedJournalPanelTests(unittest.TestCase):
    # Reuse the established application harness without inheriting its tests.
    patch = helpers.JournalPanelTests.patch
    open = helpers.JournalPanelTests.open
    buttons = helpers.JournalPanelTests.buttons
    view = helpers.JournalPanelTests.view
    seed = helpers.JournalPanelTests.seed

    def setUp(self):
        helpers.JournalPanelTests.setUp(self)
        self.enterContext(patch.dict(os.environ, {'PATTERNLAB_STORAGE_MODE': 'local'}))
        self.archive_path = self.root / 'manual-test.sqlite3'
        self.patch(archive, 'database_path', side_effect=lambda path=None:
                   self.root / 'manual_analyses.sqlite3' if path is None else Path(path))
        read, days, get = archive.list_entries, archive.days, archive.get
        self.patch(archive, 'list_entries', side_effect=lambda path=None, day=None:
                   read(self.archive_path if path is None else path, day=day))
        self.patch(archive, 'days', side_effect=lambda path=None:
                   days(self.archive_path if path is None else path))
        self.patch(archive, 'get', side_effect=lambda identity, path=None:
                   get(identity, self.archive_path if path is None else path))

    def seed_manual(self, *, size=2, day=None, offset=0, finished=False):
        day = self.day.return_value if day is None else day
        captured_day = min(day, self.day.return_value)
        created = datetime.combine(captured_day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=6)
        fixtures = [dict(match_id=str(12000 + offset + index), league='39',
                         home_id=str(22000 + offset + 2 * index),
                         away_id=str(22001 + offset + 2 * index),
                         home=f'Saved home {offset + index}', away=f'Saved away {offset + index}',
                         timestamp=f'{day.isoformat()}T18:{index:02d}:00', provider='api-football',
                         status='TIMED', raw_status='NS', is_upcoming=True)
                    for index in range(size)]
        value = dict(source='API-Football', engine='statistical', fixtures=fixtures,
                     competition=dict(competition='Saved league', country='England'), country='England',
                     model_generation='saved-generation', history_cutoff_exclusive=captured_day.isoformat(),
                     forecast=dict(eligible=True, version='saved-calculation-v1', size=size, league='39',
                                   before=day.isoformat(),
                                   latest=(created - timedelta(days=1)).isoformat(),
                                   ranking=[dict(sequence=[label] * size, probability=probability)
                                            for label, probability in [('1st', .312345), ('Égal', .2), ('2nd', .1)]]))
        entry = archive.record(value, path=self.archive_path, clock=lambda: created)
        self.raw += [manual_raw(fixture, status='FT' if finished else 'NS') for fixture in fixtures]
        return entry

    def button_keys(self, at):
        return {button.key for button in self.buttons(at)}

    def manual_key(self, entry):
        return f'journal_browser_open_manual:{entry["id"]}'

    def manual_table(self, at):
        return next(frame for frame in static_frames(at) if 'Probabilité enregistrée (moteur choisi)' in frame.columns)

    def test_manual_origin_includes_saved_archive_and_size_filters_keep_complete_groups(self):
        saved_two = self.seed_manual()
        saved_twenty = self.seed_manual(size=20, offset=100)
        confirmed_manual = self.seed(size=3)
        automatic = self.seed(size=2, offset=100, automatic=True, finished=False)
        before = self.archive_path.read_bytes()
        at = self.open()
        self.assertFalse(any(item.key == 'journal_type' for item in at.segmented_control))
        self.assertEqual(self.button_keys(at), {self.manual_key(saved_two), self.manual_key(saved_twenty),
                         f'journal_browser_open_{confirmed_manual["id"]}', f'journal_browser_open_{automatic["id"]}'})
        self.assertEqual(len(at.selectbox(key='journal_browser_size').options), 20)
        at.selectbox(key='journal_browser_origin').set_value('manual').run()
        self.assertFalse(at.exception)
        self.assertEqual(self.button_keys(at), {self.manual_key(saved_two), self.manual_key(saved_twenty),
                         f'journal_browser_open_{confirmed_manual["id"]}'})
        at.selectbox(key='journal_browser_size').set_value(20).run()
        self.assertFalse(at.exception)
        self.assertEqual(self.button_keys(at), {self.manual_key(saved_twenty)})
        at.selectbox(key='journal_browser_origin').set_value('automatic').run()
        self.assertFalse(at.exception)
        self.assertFalse(self.buttons(at))
        self.assertTrue(any('Aucune analyse ne correspond' in item.value for item in at.info))
        self.assertTrue(any(item.key == 'journal_manual_export' for item in at.get('download_button')))
        at.selectbox(key='journal_browser_size').set_value(2).run()
        self.assertEqual(self.button_keys(at), {f'journal_browser_open_{automatic["id"]}'})
        at.selectbox(key='journal_browser_origin').set_value('manual').run()
        self.assertFalse(at.exception)
        self.assertEqual(self.button_keys(at), {self.manual_key(saved_two)})
        self.assertEqual(self.archive_path.read_bytes(), before)
        self.network.assert_not_called()

    def test_manual_only_twenty_match_detail_preserves_exact_ranking_across_reruns_and_back(self):
        saved = self.seed_manual(size=20)
        before = self.archive_path.read_bytes()
        snapshot = copy.deepcopy(archive.list_entries())
        build = self.patch(models, 'build_methods', side_effect=AssertionError('No recomputed prediction'))
        forecast = self.patch(ml, 'forecast', side_effect=AssertionError('No current inference'))
        record = self.patch(archive, 'record', side_effect=AssertionError('Browsing must not archive again'))
        at = self.open()
        self.assertEqual(self.button_keys(at), {self.manual_key(saved)})
        self.assertFalse(journal.DB_PATH.exists())
        at.selectbox(key='journal_browser_origin').set_value('manual').run()
        at.selectbox(key='journal_browser_size').set_value(20).run()
        at.text_input(key='journal_browser_query').set_value('Saved home 19').run()
        at = self.view(at, 'manual:' + saved['id'])
        matches = next(item.value for item in at.dataframe if 'Domicile' in item.value.columns)
        self.assertEqual(len(matches), 20)
        self.assertEqual(list(matches['Domicile']), [fixture['home'] for fixture in saved['fixtures']])
        self.assertEqual(list(self.manual_table(at)['Scénario complet']),
                         [badge_text(panel.manual_analysis_panel._colored_sequence(item['sequence'])) for item in saved['forecast']['ranking']])
        for observed, expected in zip(self.manual_table(at)['Probabilité enregistrée (moteur choisi)'],
                                      saved['forecast']['ranking']):
            self.assertEqual(observed, f"{100 * expected['probability']:.2f} %".replace('.', ','))
        self.assertFalse(any(button.key == 'journal_refresh' for button in at.button))
        self.assertFalse(any(item.key.startswith('journal_compare_') for item in at.toggle))
        at.run()
        self.assertFalse(at.exception)
        self.assertEqual(len(self.manual_table(at)), 3)
        at.button(key='journal_back').click().run()
        self.assertFalse(at.exception)
        self.assertEqual(at.selectbox(key='journal_browser_origin').value, 'manual')
        self.assertEqual(at.selectbox(key='journal_browser_size').value, 20)
        self.assertEqual(at.text_input(key='journal_browser_query').value, 'Saved home 19')
        self.assertEqual(self.button_keys(at), {self.manual_key(saved)})
        self.assertEqual(archive.list_entries(), snapshot)
        self.assertEqual(self.archive_path.read_bytes(), before)
        build.assert_not_called(); forecast.assert_not_called(); record.assert_not_called()
        self.network.assert_not_called()

    def test_manual_only_history_is_available_when_today_has_no_rows(self):
        yesterday = date(2025, 3, 19)
        earlier = date(2025, 3, 18)
        past = self.seed_manual(day=yesterday, finished=True)
        older = self.seed_manual(day=earlier, offset=100, finished=True)
        self.seed_manual(day=date(2025, 3, 21), offset=200)
        before = self.archive_path.read_bytes()
        at = self.open()
        self.assertFalse(self.buttons(at))
        self.assertEqual(at.segmented_control(key='journal_view').value, 'today')
        at.segmented_control(key='journal_view').set_value('history').run()
        self.assertFalse(at.exception)
        selector = at.selectbox(key='journal_history_date')
        self.assertEqual(len(selector.options), 2)
        self.assertIn('19/03/2025', selector.options[0])
        self.assertIn('18/03/2025', selector.options[1])
        self.assertEqual(self.button_keys(at), {self.manual_key(past)})
        at = self.view(at, 'manual:' + past['id'])
        self.assertEqual(list(self.manual_table(at)['Suivi du scénario']),
                         ['Réalisé', 'Non réalisé', 'Non réalisé'])
        at.button(key='journal_back').click().run()
        self.assertFalse(at.exception)
        self.assertEqual(at.segmented_control(key='journal_view').value, 'history')
        at.selectbox(key='journal_history_date').set_value(earlier).run()
        self.assertFalse(at.exception)
        self.assertEqual(self.button_keys(at), {self.manual_key(older)})
        self.assertEqual(self.archive_path.read_bytes(), before)
        self.assertFalse(journal.DB_PATH.exists())
        self.network.assert_not_called()

    def test_official_comparison_never_receives_unconfirmed_manual_calculations(self):
        saved = self.seed_manual(size=4)
        confirmed = self.seed(size=4)
        before = copy.deepcopy(journal.list_entries()[0]['methods'])
        comparison = self.patch(panel.performance, 'comparison', wraps=panel.performance.comparison)
        at = self.open()
        self.assertEqual(len(self.buttons(at)), 2)
        at.selectbox(key='journal_browser_origin').set_value('manual').run()
        self.assertEqual(self.button_keys(at), {self.manual_key(saved), f'journal_browser_open_{confirmed["id"]}'})
        at = self.view(at, confirmed['id'], compare=True)
        table = next(item.value for item in at.dataframe if 'Groupes communs' in item.value.columns)
        self.assertEqual(set(table['Groupes communs']), {1})
        self.assertEqual({entry['id'] for entry in comparison.call_args.args[0]}, {confirmed['id']})
        self.assertEqual(journal.list_entries()[0]['methods'], before)
        self.assertEqual(len(journal.list_entries()), 1)
        self.network.assert_not_called()

    def test_manual_link_opens_exact_past_calculation_then_returns_to_existing_filters(self):
        saved = self.seed_manual(day=date(2025, 3, 19), size=3, finished=True)
        automatic = self.seed(size=2, automatic=True, finished=False)
        at = self.open()
        selected_day = at.session_state[analysis_date.DAY_KEY]
        at.selectbox(key='journal_browser_origin').set_value('automatic').run()
        at.selectbox(key='journal_browser_size').set_value(2).run()
        at.query_params['manual_analysis'] = saved['id']
        at.run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state['journal_selected_id'], 'manual:' + saved['id'])
        self.assertEqual(at.session_state[analysis_date.DAY_KEY], selected_day)
        self.assertNotIn('manual_analysis', at.query_params)
        matches = next(item.value for item in at.dataframe if 'Domicile' in item.value.columns)
        self.assertEqual(len(matches), 3)
        self.assertEqual(list(matches['Domicile']), [fixture['home'] for fixture in saved['fixtures']])
        at.run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state['journal_selected_id'], 'manual:' + saved['id'])
        at.button(key='journal_back').click().run()
        self.assertFalse(at.exception)
        self.assertEqual(at.selectbox(key='journal_browser_origin').value, 'automatic')
        self.assertEqual(at.selectbox(key='journal_browser_size').value, 2)
        self.assertEqual(self.button_keys(at), {f'journal_browser_open_{automatic["id"]}'})
        self.network.assert_not_called()

    def test_local_score_read_failure_keeps_saved_probabilities_without_success_claims(self):
        saved = self.seed_manual(day=date(2025, 3, 19), finished=True)
        before = self.archive_path.read_bytes()
        self.patch(api, 'load_fixtures', side_effect=api.ApiFootballError('Local catalogue unavailable'))
        at = self.open()
        at.segmented_control(key='journal_view').set_value('history').run()
        self.assertFalse(at.exception)
        self.assertEqual(self.button_keys(at), {self.manual_key(saved)})
        self.assertTrue(any('indisponibles' in item.value or 'ne peuvent pas être lus' in item.value
                            for item in at.warning))
        at = self.view(at, 'manual:' + saved['id'])
        table = self.manual_table(at)
        self.assertEqual(list(table['Suivi du scénario']), ['Vérification indisponible'] * 3)
        self.assertEqual(len(table), 3)
        self.assertEqual(table['Probabilité enregistrée (moteur choisi)'].iloc[0], '31,23 %')
        self.assertEqual(self.archive_path.read_bytes(), before)
        self.network.assert_not_called()

    def test_public_manual_browsing_never_reconciles_or_refreshes_official_results(self):
        saved = self.seed_manual()
        reconcile = self.patch(panel.tracking, 'reconcile_local', side_effect=AssertionError('Public view cannot reconcile'))
        refresh = self.patch(panel.tracking, 'refresh_entry', side_effect=AssertionError('No automatic refresh'))
        record = self.patch(archive, 'record', side_effect=AssertionError('Read-only browsing'))
        self.patch(panel.admin_access, 'can_admin', return_value=False)
        at = self.view(self.open(), 'manual:' + saved['id'])
        self.assertEqual(len(self.manual_table(at)), 3)
        self.assertFalse(any(button.key == 'journal_refresh' for button in at.button))
        reconcile.assert_not_called(); refresh.assert_not_called(); record.assert_not_called()
        self.network.assert_not_called()


if __name__ == '__main__':
    unittest.main()
