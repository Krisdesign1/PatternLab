"""Browsing caches only projections; opening details re-verifies one record."""
from datetime import date, timedelta
import copy
import json
import unittest
from unittest.mock import patch

import api_football as api
import journal_page_data as data
import journal_panel as panel
import prediction_journal as journal
import test_journal_panel as helpers
from test_prediction_journal import BEFORE, AFTER, raw_fixtures


class JournalPageDataTests(unittest.TestCase):
    patch = helpers.JournalPanelTests.patch
    open = helpers.JournalPanelTests.open
    buttons = helpers.JournalPanelTests.buttons
    view = helpers.JournalPanelTests.view
    seed = helpers.JournalPanelTests.seed

    def setUp(self):
        helpers.JournalPanelTests.setUp(self)
        self.patch(panel.admin_access, 'can_admin', return_value=False)
        data.day_rows.clear()
        data.inventory.clear()
        self.addCleanup(data.day_rows.clear)
        self.addCleanup(data.inventory.clear)

    def test_filter_reruns_reuse_only_day_projection_and_detail_reads_one_id(self):
        saved = self.seed(size=3)
        self.seed(offset=100, day=date(2025, 3, 19), created_at=BEFORE - timedelta(days=2))
        before = journal.DB_PATH.read_bytes()
        read = self.patch(journal, 'list_entries', wraps=journal.list_entries)
        get = self.patch(journal, 'get_entry', wraps=journal.get_entry)
        at = self.open()
        read.assert_called_once_with(day=date(2025, 3, 20), include_mixed=False)
        at.text_input(key='journal_browser_query').set_value(saved['fixtures'][0]['home']).run()
        at.selectbox(key='journal_browser_size').set_value(3).run()
        self.assertFalse(at.exception)
        self.assertEqual(read.call_count, 1)
        at = self.view(at, saved['id'])
        at.run()
        self.assertFalse(at.exception)
        self.assertEqual(read.call_count, 1)
        self.assertGreaterEqual(get.call_count, 2)
        self.assertEqual({call.args[0] for call in get.call_args_list}, {saved['id']})
        self.assertEqual(journal.DB_PATH.read_bytes(), before)
        self.network.assert_not_called()

    def test_new_forecast_and_result_revisions_invalidate_display_cache(self):
        saved = self.seed(finished=False)
        now = AFTER
        first = data.load_rows(date(2025, 3, 20), now=now)
        self.assertEqual(first['rows'][0]['journal_status'], 'pending')
        self.seed(offset=100)
        second = data.load_rows(date(2025, 3, 20), now=now)
        self.assertEqual(len(second['rows']), 2)
        journal.reconcile(raw_fixtures(saved['fixtures'], status='FT'), now=AFTER)
        third = data.load_rows(date(2025, 3, 20), now=now)
        selected = next(row for row in third['rows'] if row['id'] == saved['id'])
        self.assertEqual(selected['journal_status'], 'settled')
        self.assertEqual(selected['observed_matches'], saved['size'])

    def test_projection_copies_are_independent_and_never_hold_distributions(self):
        self.seed()
        now = panel._now()
        value = data.load_rows(date(2025, 3, 20), now=now)
        original = copy.deepcopy(value)
        self.assertNotIn('methods', value['rows'][0])
        self.assertNotIn('result_history', value['rows'][0])
        self.assertNotIn('distribution', json.dumps(value))
        value['rows'][0]['primary_sequence'][0] = 'corrupted'
        self.assertEqual(data.load_rows(date(2025, 3, 20), now=now), original)

    def test_time_and_fixture_revision_both_refresh_projection(self):
        saved = self.seed(finished=False)
        read = self.patch(journal, 'list_entries', wraps=journal.list_entries)
        now = panel._now()
        data.load_rows(date(2025, 3, 20), now=now)
        data.load_rows(date(2025, 3, 20), now=now + timedelta(seconds=10))
        self.assertEqual(read.call_count, 1)
        data.load_rows(date(2025, 3, 20), now=now + timedelta(minutes=1))
        self.assertEqual(read.call_count, 2)
        self.raw[0]['fixture']['status']['short'] = 'PST'
        result = data.load_rows(date(2025, 3, 20), now=now + timedelta(minutes=1))
        self.assertEqual(read.call_count, 3)
        self.assertIn('Reporté', json.dumps(result, ensure_ascii=False))

    def test_full_export_is_lazy_and_still_contains_other_dates_and_mixed_groups(self):
        self.seed()
        self.seed(offset=100, day=date(2025, 3, 19), created_at=BEFORE - timedelta(days=2))
        read = self.patch(journal, 'list_entries', wraps=journal.list_entries)
        with patch.object(panel.st, 'download_button') as download:
            panel._export()
            read.assert_not_called()
            content = json.loads(download.call_args.args[1]())
        self.assertEqual(len(content), 2)
        read.assert_called_once_with()

    def test_invalid_time_or_day_does_not_enter_cache(self):
        with self.assertRaises(ValueError):
            data.load_rows('2025-03-20', now=BEFORE)
        with self.assertRaises(ValueError):
            data.load_rows(date(2025, 3, 20), now=BEFORE.replace(tzinfo=None))


if __name__ == '__main__':
    unittest.main()
