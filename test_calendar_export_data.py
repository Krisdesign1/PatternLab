"""Multi-day calendar export is date-exact, read-only and honest about gaps."""
from datetime import date, datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import calendar_export_data as export
import fixture_store
from test_competition_trends import raw


NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
START, END = date(2026, 11, 6), date(2026, 11, 7)


class CalendarExportDataTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'fixtures.json'

    def store(self, rows):
        self.path.write_text(json.dumps({'version': 1, 'fixtures': rows, 'season_imports': []}))
        fixture_store.migrate_json(self.path)
        return fixture_store.database_path(self.path)

    def build(self, **kwargs):
        return export.build(START, END, path=self.path, now=NOW, **kwargs)

    def test_inclusive_local_dates_have_precise_utc_times_and_all_fields(self):
        self.store([raw(1, '1st', stamp='2026-11-05T23:00:00Z'),
                    raw(2, 'Égal', stamp='2026-11-06T22:59:59Z'),
                    raw(3, '2nd', stamp='2026-11-06T23:00:00Z')])
        report = self.build()
        self.assertEqual(report['metadata']['day_count'], 2)
        self.assertEqual(report['columns'], list(export.COLUMNS))
        self.assertEqual(report['day_columns'], list(export.DAY_COLUMNS))
        self.assertEqual([row['Date'] for row in report['rows']], ['2026-11-06', '2026-11-06', '2026-11-07'])
        self.assertEqual([row['Verdict final'] for row in report['rows']], ['1st', 'Égal', '2nd'])
        self.assertEqual(report['rows'][0]['Heure (Kinshasa)'], '00:00:00')
        self.assertEqual(report['rows'][0]['Coup d’envoi UTC'], '2026-11-05T23:00:00+00:00')
        self.assertTrue(all(set(row) == set(export.COLUMNS) for row in report['rows']))
        self.assertTrue(all(row['Fuseau horaire'] == 'Africa/Kinshasa' for row in report['rows']))
        self.assertTrue(all(row['Extraction UTC'] == NOW.isoformat() for row in report['rows']))

    def test_lower_and_upper_local_day_bounds_are_strict(self):
        self.store([raw(1, stamp='2026-11-05T22:59:59Z'),
                    raw(2, stamp='2026-11-05T23:00:00Z'),
                    raw(3, stamp='2026-11-07T22:59:59Z'),
                    raw(4, stamp='2026-11-07T23:00:00Z')])
        self.assertEqual([row['ID du match'] for row in self.build()['rows']], ['2', '3'])
        single = export.build(START, START, path=self.path, now=NOW)
        self.assertEqual([row['ID du match'] for row in single['rows']], ['2'])
        self.assertEqual(single['metadata']['day_count'], 1)

    def test_calendar_statuses_and_live_scores_are_preserved_without_final_guess(self):
        rows = [raw(i, stamp=f'2026-11-06T{i:02d}:00:00Z', status=status)
                for i, status in enumerate(('FT', 'NS', '2H', 'PST', 'CANC', 'AWD', 'WO'), 1)]
        rows[2]['fixture']['status'].update(elapsed=90, extra=4)
        self.store(rows)
        report = self.build()
        self.assertEqual(len(report['rows']), 7)
        by_status = {row['Code statut API']: row for row in report['rows']}
        self.assertEqual(by_status['NS']['Verdict final'], 'En attente')
        self.assertEqual(by_status['2H']['Verdict final'], 'En cours')
        self.assertEqual(by_status['2H']['Minute'], '90+4′')
        self.assertEqual(by_status['2H']['À 90 minutes'], '—')
        self.assertEqual(by_status['2H']['Score actuel'], '1–1')
        self.assertTrue(all(by_status[status]['Verdict final'] == 'Non évaluable'
                            for status in ('PST', 'CANC', 'AWD', 'WO')))

    def test_overtime_and_penalties_use_only_regulation_scores(self):
        rows = [raw(1, '1st', stamp='2026-11-06T12:00:00Z', status='AET'),
                raw(2, 'Égal', stamp='2026-11-06T13:00:00Z', status='PEN')]
        for row in rows:
            row['goals'] = {'home': 8, 'away': 4}
            row['score'].update(extratime={'home': 8, 'away': 4}, penalty={'home': 8, 'away': 7})
        self.store(rows)
        exported = self.build()['rows']
        self.assertEqual([row['Verdict final'] for row in exported], ['1st', 'Égal'])
        self.assertTrue(all(row['Score actuel'] == '8–4' for row in exported))
        self.assertEqual(exported[0]['À 90 minutes'], '2–1')

    def test_missing_scores_quarantine_and_foreign_provider_keep_review_states(self):
        rows = [raw(i, stamp=f'2026-11-06T{i:02d}:00:00Z') for i in range(1, 5)]
        rows[0]['score']['halftime']['home'] = None
        rows[1]['_patternlab_import'] = {'quarantined': True, 'reason': 'identity_changed'}
        rows[2]['provider'] = 'another-provider'
        rows[3]['score']['halftime'] = {'home': 3, 'away': 0}
        self.store(rows)
        exported = self.build()['rows']
        self.assertEqual(exported[0]['Mi-temps'], '—')
        self.assertEqual(exported[0]['Verdict final'], 'Non évaluable')
        self.assertTrue(all(row['Verdict final'] == 'À vérifier' for row in exported[1:]))
        self.assertIn('Quarantaine', exported[1]['Motif du verdict'])
        self.assertIn('Source non confirmée', exported[2]['Source'])

    def test_future_fixtures_exported_but_premature_final_never_sporting_verdict(self):
        self.store([raw(1, stamp='2026-11-07T15:00:00Z'),
                    raw(2, stamp='2026-11-07T16:00:00Z', status='NS')])
        rows = self.build()['rows']
        self.assertEqual(rows[0]['Verdict final'], 'À vérifier')
        self.assertIn('avant le coup', rows[0]['Motif du verdict'])
        self.assertEqual(rows[1]['Verdict final'], 'En attente')

    def test_filters_use_exact_ids_and_do_not_change_inventory_coverage(self):
        self.store([raw(1, stamp='2026-11-06T12:00:00Z', league=39),
                    raw(2, stamp='2026-11-06T13:00:00Z', league=390),
                    raw(3, stamp='2026-11-07T10:00:00Z', league=39, status='NS')])
        report = self.build(leagues=['39'], status='Terminés', query='home')
        self.assertEqual([row['ID du match'] for row in report['rows']], ['1'])
        self.assertEqual(report['metadata']['stored_matches'], 3)
        self.assertEqual(report['metadata']['exported_matches'], 1)
        self.assertEqual([row['Matchs stockés'] for row in report['days']], [2, 1])
        self.assertEqual([row['Matchs exportés'] for row in report['days']], [1, 0])
        self.assertEqual(len(report['competitions']), 2)
        self.assertEqual(self.build(leagues=[])['rows'], [])

    def test_coverage_distinguishes_receipts_empty_days_and_unconfirmed_history(self):
        database = self.store([raw(1, stamp='2026-11-06T12:00:00Z')])
        fixture_store.mark_calendar_refresh(database, '2026-11-07', 'Africa/Kinshasa', '2026-11-07T10:00:00Z')
        report = export.build(date(2026, 11, 5), END, path=self.path, now=NOW)
        self.assertEqual(report['metadata']['missing_days'], ['2026-11-05'])
        self.assertEqual(report['metadata']['unconfirmed_days'], ['2026-11-06'])
        self.assertEqual(report['days'][0]['État du relevé'], 'Aucune donnée locale ni relevé calendrier')
        self.assertEqual(report['days'][1]['État du relevé'], 'Rencontres stockées sans relevé calendrier daté')
        self.assertEqual(report['days'][2]['État du relevé'], 'Relevé enregistré sans match stocké')
        self.assertEqual(report['days'][2]['Dernier relevé UTC'], '2026-11-07T10:00:00+00:00')

    def test_invalid_local_records_and_undated_global_records_are_reported(self):
        rows = [raw(1, stamp='2026-11-06T12:00:00Z'), raw(2, stamp='not-a-date'),
                raw(3, stamp='2026-11-06T13:00:00Z', league=None)]
        self.store(rows)
        report = self.build()
        self.assertEqual(len(report['rows']), 1)
        self.assertEqual(report['metadata']['unassignable_records'], 1)
        self.assertEqual(len(report['metadata']['invalid_records']), 1)
        self.assertEqual(report['metadata']['stored_matches'], 2)

    def test_malformed_text_metadata_is_safe_in_export_and_competition_controls(self):
        item = raw(1, stamp='2026-11-06T12:00:00Z')
        item['league'].update(name=123, country={'name': 'bad'})
        self.store([item])
        report = self.build()
        self.assertIsInstance(report['rows'][0]['Compétition'], str)
        self.assertIsInstance(report['rows'][0]['Pays'], str)
        choices = export.competitions(START, END, path=self.path)
        self.assertEqual(choices[0]['league'], '39')
        self.assertEqual(choices[0]['count'], 1)
        self.assertIsInstance(choices[0]['competition'], str)

    def test_read_only_requests_do_not_load_full_store_call_api_or_migrate(self):
        database = self.store([raw(1, stamp='2026-11-06T12:00:00Z')])
        before = fixture_store.metadata(database)
        with patch.object(api, 'load_fixtures', side_effect=AssertionError('No full raw load')), \
             patch.object(api, 'load_matches', side_effect=AssertionError('No full history load')), \
             patch.object(api.ApiFootballClient, 'get', side_effect=AssertionError('No API')), \
             patch.object(fixture_store, 'migrate_json', side_effect=AssertionError('No migration')):
            self.assertEqual(len(self.build()['rows']), 1)
            self.assertEqual(len(export.competitions(START, END, path=self.path)), 1)
        self.assertEqual(before, fixture_store.metadata(database))

    def test_invalid_controls_and_missing_active_database_are_explicit(self):
        self.store([])
        for options in ({'leagues': ['39 OR 1=1']}, {'status': 'bad'}, {'query': None}):
            with self.subTest(options=options), self.assertRaises(export.ExportDataError):
                self.build(**options)
        for start, end in ((END, START), ('2026-11-06', END), (datetime.now(), END)):
            with self.assertRaises(export.ExportDataError):
                export.build(start, end, path=self.path, now=NOW)
        with self.assertRaises(export.ExportDataError):
            export.build(START, END, path=self.path, now=NOW.replace(tzinfo=None))
        with self.assertRaises(export.ExportDataError):
            export.build(START, END, path=self.path.parent / 'absent.json', now=NOW)


if __name__ == '__main__':
    unittest.main()
