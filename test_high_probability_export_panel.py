"""Multi-day exports are independent of history pagination and display filters."""
from copy import deepcopy
from datetime import date
import csv
from io import StringIO
import json
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import api_football as api
import high_probability_archive as archive
import high_probability_archive_panel as panel
import high_probability_export_panel as exports
import high_probability_panel as ranking
import high_probability_workspace as workspace
from test_high_probability_archive_view import archived
from test_high_probability_history import LATER


def second_day():
    row = archived(0, status='settled', created='2026-10-23T10:00:00Z', identifier='next-day')
    row['fixture'].update(timestamp='2026-10-23T15:00:00Z', match_id='200')
    row['day'] = '2026-10-23'
    row['before'] = '2026-10-23'
    return row


class MultiDayExportPanelTests(unittest.TestCase):
    def setUp(self):
        self.records = [archived(0, status='settled'), archived(1), second_day()]
        self.patch(ranking, '_now', return_value=LATER)
        self.patch(panel, '_load', side_effect=lambda *args: deepcopy(self.records))
        self.patch(panel, '_load_groups', return_value=([], []))
        self.patch(panel, 'annotate_entries', side_effect=lambda rows, **kwargs: rows)
        self.scan = self.patch(workspace, 'load_scan', side_effect=AssertionError('No scan'))
        self.capture = self.patch(archive, 'capture', side_effect=AssertionError('No new forecasts'))
        self.reconcile = self.patch(archive, 'reconcile', side_effect=AssertionError('No result writes'))
        self.network = self.patch(api.ApiFootballClient, 'get', side_effect=AssertionError('No API'))
        self.local = self.patch(api, 'load_fixtures', side_effect=AssertionError('No fixture read'))

    def patch(self, target, name, **kwargs):
        patcher = patch.object(target, name, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def open(self, day=date(2026, 10, 22)):
        at = AppTest.from_string('from high_probability_panel import render_high_probabilities\nrender_high_probabilities()',
                                default_timeout=20)
        at.session_state['hp_view'] = 'history'
        at.session_state['analysis_selected_day'] = day
        at.session_state['analysis_follow_today'] = False
        at.run()
        self.assertFalse(at.exception)
        return at

    def prepare(self, at):
        at.button(key='hp_archive_export_prepare').click().run()
        self.assertFalse(at.exception)
        self.readonly()
        return at.session_state[exports.PREPARED]

    def readonly(self):
        for guard in (self.scan, self.capture, self.reconcile, self.network, self.local):
            guard.assert_not_called()

    def test_one_day_all_categories_and_reference_odds_are_in_both_files(self):
        before = deepcopy(self.records)
        at = self.open()
        self.assertEqual(at.multiselect(key='hp_archive_export_days').value, ['2026-10-22'])
        self.assertNotIn(exports.PREPARED, at.session_state)
        prepared = self.prepare(at)
        rows = prepared['rows']
        self.assertEqual(len(rows), 6)
        self.assertEqual({r['Catégorie'] for r in rows}, {'1st', 'Égal', '2nd'})
        self.assertEqual({r['Catégorie']:r['Cote de référence'] for r in rows}, {'1st':3.1,'Égal':3.0,'2nd':2.1})
        parsed = list(csv.DictReader(StringIO(prepared['csv'].decode('utf-8-sig')), delimiter=';'))
        payload = json.loads(prepared['json'])
        self.assertEqual(len(parsed), 6)
        self.assertEqual(payload['rows'], rows)
        self.assertEqual(payload['odds_reference']['kind'], 'manual_fixed')
        self.assertEqual({r['Cote de référence'] for r in parsed}, {'3,10','3,00','2,10'})
        self.assertFalse(at.expander)
        self.assertEqual(len(at.dataframe), 1)
        self.assertEqual(self.records, before)

    def test_two_days_single_outcome_then_all_outcomes_preserve_the_history_day(self):
        at = self.open()
        at.multiselect(key='hp_archive_export_days').set_value(['2026-10-22','2026-10-23']).run()
        for outcome in ('1st', 'Égal', '2nd'):
            at.segmented_control(key='hp_archive_export_outcomes').set_value(outcome).run()
            prepared = self.prepare(at)
            self.assertEqual(len(prepared['rows']), 3)
            self.assertEqual({row['Catégorie'] for row in prepared['rows']}, {outcome})
            self.assertEqual({row['Journée'] for row in prepared['rows']}, {'2026-10-22','2026-10-23'})
        at.segmented_control(key='hp_archive_export_outcomes').set_value('all').run()
        self.assertEqual(len(self.prepare(at)['rows']), 9)
        self.assertEqual(at.selectbox(key='hp_archive_day').value, '2026-10-22')

    def test_export_all_pages_is_not_limited_by_history_search_or_category(self):
        self.records = []
        for index in range(28):
            row = archived(0, identifier=f'many-{index}')
            row['fixture'].update(match_id=str(1000+index), home=f'Home {index}')
            self.records.append(row)
        at = self.open()
        at.selectbox(key='hp_archive_page_size').set_value(10).run()
        at.selectbox(key='hp_archive_page').set_value(1).run()
        prepared = self.prepare(at)
        self.assertEqual(len(prepared['rows']), 84)
        self.assertEqual(at.selectbox(key='hp_archive_page').value, 1)
        at.text_input(key='hp_archive_search').set_value('Home 10').run()
        self.assertEqual(len(self.prepare(at)['rows']), 84)
        self.readonly()

    def test_empty_display_filters_do_not_hide_other_days_export(self):
        at = self.open()
        at.number_input(key='hp_archive_minimum').set_value(100).run()
        self.assertFalse(at.dataframe)
        at.multiselect(key='hp_archive_export_days').set_value(['2026-10-23']).run()
        prepared = self.prepare(at)
        self.assertEqual(len(prepared['rows']), 3)
        self.assertEqual({row['Journée'] for row in prepared['rows']}, {'2026-10-23'})
        self.assertTrue(any('Aucun match ne correspond' in item.value for item in at.info))

    def test_missing_shared_day_does_not_silently_export_an_unrequested_day(self):
        at = self.open(date(2026, 10, 26))
        self.assertEqual(at.multiselect(key='hp_archive_export_days').value, [])
        self.assertTrue(at.button(key='hp_archive_export_prepare').disabled)
        at.multiselect(key='hp_archive_export_days').set_value(['2026-10-22']).run()
        self.assertEqual(len(self.prepare(at)['rows']), 6)
        self.assertEqual(at.selectbox(key='hp_archive_day').value, '2026-10-26')

    def test_select_all_days_and_clear_are_explicit_and_clear_stale_downloads(self):
        at = self.open()
        self.prepare(at)
        at.button(key='hp_archive_export_all_days').click().run()
        self.assertEqual(set(at.multiselect(key='hp_archive_export_days').value), {'2026-10-22','2026-10-23'})
        self.assertNotIn(exports.PREPARED, at.session_state)
        self.assertEqual(len(self.prepare(at)['rows']), 9)
        at.button(key='hp_archive_export_clear_days').click().run()
        self.assertEqual(at.multiselect(key='hp_archive_export_days').value, [])
        self.assertTrue(at.button(key='hp_archive_export_prepare').disabled)
        self.assertNotIn(exports.PREPARED, at.session_state)
        self.assertFalse(any(node.key in ('hp_archive_export_csv','hp_archive_export_json')
                             for node in at.get('download_button')))
        self.readonly()

    def test_exact_export_threshold_and_selection_changes_invalidate_prepared_files(self):
        at = self.open()
        self.prepare(at)
        at.number_input(key='hp_archive_export_minimum').set_value(50).run()
        self.assertNotIn(exports.PREPARED, at.session_state)
        rows = self.prepare(at)['rows']
        self.assertEqual(len(rows), 2)
        self.assertEqual({row['Catégorie'] for row in rows}, {'1st','2nd'})
        self.assertTrue(all(row['Probabilité (%)'] >= 50 for row in rows))
        at.number_input(key='hp_archive_export_minimum').set_value(100).run()
        self.assertEqual(self.prepare(at)['rows'], [])
        buttons = [node for node in at.get('download_button') if node.key in
                   ('hp_archive_export_csv', 'hp_archive_export_json')]
        self.assertTrue(all(node.proto.disabled for node in buttons))

    def test_new_result_revision_invalidates_old_export_instead_of_caching_a_verdict(self):
        at = self.open()
        rows = self.prepare(at)['rows']
        self.assertTrue(any(row['Verdict'] == 'Non évalué' for row in rows))
        self.records[1].update(status='settled', actual='2nd', ht='0-0', ft='1-0',
                               checked_at='2026-10-25T09:59:00Z')
        at.run()
        self.assertNotIn(exports.PREPARED, at.session_state)
        prepared = self.prepare(at)
        won = [row for row in prepared['rows'] if row['ID du match'] == '101' and row['Catégorie'] == '2nd']
        self.assertEqual(won[0]['Verdict'], 'Réalisé')


if __name__ == '__main__':
    unittest.main()
