"""Workflow navigation keeps old routes and never runs inactive panels."""
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import navigation_local as nav


class WorkflowNavigationTests(unittest.TestCase):
    def setUp(self):
        # Render only markers: this verifies routing, without any data reads,
        # model work or API calls from the application's business panels.
        modules = (
            ('scenario_workspace_panel.render_workspace', 'manual'),
            ('high_probability_panel.render_high_probabilities', 'probabilities'),
            ('daily_analysis_panel.render_daily', 'daily'),
            ('journal_panel.render_journal', 'journal'),
            ('daily_evaluation_panel.render_evaluation', 'evidence'),
            ('daily_selection_study_panel.render_selection_study', 'study'),
            ('league_pattern_panel.render_leagues', 'explore'),
            ('automation_panel.render_automation', 'automation'),
            ('model_training_panel.render_training', 'models'),
            ('api_football_panel.render_api_football', 'settings'),
        )
        self.renders = {}
        for target, label in modules:
            def marker(*_args, value=label, **_kwargs):
                import streamlit as st
                st.markdown(f'PANEL:{value}')
            stub = patch(target, side_effect=marker)
            self.renders[label] = stub.start()
            self.addCleanup(stub.stop)
        for target in ('cloud_ui.render_storage_status', 'admin_access.render_identity',
                       'cloud_ui.install_retry_watchdog'):
            stub = patch(target)
            stub.start(); self.addCleanup(stub.stop)
        # Keep the test independent of any private deployment configuration.
        stub = patch('cloud_ui.guarded', side_effect=lambda function: function)
        stub.start(); self.addCleanup(stub.stop)
        stub = patch('api_football.ApiFootballClient.get',
                     side_effect=AssertionError('Navigation cannot call API'))
        self.network = stub.start(); self.addCleanup(stub.stop)

    def open(self):
        at = AppTest.from_file('app.py', default_timeout=15).run()
        self.assertFalse(at.exception)
        return at

    def current(self, at):
        return [node.value for node in at.markdown if node.value.startswith('PANEL:')]

    def test_five_entries_keep_ten_registered_routes(self):
        self.assertEqual(sum(visible for _path, _title, _icon, _default, visible in nav.PAGES), 5)
        at = self.open()
        for path, _title, _icon, _default, _visible in nav.PAGES:
            at.switch_page(path).run()
            self.assertFalse(at.exception, path)
            self.assertEqual(len(self.current(at)), 1, path)
        self.network.assert_not_called()

    def test_today_selector_switches_pages_without_executing_other_views(self):
        at = self.open()
        self.assertEqual(self.current(at), ['PANEL:manual'])
        self.renders['probabilities'].assert_not_called()
        self.renders['daily'].assert_not_called()
        at.segmented_control(key='workflow_today').set_value('high_probabilities').run()
        self.assertFalse(at.exception)
        self.assertEqual(self.current(at), ['PANEL:probabilities'])
        self.assertEqual(at.segmented_control(key='workflow_today').value, 'high_probabilities')
        # AppTest's next request keeps its originally selected page hash unless
        # switch_page() supplies the browser's new URL explicitly.
        at.switch_page('ui_pages/high_probabilities.py').run()
        at.segmented_control(key='workflow_today').set_value('daily').run()
        self.assertFalse(at.exception)
        self.assertEqual(self.current(at), ['PANEL:daily'])
        self.network.assert_not_called()

    def test_deep_link_wins_over_last_subview_and_controls_are_isolated(self):
        at = self.open()
        at.switch_page('ui_pages/models.py').run()
        self.assertEqual(self.current(at), ['PANEL:models'])
        self.assertEqual(at.segmented_control(key='workflow_manage').value, 'models')
        self.assertFalse(any(node.key == 'workflow_today' for node in at.segmented_control))
        at.segmented_control(key='workflow_manage').set_value('automation').run()
        self.assertFalse(at.exception)
        self.assertEqual(self.current(at), ['PANEL:automation'])
        at.switch_page('ui_pages/settings.py').run()
        self.assertFalse(at.exception)
        self.assertEqual(self.current(at), ['PANEL:settings'])
        self.assertEqual(at.segmented_control(key='workflow_manage').value, 'settings')
        at.switch_page('ui_pages/selection_study.py').run()
        self.assertEqual(self.current(at), ['PANEL:study'])
        at.segmented_control(key='workflow_evidence').set_value('daily_evaluation').run()
        self.assertFalse(at.exception)
        self.assertEqual(self.current(at), ['PANEL:evidence'])


if __name__ == '__main__':
    unittest.main()
