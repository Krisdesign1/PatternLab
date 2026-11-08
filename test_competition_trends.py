"""Real SQLite trend calculations, import-quality exclusions and bounded reads."""
from copy import deepcopy
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import competition_trends as trends
import fixture_store

NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
START, END = date(2026, 10, 1), date(2026, 11, 7)


def raw(mid, value='2nd', *, stamp=None, league=39, season=2026, status='FT'):
    ht, ft = {'1st': ((2, 0), (2, 1)), 'Égal': ((1, 0), (2, 0)), '2nd': ((0, 0), (1, 1))}[value]
    return {'fixture': {'id': mid, 'date': stamp or f'2026-10-{mid:02d}T12:00:00Z',
                        'status': {'short': status, 'elapsed': 90}},
            'league': {'id': league, 'name': 'Premier League' if league == 39 else 'Ligue 1',
                       'country': 'England' if league == 39 else 'France', 'season': season},
            'teams': {'home': {'id': mid * 2, 'name': f'Home {mid}'},
                      'away': {'id': mid * 2 + 1, 'name': f'Away {mid}'}},
            'score': {'halftime': dict(zip(('home', 'away'), ht)), 'fulltime': dict(zip(('home', 'away'), ft))},
            'goals': dict(zip(('home', 'away'), ft))}


class CompetitionTrendsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'fixtures.json'

    def store(self, rows):
        self.path.write_text(json.dumps({'version': 1, 'fixtures': rows, 'season_imports': []}))
        fixture_store.migrate_json(self.path)
        return fixture_store.database_path(self.path)

    def build(self, **kwargs):
        return trends.build(START, END, path=self.path, now=NOW, **kwargs)

    def test_three_class_counts_recent_comparison_is_disjoint_and_no_rounding(self):
        self.store([raw(1, '1st'), raw(2, 'Égal'), raw(3, '2nd'), raw(4, '2nd'), raw(5, '2nd')])
        report = self.build(recent=2)
        item = report['rows'][0]
        self.assertEqual(item['counts'], {'1st': 1, 'Égal': 1, '2nd': 3})
        self.assertEqual(item['percentages'], {'1st': 20.0, 'Égal': 20.0, '2nd': 60.0})
        self.assertEqual(item['recent_counts'], {'1st': 0, 'Égal': 0, '2nd': 2})
        self.assertEqual(item['previous_counts'], {'1st': 1, 'Égal': 1, '2nd': 1})
        self.assertAlmostEqual(item['delta_points']['2nd'], 100 - 100 / 3)
        self.assertEqual((item['recent_matches'], item['previous_matches']), (2, 3))
        self.assertEqual(report['totals'], dict(matches=5, excluded=0, not_finished=0, observed=5, competitions=1))

    def test_small_sample_has_no_previous_baseline_and_ties_remain_visible(self):
        self.store([raw(1, '1st'), raw(2, 'Égal'), raw(3, '2nd')])
        item = self.build(recent=30)['rows'][0]
        self.assertEqual(item['leaders'], ['1st', 'Égal', '2nd'])
        self.assertEqual(item['recent_matches'], 3)
        self.assertEqual(item['previous_matches'], 0)
        self.assertTrue(all(value is None for value in item['delta_points'].values()))

    def test_kinshasa_day_boundaries_and_now_exclude_future_kickoffs(self):
        self.store([raw(1, stamp='2026-10-01T22:59:59Z'), raw(2, stamp='2026-10-01T23:00:00Z'),
                    raw(3, stamp='2026-10-02T22:59:59Z'), raw(4, stamp='2026-10-02T23:00:00Z'),
                    raw(5, stamp='2026-11-07T12:00:01Z')])
        report = trends.build(date(2026, 10, 2), date(2026, 10, 2), path=self.path, now=NOW)
        self.assertEqual(report['totals']['matches'], 2)
        self.assertEqual(self.build()['totals']['matches'], 4)
        detail = trends.details('39', date(2026, 10, 2), date(2026, 10, 2), path=self.path, now=NOW)
        self.assertEqual([item['match_id'] for item in detail['rows']], ['3', '2'])
        self.assertEqual({item['local_day'] for item in detail['rows']}, {'2026-10-02'})

    def test_invalid_final_results_are_excluded_nonfinals_are_separate(self):
        good, missing, inconsistent, live, cancelled = [raw(i) for i in range(1, 6)]
        missing['score']['halftime']['away'] = None
        inconsistent['score']['halftime'] = {'home': 2, 'away': 0}
        inconsistent['score']['fulltime'] = {'home': 1, 'away': 3}
        live['fixture']['status']['short'] = '2H'
        cancelled['fixture']['status']['short'] = 'CANC'
        self.store([good, missing, inconsistent, live, cancelled])
        report = self.build()
        self.assertEqual(report['totals'], dict(matches=1, excluded=2, not_finished=2, observed=5, competitions=1))
        self.assertEqual(sum(report['exclusion_reasons'].values()), 2)
        detail = trends.details('39', START, END, path=self.path, now=NOW)
        self.assertEqual(sum(item['included'] for item in detail['rows']), 1)
        self.assertTrue(all(item['value'] is None for item in detail['rows'] if not item['included']))

    def test_quarantine_wrong_provider_and_same_identity_are_not_in_distribution(self):
        rows = [raw(i) for i in range(1, 5)]
        rows[1]['_patternlab_import'] = {'quarantined': True, 'reason': 'identity_changed'}
        rows[2]['provider'] = 'other-source'
        rows[3]['teams']['away']['id'] = rows[3]['teams']['home']['id']
        self.store(rows)
        self.assertEqual(self.build()['totals']['matches'], 1)
        self.assertEqual(self.build()['totals']['excluded'], 3)
        self.assertTrue(any('Quarantaine' in reason for reason in self.build()['exclusion_reasons']))

    def test_aet_pen_and_zero_draw_use_regulation_scores_only(self):
        rows = [raw(1, '1st', status='AET'), raw(2, 'Égal', status='PEN'), raw(3, 'Égal')]
        for item in rows:
            item['goals'] = {'home': 8, 'away': 4}
            item['score'].update(extratime={'home': 8, 'away': 4}, penalty={'home': 9, 'away': 8})
        rows[2]['score'] = {'halftime': {'home': 0, 'away': 0}, 'fulltime': {'home': 0, 'away': 0}}
        self.store(rows)
        self.assertEqual(self.build()['rows'][0]['counts'], {'1st': 1, 'Égal': 2, '2nd': 0})

    def test_season_and_competition_filters_share_exact_detail_universe(self):
        self.store([raw(1, '1st', season=2025), raw(2, '2nd', season=2026), raw(3, 'Égal', league=61)])
        report = self.build(seasons=[2026])
        self.assertEqual(report['totals']['matches'], 2)
        self.assertEqual({item['league'] for item in report['rows']}, {'39', '61'})
        self.assertEqual(self.build(seasons=[])['rows'], [])
        detail = trends.details('39', START, END, seasons=[2026], path=self.path, now=NOW)
        self.assertEqual(detail['total'], 1)
        self.assertEqual(detail['rows'][0]['value'], '2nd')
        catalogue = trends.catalogue(self.path, now=NOW)
        self.assertEqual(catalogue['seasons'], [2026, 2025])
        self.assertEqual(catalogue['first_day'], '2026-10-01')

    def test_equal_kickoff_order_uses_numeric_id_for_recent_window_and_pagination(self):
        self.store([raw(10, '1st', stamp='2026-10-01T12:00:00Z'),
                    raw(2, '2nd', stamp='2026-10-01T12:00:00Z')])
        item = self.build(recent=1)['rows'][0]
        self.assertEqual(item['recent_counts']['1st'], 1)
        page = trends.details('39', START, END, path=self.path, now=NOW, limit=1, offset=1)
        self.assertEqual((page['total'], len(page['rows']), page['rows'][0]['match_id']), (2, 1, '2'))

    def test_corrupt_projection_is_detected_again_without_changing_database(self):
        database = self.store([raw(1)])
        with sqlite3.connect(database) as connection:
            payload = json.loads(connection.execute('SELECT normalized_json FROM fixtures').fetchone()[0])
            payload['value'] = '1st'
            connection.execute('UPDATE fixtures SET normalized_json=?', (json.dumps(payload),))
        report = self.build()
        self.assertEqual(report['totals']['matches'], 0)
        self.assertEqual(report['totals']['excluded'], 1)
        self.assertTrue(any('Verdict conservé incohérent' in reason for reason in report['exclusion_reasons']))

    def test_missing_competition_is_reported_globally_not_as_selectable_pseudo_league(self):
        self.store([raw(1), raw(2, league=None)])
        report = self.build()
        self.assertEqual([row['league'] for row in report['rows']], ['39'])
        self.assertEqual(report['unassigned_matches'], 1)
        self.assertEqual(report['totals']['observed'], 2)
        self.assertEqual(report['totals']['excluded'], 1)

    def test_malformed_display_metadata_cannot_crash_trend_sorting(self):
        valid, invalid_name, invalid_team = raw(1), raw(2), raw(3)
        valid['league']['country'] = 123
        invalid_name['league']['name'] = 123
        invalid_team['teams']['home']['name'] = {'unexpected': 'object'}
        self.store([valid, invalid_name, invalid_team])
        report = self.build()
        self.assertEqual(report['totals']['matches'], 1)
        self.assertEqual(report['totals']['excluded'], 2)
        for row in report['rows']:
            self.assertIsInstance(row['country'], str)
            self.assertIsInstance(row['competition'], str)
        detail = trends.details('39', START, END, path=self.path, now=NOW)
        self.assertTrue(all(isinstance(row['home'], str) for row in detail['rows']))

    def test_reads_are_query_only_no_api_no_full_raw_loading_or_migration(self):
        database = self.store([raw(1), raw(2)])
        before = fixture_store.metadata(database)
        with patch.object(api, 'load_fixtures', side_effect=AssertionError('Full raw read')), \
             patch.object(api, 'load_matches', side_effect=AssertionError('Full normalized read')), \
             patch.object(api.ApiFootballClient, 'get', side_effect=AssertionError('API')), \
             patch.object(fixture_store, 'migrate_json', side_effect=AssertionError('Migration')):
            self.assertEqual(self.build()['totals']['matches'], 2)
            self.assertEqual(trends.catalogue(self.path, now=NOW)['observed_matches'], 2)
            self.assertEqual(trends.details('39', START, END, path=self.path, now=NOW)['total'], 2)
        self.assertEqual(before, fixture_store.metadata(database))

    def test_absent_or_json_only_store_requires_explicit_migration(self):
        self.path.write_text(json.dumps({'version': 1, 'fixtures': [raw(1)], 'season_imports': []}))
        with patch.object(Path, 'read_text', side_effect=AssertionError('Must not read JSON store')):
            with self.assertRaises(trends.TrendsError):
                self.build()
        self.assertFalse(fixture_store.database_path(self.path).exists())

    def test_invalid_controls_fail_explicitly(self):
        self.store([])
        for kwargs in ({'recent': 0}, {'recent': True}, {'seasons': ['2026']}, {'seasons': [True]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(trends.TrendsError):
                self.build(**kwargs)
        with self.assertRaises(trends.TrendsError):
            trends.build(END, START, path=self.path, now=NOW)
        with self.assertRaises(trends.TrendsError):
            trends.details('39 OR 1=1', START, END, path=self.path, now=NOW)
        with self.assertRaises(trends.TrendsError):
            trends.details('39', START, END, path=self.path, now=NOW, limit=0)


if __name__ == '__main__':
    unittest.main()
