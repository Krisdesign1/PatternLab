"""The reliability panel must never refresh, train or write the journal."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import api_football as api
import prediction_journal as journal
import probability_reliability_panel as panel
from test_probability_reliability import entry


def script():
    from probability_reliability_panel import render_probability_reliability
    render_probability_reliability()


class ProbabilityReliabilityPanelTests(unittest.TestCase):
    def setUp(self):
        panel._load.clear()
        self.addCleanup(panel._load.clear)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'predictions.sqlite3'
        self.entries = [entry()]
        self.patch(journal, 'DB_PATH', self.path)
        self.read = self.patch(journal, 'list_entries', side_effect=lambda path: deepcopy(self.entries))
        self.api = self.patch(api.ApiFootballClient, 'get', side_effect=AssertionError('No API'))
        self.reconcile = self.patch(journal, 'reconcile', side_effect=AssertionError('No reconcile'))
        self.record = self.patch(journal, 'record', side_effect=AssertionError('No writes'))
        self.record_mixed = self.patch(journal, 'record_mixed', side_effect=AssertionError('No writes'))

    def patch(self, target, name, *args, **kwargs):
        patcher = patch.object(target, name, *args, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def open(self):
        at = AppTest.from_function(script, default_timeout=10)
        at.session_state['hp_reliability_open'] = True
        at.run()
        self.assertFalse(at.exception)
        return at

    def readonly(self):
        for mock in (self.api, self.reconcile, self.record, self.record_mixed):
            mock.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_collapsed_section_does_not_read_storage(self):
        at = AppTest.from_function(script).run()
        self.assertFalse(at.exception)
        self.read.assert_not_called()
        self.readonly()

    def test_open_report_preserves_all_three_categories_and_percentage_units(self):
        at = self.open()
        self.assertEqual(at.metric[0].value, '2')
        self.assertEqual(len(at.dataframe[0].value), 10)
        at.segmented_control(key='hp_reliability_outcome').set_value('2nd').run()
        self.assertFalse(at.exception)
        row = at.dataframe[0].value.iloc[5]
        self.assertEqual(row['Moyenne annoncée (%)'], '50.00')
        self.assertEqual(row['Taux observé (%)'], '100.00')
        self.assertTrue(any('tous les pourcentages' in node.value for node in at.caption))
        self.assertEqual(self.read.call_count, 1)
        self.readonly()

    def test_pending_results_have_missing_rates_instead_of_zero_success(self):
        self.entries[0].update(status='pending', observed=[])
        at = self.open()
        self.assertEqual(at.metric[0].value, '0')
        self.assertEqual(at.metric[1].value, '2')
        self.assertTrue(at.dataframe[0].value['Taux observé (%)'].eq('—').all())
        self.assertTrue(any('pas comme des échecs' in node.value for node in at.info))
        self.readonly()

    def test_versions_switch_without_pooling(self):
        other = entry(identity='other', version='half-goals-v2', status='pending')
        other['observed'] = []
        self.entries.append(other)
        at = self.open()
        at.selectbox(key='hp_reliability_version').select('half-goals-v2').run()
        self.assertFalse(at.exception)
        self.assertEqual(at.metric[0].value, '0')
        self.assertEqual(at.metric[1].value, '2')
        self.readonly()

    def test_absent_storage_empty_report_has_no_fake_rates(self):
        self.entries.clear()
        at = self.open()
        self.assertTrue(any('Aucune probabilité Clubs' in node.value for node in at.info))
        self.assertEqual(len(at.dataframe), 0)
        self.readonly()

    def test_read_failure_is_visible_without_recalculating(self):
        self.read.side_effect = journal.JournalError('broken')
        at = self.open()
        self.assertTrue(any('Aucun taux' in node.value for node in at.warning))
        self.readonly()

    def test_cache_signature_includes_wal_and_invalidates_on_result_changes(self):
        before = panel._storage_signature(self.path)
        Path(str(self.path) + '-wal').write_bytes(b'new result')
        after = panel._storage_signature(self.path)
        self.assertNotEqual(before, after)
        self.assertEqual(before[0], after[0])


if __name__ == '__main__':
    unittest.main()
