"""The individual view must read frozen evidence without starting research."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import api_football as api
import individual_probability_validation as validation
import individual_probability_validation_panel as panel
from test_individual_probability_validation import capture, NOW


def script():
    from individual_probability_validation_panel import render
    render()


class IndividualPanelTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        panel._read.clear()
        self.addCleanup(panel._read.clear)
        self.values = [capture()]
        self.patch(api, 'DATA_DIR', self.root)
        self.patch(panel, '_now', return_value=NOW)
        self.read = self.patch(panel.archive, 'list_entries', side_effect=lambda **kw: deepcopy(self.values))
        self.network = self.patch(api.ApiFootballClient, 'get', side_effect=AssertionError('No API'))
        self.reconcile = self.patch(panel.archive, 'reconcile', side_effect=AssertionError('No result writes'))
        self.capture = self.patch(panel.archive, 'capture', side_effect=AssertionError('No forecast writes'))

    def patch(self, obj, name, *args, **kwargs):
        patcher = patch.object(obj, name, *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def readonly(self):
        self.network.assert_not_called()
        self.reconcile.assert_not_called()
        self.capture.assert_not_called()

    def open(self):
        at = AppTest.from_function(script, default_timeout=10).run()
        self.assertFalse(at.exception)
        return at

    def prepare(self):
        return validation.prepare(self.root, now=datetime(2026, 11, 1, 12, tzinfo=timezone.utc))

    def test_descriptive_view_compares_same_match_and_caches_reads(self):
        at = self.open()
        self.assertEqual(at.metric[1].value, '1')
        comparison = at.dataframe[0].value
        self.assertEqual(list(comparison['Matchs communs évalués']), [1, 1])
        self.assertEqual(list(comparison['Taux de réussite par match (%)']), [0, 100])
        self.assertTrue(any('Historique descriptif' in n.value for n in at.info))
        at.segmented_control(key='individual_validation_calibration').set_value('2nd').run()
        self.assertFalse(at.exception)
        self.read.assert_called_once()
        self.assertFalse(validation.protocol_path(self.root).exists())
        self.readonly()

    def test_future_protocol_cannot_admit_older_diagnostic_results(self):
        self.prepare()
        self.values = [capture(created='2026-10-28T10:00:00Z', kickoff='2026-10-28T15:00:00Z')]
        at = self.open()
        at.segmented_control(key='individual_validation_scope').set_value('prospective').run()
        self.assertFalse(at.exception)
        self.assertEqual(at.metric[0].value, '0')
        self.assertTrue(at.dataframe[0].value['Taux de réussite par match (%)'].isna().all())
        self.assertTrue(any('non retenu' in n.value for n in at.markdown))
        self.readonly()

    def test_before_start_waits_without_fabricated_rate(self):
        plan = self.prepare()
        self.patch(panel, '_now', return_value=validation._stamp(plan['created_at']))
        self.values = []
        at = self.open()
        at.segmented_control(key='individual_validation_scope').set_value('prospective').run()
        self.assertFalse(at.exception)
        self.assertTrue(any('suivi futur est préparé' in n.value for n in at.info))
        self.assertTrue(at.dataframe[0].value['Erreur probabiliste ↓'].isna().all())
        self.readonly()

    def test_no_protocol_read_does_not_create_one(self):
        at = self.open()
        at.segmented_control(key='individual_validation_scope').set_value('prospective').run()
        self.assertFalse(at.exception)
        self.assertTrue(any('Aucun protocole' in n.value for n in at.info))
        self.assertFalse(validation.protocol_path(self.root).exists())
        self.readonly()

    def test_missing_baseline_stays_visible_outside_paired_scores(self):
        self.values = [capture(baseline=None)]
        at = self.open()
        self.assertEqual(at.metric[0].value, '1')
        self.assertEqual(at.metric[1].value, '0')
        self.assertTrue(any('sans référence comparable' in n.value for n in at.warning))
        self.assertTrue(at.dataframe[0].value['Erreur probabiliste ↓'].isna().all())
        self.readonly()

    def test_read_error_has_no_fabricated_metrics(self):
        self.read.side_effect = ValueError('unreadable')
        at = self.open()
        self.assertTrue(at.error)
        self.assertFalse(at.metric)
        self.assertFalse(at.dataframe)
        self.readonly()

    def test_export_contains_protocol_and_both_paired_methods(self):
        self.prepare()
        data = panel._read(str(self.root), (), (), NOW.isoformat())
        exported = json.loads(panel._export(data, 'prospective'))
        self.assertEqual(exported['report']['scope'], 'prospective')
        self.assertEqual(set(exported['report']['metrics']), {'frequency', 'clubs'})
        self.assertEqual(exported['protocol']['enriched']['status'], 'rejected_on_historical_validation')
        self.readonly()

    def test_new_mode_routes_without_loading_group_history(self):
        import daily_evaluation_panel as daily
        with patch.object(panel, 'render') as render, patch.object(daily, 'render_pending_overview') as pending:
            def daily_script():
                import streamlit as st
                import daily_evaluation_panel
                st.session_state['daily_audit_view'] = 'individual'
                daily_evaluation_panel.render_evaluation()
            at = AppTest.from_function(daily_script, default_timeout=15).run()
            self.assertFalse(at.exception)
            render.assert_called_once()
            pending.assert_not_called()


if __name__ == '__main__':
    unittest.main()
