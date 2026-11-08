"""The club screen queries real SQLite; controls must never trigger provider calls."""
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import api_football as api
import club_trends as trends
import club_trends_panel as panel
import fixture_store
from test_competition_trends import raw


def fixture(mid, *, league=39, season=2026, invalid=False):
    row = raw(mid, ('1st', 'Égal', '2nd')[mid % 3], league=league, season=season)
    role = 'home' if mid % 2 else 'away'
    row['teams'][role] = {'id': 99, 'name': 'Club témoin'}
    if invalid:
        row['score']['halftime']['home'] = None
    return row


class ClubTrendsPanelTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'fixtures.json'
        rows = [fixture(mid) for mid in range(1, 25)]
        rows += [fixture(25, league=61, season=2025), fixture(26, league=61),
                 fixture(27, league=61, invalid=True)]
        self.path.write_text(json.dumps({'version': 1, 'fixtures': rows, 'season_imports': []}))
        fixture_store.migrate_json(self.path)
        for cached in (panel._catalogue, panel._summary, panel._details):
            cached.clear()
            self.addCleanup(cached.clear)
        self._patch(api, 'STORE_PATH', self.path)
        self.build = self._patch(trends, 'build', wraps=trends.build)
        self.detail = self._patch(trends, 'details', wraps=trends.details)
        self.network = self._patch(api.ApiFootballClient, 'get', side_effect=AssertionError('No API'))
        self.full_raw = self._patch(api, 'load_fixtures', side_effect=AssertionError('No full raw load'))
        self.migrate = self._patch(fixture_store, 'migrate_json', side_effect=AssertionError('No migration'))
        self.at = AppTest.from_string('from club_trends_panel import render\nrender()', default_timeout=15)
        self.at.session_state['analysis_selected_day'] = date(2026, 11, 6)
        self.at.session_state['analysis_follow_today'] = False
        self.at.run()
        self.assertFalse(self.at.exception)

    def _patch(self, target, name, *args, **kwargs):
        patcher = patch.object(target, name, *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def table(self, kind='summary'):
        column = {'summary': 'Tendance dominante', 'comparison': 'Écart (points)',
                  'matches': 'Compté dans la tendance'}[kind]
        return next(frame.value for frame in self.at.dataframe if column in frame.value.columns)

    def apply(self):
        next(item for item in self.at.button if item.label == 'Afficher et actualiser').click().run()
        self.assertFalse(self.at.exception)

    def test_counts_three_classes_ties_separate_competitions_and_unique_fixture_totals(self):
        table = self.table()
        target = table[table['ID club API'] == '99']
        self.assertEqual(list(target['Matchs exploitables']), [24, 2])
        self.assertEqual(list(target['Résultats exclus']), [0, 1])
        self.assertEqual(list(target.iloc[0]['Tendance dominante']), ['1st', 'Égal', '2nd'])
        for _, row in target.iterrows():
            self.assertAlmostEqual(sum(row[f'{value} (%)'] for value in ('1st', 'Égal', '2nd')), 100)
        totals = {item.label: item.value for item in self.at.metric}
        self.assertEqual(totals['Rencontres exploitables distinctes'], '26')
        self.assertEqual(totals['Résultats exclus distincts'], '1')
        self.detail.assert_not_called()
        self.network.assert_not_called(); self.full_raw.assert_not_called(); self.migrate.assert_not_called()

    def test_search_competition_country_sort_and_minimum_reuse_the_cached_source(self):
        self.at.text_input(key='club_trends_search').set_value('témoin').run()
        self.assertEqual(len(self.table()), 2)
        self.at.selectbox(key='club_trends_country').set_value('France').run()
        self.assertEqual(len(self.table()), 1)
        self.at.selectbox(key='club_trends_competition').set_value('61').run()
        self.at.selectbox(key='club_trends_sort').set_value('1st').run()
        self.at.number_input(key='club_trends_minimum').set_value(3).run()
        self.assertFalse(self.at.exception)
        self.assertTrue(any('Aucun club' in item.value for item in self.at.info))
        self.assertEqual(self.build.call_count, 1)

    def test_role_is_applied_to_actual_history_and_recent_baseline_is_disjoint(self):
        comparison = self.table('comparison')
        self.assertEqual(set(comparison['Matchs récents']), {10})
        self.assertEqual(set(comparison['Matchs précédents']), {14})
        self.assertEqual(list(comparison['Effectif sur la période']), [8, 8, 8])
        self.assertEqual(list(comparison['Effectif récent']), [4, 3, 3])
        self.assertEqual(list(comparison['Effectif précédent']), [4, 5, 5])
        self.assertAlmostEqual(comparison.iloc[0]['Écart (points)'], 40 - 400 / 14)
        self.at.selectbox(key='club_trends_role').set_value('home')
        self.at.selectbox(key='club_trends_recent').set_value(5)
        self.apply()
        target = self.table()
        target = target[target['ID club API'] == '99']
        self.assertEqual(list(target['Matchs exploitables']), [12, 1])
        self.assertEqual(set(self.table('comparison')['Matchs récents']), {5})
        self.at.selectbox(key='club_trends_role').set_value('away')
        self.apply()
        target = self.table()
        target = target[target['ID club API'] == '99']
        self.assertEqual(list(target['Matchs exploitables']), [12, 1])
        self.assertEqual(self.build.call_count, 3)

    def test_summary_pagination_bounds_the_table_but_not_profile_selection_or_export(self):
        total = len(self.table())
        self.assertGreater(total, 20)
        profiles = list(self.at.selectbox(key='club_trends_profile').options)
        self.at.selectbox(key='club_trends_summary_limit').set_value(10).run()
        self.assertEqual(len(self.table()), 10)
        self.assertEqual(list(self.at.selectbox(key='club_trends_profile').options), profiles)
        self.at.selectbox(key='club_trends_summary_page').set_value(3).run()
        self.assertEqual(len(self.table()), total - 20)
        self.assertTrue(any(f'Profils 21–{total} sur {total}' in item.value for item in self.at.caption))
        self.assertTrue(any(item.label == 'Exporter tous les profils filtrés (CSV)'
                            for item in self.at.get('download_button')))
        # Filtering from the last page must select a valid first page again.
        self.at.text_input(key='club_trends_search').set_value('témoin').run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.at.selectbox(key='club_trends_summary_page').value, 1)
        self.assertEqual(len(self.table()), 2)
        self.assertEqual(self.build.call_count, 1)

    def test_on_demand_match_proof_keeps_scores_verdicts_roles_and_pagination(self):
        self.at.toggle(key='club_trends_show_matches').set_value(True).run()
        self.assertFalse(self.at.exception)
        table = self.table('matches')
        self.assertEqual(len(table), 24)
        self.assertTrue(table['Compté dans la tendance'].all())
        self.assertEqual(set(table['Rôle du club']), {'À domicile', 'À l’extérieur'})
        self.assertTrue(all(list(value) for value in table['Verdict final']))
        self.assertFalse(table['Buts marqués 2e période'].isna().any())
        self.assertTrue(all('13:00' in value for value in table['Coup d’envoi (Kinshasa)']))
        self.at.selectbox(key='club_trends_match_limit').set_value(10).run()
        self.at.selectbox(key='club_trends_match_page').set_value(3).run()
        self.assertEqual(len(self.table('matches')), 4)
        self.at.selectbox(key='club_trends_profile').set_value('99:61').run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.at.selectbox(key='club_trends_match_page').value, 1)
        evidence = self.table('matches')
        self.assertEqual(len(evidence), 3)
        self.assertEqual(sum(evidence['Compté dans la tendance']), 2)
        self.assertIn('Score HT/FT absent', ' '.join(evidence['Motif']))

    def test_season_period_and_missing_database_have_explicit_empty_or_error_states(self):
        self.at.multiselect(key='club_trends_seasons').set_value([2025])
        self.apply()
        self.assertTrue((self.table()['Matchs exploitables'] == 1).all())
        self.at.date_input(key='club_trends_start').set_value(date(2026, 11, 1))
        self.apply()
        self.assertTrue(any('Aucune rencontre' in item.value for item in self.at.info))
        self.at.date_input(key='club_trends_start').set_value(date(2026, 12, 1))
        self.apply()
        self.assertTrue(any('doit précéder' in item.value for item in self.at.warning))
        panel._catalogue.clear()
        with patch.object(trends, 'catalogue', side_effect=trends.TrendsError('Base indisponible')):
            at = AppTest.from_string('from club_trends_panel import render\nrender()').run()
        self.assertFalse(at.exception)
        self.assertTrue(any('Base indisponible' in item.value for item in at.warning))


if __name__ == '__main__':
    unittest.main()
