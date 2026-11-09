"""Day membership filters club profiles without changing their historical trends."""
from copy import deepcopy
import csv
from datetime import date
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import api_football as api
import club_trends as trends
import club_trends_day as day_trends
import club_trends_panel as panel
import fixture_store
from test_competition_trends import raw
from test_club_trends_day_panel import day_fixture

DAY = date(2026, 11, 6)


def history(mid, home, away, *, league=39, value='2nd'):
    result = raw(mid, value, stamp='2026-10-15T12:00:00Z', league=league)
    result['teams'] = {'home': {'id': home, 'name': f'Club {home}'},
                       'away': {'id': away, 'name': f'Club {away}'}}
    return result


class ClubDayMembershipTests(unittest.TestCase):
    def test_exact_ids_both_sides_all_statuses_and_competition_preserve_profile_role(self):
        profiles = [dict(team_id='99', league='39', role='home', matches=12, club='Renamed'),
                    dict(team_id='98', league='39', role='home', matches=7, club='Same name'),
                    dict(team_id='99', league='61', role='home', matches=40, club='Renamed'),
                    dict(team_id='100', league='39', role='home', matches=3, club='Same name')]
        fixtures = [dict(league='39', home_id=99, away_id='98', status_group='finished'),
                    dict(league='39', home_id='99', away_id='777', status_group='other'),
                    dict(league='39', home_id='٩٩', away_id=True, status_group='upcoming'),
                    dict(league=None, home_id='99', away_id='100')]
        before = deepcopy((profiles, fixtures))
        result = day_trends.filter_profiles(profiles, fixtures)
        self.assertEqual(result['rows'], profiles[:2])
        self.assertEqual((result['participating_profiles'], result['missing_profiles']), (3, 1))
        self.assertEqual([row['role'] for row in result['rows']], ['home', 'home'])
        self.assertEqual((profiles, fixtures), before)
        self.assertEqual(day_trends.filter_profiles(profiles, [])['rows'], [])


class ClubTrendsDayFilterPanelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)/'fixtures.json'
        historical = [history(1, 99, 98, value='1st'), history(2, 99, 98, value='Égal'),
                      history(3, 98, 99), history(4, 99, 900, league=61),
                      history(5, 555, 556, league=61), history(6, 333, 334), history(7, 444, 445)]
        self.calendar = [day_fixture(1001, '2026-11-05T23:00:00Z'),
                         day_fixture(1002, '2026-11-06T22:59:59Z', 'PST', league=61, home=555, away=556),
                         day_fixture(1003, '2026-11-05T22:59:59Z', home=333, away=334),
                         day_fixture(1004, '2026-11-06T23:00:00Z', home=444, away=445),
                         day_fixture(1005, '2026-11-06T20:00:00Z', home=777, away=778)]
        self.path.write_text(json.dumps(dict(version=1, fixtures=historical+self.calendar, season_imports=[])))
        fixture_store.migrate_json(self.path)
        for name in ('_catalogue', '_summary', '_details', '_day_calendar'):
            function = getattr(panel, name)
            function.clear(); self.addCleanup(function.clear)
        self._patch(api, 'STORE_PATH', self.path)
        self.build = self._patch(trends, 'build', wraps=trends.build)
        self.network = self._patch(api.ApiFootballClient, 'get', side_effect=AssertionError('No API'))
        self.full = self._patch(api, 'load_fixtures', side_effect=AssertionError('No full raw load'))
        self.migrate = self._patch(fixture_store, 'migrate_json', side_effect=AssertionError('No screen migration'))
        self.at = AppTest.from_string('from club_trends_panel import render\nrender()', default_timeout=20)
        self.at.session_state['analysis_selected_day'] = DAY
        self.at.session_state['analysis_follow_today'] = False
        self.at.run(); self.assertFalse(self.at.exception)
        # Historical window remains independent of today's membership.
        self.at.date_input(key='club_trends_end').set_value(date(2026,11,5))
        self.apply()

    def _patch(self, target, name, *args, **kwargs):
        handle = patch.object(target, name, *args, **kwargs)
        value = handle.start(); self.addCleanup(handle.stop)
        return value

    def apply(self):
        next(item for item in self.at.button if item.label=='Afficher et actualiser').click().run()
        self.assertFalse(self.at.exception)

    def table(self):
        return next(frame.value for frame in self.at.dataframe if 'Tendance dominante' in frame.value.columns)

    def identities(self):
        return set(zip(self.table()['ID club API'], self.table()['ID compétition API']))

    def no_side_effects(self):
        self.network.assert_not_called(); self.full.assert_not_called(); self.migrate.assert_not_called()

    def test_toggle_filters_both_clubs_clamps_selectors_and_restores_full_history(self):
        self.assertFalse(self.at.toggle(key='club_trends_day_only').value)
        original = self.identities()
        self.at.selectbox(key='club_trends_country').set_value('France').run()
        self.at.selectbox(key='club_trends_competition').set_value('61').run()
        self.at.toggle(key='club_trends_day_only').set_value(True).run()
        self.assertFalse(self.at.exception)
        # France remains valid for the postponed fixture; narrowing to a day
        # without French clubs must clamp both stale selectors automatically.
        self.at.session_state['analysis_selected_day'] = date(2026,11,7)
        self.at.run(); self.assertFalse(self.at.exception)
        self.assertIsNone(self.at.selectbox(key='club_trends_country').value)
        self.assertIsNone(self.at.selectbox(key='club_trends_competition').value)
        self.assertEqual(self.identities(), {('444','39'), ('445','39')})
        self.at.session_state['analysis_selected_day'] = DAY
        self.at.run(); self.assertFalse(self.at.exception)
        self.assertTrue({('99','39'), ('98','39'), ('555','61'), ('556','61')} <= self.identities())
        self.assertNotIn(('99','61'), self.identities())
        self.at.toggle(key='club_trends_day_only').set_value(False).run()
        self.assertFalse(self.at.exception)
        self.assertTrue(original <= self.identities())
        self.no_side_effects()

    def test_historical_role_counts_and_cached_history_survive_membership_filter(self):
        self.at.selectbox(key='club_trends_role').set_value('home')
        self.apply()
        original = self.table().copy(deep=True)
        calls = self.build.call_count
        self.at.toggle(key='club_trends_day_only').set_value(True).run()
        self.assertFalse(self.at.exception)
        rows = self.table().set_index(['ID club API','ID compétition API'])
        self.assertEqual(rows.loc[('99','39'),'Matchs exploitables'], 2)
        # Club98 plays away today but its selected historical home profile stays.
        self.assertEqual(rows.loc[('98','39'),'Matchs exploitables'], 1)
        self.assertEqual(self.at.selectbox(key='club_trends_role').value, 'home')
        self.at.toggle(key='club_trends_day_only').set_value(False).run()
        self.assertFalse(self.at.exception)
        __import__('pandas').testing.assert_frame_equal(self.table(), original)
        self.assertEqual(self.build.call_count, calls)
        self.no_side_effects()

    def test_day_boundary_empty_day_and_missing_history_are_explicit(self):
        calls = self.build.call_count
        self.at.toggle(key='club_trends_day_only').set_value(True).run()
        self.assertFalse(self.at.exception)
        self.assertNotIn(('333','39'), self.identities())
        self.assertNotIn(('444','39'), self.identities())
        self.assertTrue(any('2 profil(s)' in node.value and 'sans historique' in node.value for node in self.at.caption))
        self.assertEqual(self.build.call_count, calls)
        self.at.session_state['analysis_selected_day'] = date(2026,11,8)
        self.at.run(); self.assertFalse(self.at.exception)
        self.assertTrue(any('Aucun match enregistré pour cette journée' in node.value for node in self.at.info))
        self.assertFalse(any('Tendance dominante' in frame.value.columns for frame in self.at.dataframe))
        self.no_side_effects()

    def test_paginated_export_contains_all_day_profiles_and_precise_filter_date(self):
        extra = []
        for index in range(7):
            home, away = 2000+2*index, 2001+2*index
            extra += [history(200+index, home, away),
                      day_fixture(2001+index, f'2026-11-06T19:{index:02}:00Z', home=home, away=away)]
        fixture_store.upsert(fixture_store.database_path(self.path), extra)
        self.apply()
        calls = self.build.call_count
        self.at.toggle(key='club_trends_day_only').set_value(True).run()
        self.assertFalse(self.at.exception)
        total = len(self.table())
        self.assertEqual(total, 18)
        with patch.object(panel.st,'download_button', wraps=panel.st.download_button) as download:
            self.at.selectbox(key='club_trends_summary_limit').set_value(10).run()
            self.assertFalse(self.at.exception)
            self.assertEqual(len(self.table()), 10)
            self.at.selectbox(key='club_trends_summary_page').set_value(2).run()
            self.assertEqual(len(self.table()), 8)
            kwargs = download.call_args.kwargs
        content = kwargs['data']() if callable(kwargs['data']) else kwargs['data']
        exported = list(csv.DictReader(StringIO(content.decode('utf-8-sig')),delimiter=';'))
        self.assertEqual(len(exported), total)
        self.assertEqual({row['Filtre matchs · journée de Kinshasa'] for row in exported}, {DAY.isoformat()})
        self.assertEqual({row['Fin incluse (Kinshasa)'] for row in exported}, {'2026-11-05'})
        self.assertEqual(kwargs['file_name'],'tendances_clubs_matchs_2026-11-06_all.csv')
        self.at.toggle(key='club_trends_day_only').set_value(False).run()
        self.assertEqual(self.at.selectbox(key='club_trends_summary_page').value,1)
        self.assertEqual(self.build.call_count,calls)
        self.no_side_effects()


if __name__=='__main__':unittest.main()
