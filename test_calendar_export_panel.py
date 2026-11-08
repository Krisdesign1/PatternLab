"""Export choices, exact inclusive dates and downloads are explicitly user controlled."""
import csv
from datetime import date
from io import BytesIO, StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from streamlit.testing.v1 import AppTest

import api_football as api
import calendar_export_data as data
import calendar_export_formats as formats
import calendar_export_panel as panel
import fixture_store
from test_competition_trends import raw


class CalendarExportPanelTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'fixtures.json'
        rows = [raw(1, '1st', stamp='2026-11-05T23:00:00Z'),
                raw(2, 'Égal', stamp='2026-11-06T14:15:00Z', league=61),
                raw(3, '2nd', stamp='2026-11-06T23:00:00Z'),
                raw(4, '2nd', stamp='2026-11-07T16:00:00Z', status='NS')]
        self.path.write_text(json.dumps({'version': 1, 'fixtures': rows, 'season_imports': []}))
        fixture_store.migrate_json(self.path)
        self.patch(api, 'STORE_PATH', self.path)
        self.build = self.patch(data, 'build', wraps=data.build)
        self.xlsx = self.patch(formats, 'to_xlsx', wraps=formats.to_xlsx)
        self.csv = self.patch(formats, 'to_csv', wraps=formats.to_csv)
        self.network = self.patch(api.ApiFootballClient, 'get', side_effect=AssertionError('No API'))
        self.patch(api, 'load_fixtures', side_effect=AssertionError('No full raw read'))
        self.patch(fixture_store, 'migrate_json', side_effect=AssertionError('No migration'))
        panel._competitions.clear(); self.addCleanup(panel._competitions.clear)
        self.at = AppTest.from_string("from datetime import date\nfrom calendar_export_panel import render_export\n"
                                     "render_export(date(2026,11,7))", default_timeout=15).run()
        self.assertFalse(self.at.exception)

    def patch(self, target, name, *args, **kwargs):
        patcher = patch.object(target, name, *args, **kwargs)
        value = patcher.start(); self.addCleanup(patcher.stop)
        return value

    def prepare(self):
        self.at.button(key='calendar_export_prepare').click().run()
        self.assertFalse(self.at.exception)
        self.network.assert_not_called()
        return self.at.session_state[panel.PREPARED]

    def test_initial_export_controls_do_not_generate_files_or_run_collection(self):
        self.assertNotIn(panel.PREPARED, self.at.session_state)
        self.assertEqual(self.at.date_input(key='calendar_export_start').value, date(2026, 11, 7))
        self.assertEqual(self.at.date_input(key='calendar_export_end').value, date(2026, 11, 7))
        self.assertEqual(self.at.multiselect(key='calendar_export_leagues').value, [])
        self.build.assert_not_called(); self.xlsx.assert_not_called(); self.csv.assert_not_called()

    def test_excel_period_contains_both_dates_scores_and_coverage(self):
        self.at.date_input(key='calendar_export_start').set_value(date(2026, 11, 6)).run()
        prepared = self.prepare()
        self.assertEqual(prepared['metadata']['day_count'], 2)
        self.assertEqual(prepared['count'], 4)
        self.assertEqual({row['Date'] for row in prepared['preview']}, {'2026-11-06', '2026-11-07'})
        self.assertEqual(prepared['preview'][0]['Heure (Kinshasa)'], '00:00:00')
        with ZipFile(BytesIO(prepared['content'])) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn('Verdict final', archive.read('xl/worksheets/sheet1.xml').decode())
            self.assertIn('2026-11-06', archive.read('xl/worksheets/sheet2.xml').decode())
        self.assertEqual(prepared['filename'], 'calendrier_2026-11-06_au_2026-11-07_2jours')
        self.assertNotIn('rows', prepared)
        self.csv.assert_not_called()

    def test_number_of_days_is_inclusive_and_missing_dates_remain_visible(self):
        self.at.segmented_control(key='calendar_export_period_mode').set_value('days').run()
        self.at.number_input(key='calendar_export_day_count').set_value(3).run()
        prepared = self.prepare()
        self.assertEqual(prepared['metadata']['start'], '2026-11-05')
        self.assertEqual(prepared['metadata']['end'], '2026-11-07')
        self.assertEqual(prepared['metadata']['day_count'], 3)
        self.assertEqual(len(prepared['days']), 3)
        self.assertEqual(prepared['metadata']['missing_days'], ['2026-11-05'])
        self.assertTrue(any('sans donnée locale' in warning.value for warning in self.at.warning))

    def test_csv_filters_and_coverage_file_are_independent_of_current_calendar(self):
        self.at.date_input(key='calendar_export_start').set_value(date(2026, 11, 6)).run()
        self.at.selectbox(key='calendar_export_format').set_value('csv').run()
        self.at.multiselect(key='calendar_export_leagues').set_value(['61']).run()
        self.at.selectbox(key='calendar_export_status').set_value('Terminés').run()
        prepared = self.prepare()
        rows = list(csv.DictReader(StringIO(prepared['content'].decode('utf-8-sig')), delimiter=';'))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['Date'], '2026-11-06')
        self.assertEqual(rows[0]['Verdict final'], 'Égal')
        coverage = list(csv.DictReader(StringIO(prepared['coverage_csv'].decode('utf-8-sig')), delimiter=';'))
        self.assertEqual([row['Matchs stockés'] for row in coverage], ['2', '2'])
        self.assertEqual([row['Matchs exportés'] for row in coverage], ['1', '0'])
        self.xlsx.assert_not_called()

    def test_changing_options_invalidates_prepared_download_without_auto_generation(self):
        self.prepare()
        self.assertEqual(self.build.call_count, 1)
        self.at.text_input(key='calendar_export_query').set_value('Home 4').run()
        self.assertFalse(self.at.exception)
        self.assertNotIn(panel.PREPARED, self.at.session_state)
        self.assertEqual(self.build.call_count, 1)
        prepared = self.prepare()
        self.assertEqual(prepared['count'], 1)
        self.assertEqual(prepared['preview'][0]['Domicile'], 'Home 4')

    def test_invalid_range_or_read_failure_produces_explicit_message(self):
        self.at.date_input(key='calendar_export_start').set_value(date(2026, 11, 8)).run()
        self.assertFalse(self.at.exception)
        self.assertTrue(any('première journée' in warning.value for warning in self.at.warning))
        self.build.assert_not_called()
        self.at.date_input(key='calendar_export_start').set_value(date(2026, 11, 7)).run()
        with patch.object(data, 'build', side_effect=data.ExportDataError('Base indisponible')):
            self.at.button(key='calendar_export_prepare').click().run()
        self.assertFalse(self.at.exception)
        self.assertTrue(any('Base indisponible' in error.value for error in self.at.error))
        self.assertNotIn(panel.PREPARED, self.at.session_state)

    def test_period_and_competitions_survive_hiding_export_controls(self):
        self.at = AppTest.from_string("from datetime import date\nimport streamlit as st\n"
            "from calendar_export_panel import render_export\n"
            "if st.toggle('Afficher',value=True,key='show_export'):\n"
            "    render_export(date(2026,11,7), selected_league='39')", default_timeout=15).run()
        self.at.date_input(key='calendar_export_start').set_value(date(2026, 11, 5)).run()
        self.at.selectbox(key='calendar_export_format').set_value('csv').run()
        self.at.toggle(key='show_export').set_value(False).run()
        self.at.toggle(key='show_export').set_value(True).run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.at.date_input(key='calendar_export_start').value, date(2026, 11, 5))
        self.assertEqual(self.at.multiselect(key='calendar_export_leagues').value, ['39'])
        self.assertEqual(self.at.selectbox(key='calendar_export_format').value, 'csv')


if __name__ == '__main__':
    unittest.main()
