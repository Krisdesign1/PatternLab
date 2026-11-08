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
            ('competition_trends_panel.render', 'competition_trends'),
            ('club_trends_panel.render', 'club_trends'),
            ('argumented_selection_panel.render', 'argumented_selection'),
            ('daily_analysis_panel.render_daily', 'daily'),
            ('journal_panel.render_journal', 'journal'),
            ('daily_evaluation_panel.render_evaluation', 'evidence'),
            ('bet_simulation_panel.render_simulation', 'bet_simulation'),
            ('daily_selection_study_panel.render_selection_study', 'study'),
            ('league_pattern_panel.render_leagues', 'explore'),
            ('automation_panel.render_automation', 'automation'),
            ('model_training_panel.render_training', 'models'),
            ('api_football_panel.render_api_football', 'settings'),
            ('halftime_collection_panel.render_collection', 'collection'),
            ('operational_acceptance_panel.render', 'acceptance'),
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
                       'cloud_ui.install_retry_watchdog','halftime_collection_panel.render_compact'):
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

    def test_five_entries_keep_all_registered_routes(self):
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
        self.renders['competition_trends'].assert_not_called()
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

    def test_reception_route_only_runs_its_panel_and_preserves_main_navigation(self):
        at=self.open()
        at.switch_page('ui_pages/settings.py').run()
        self.renders['acceptance'].assert_not_called()
        at.segmented_control(key='workflow_manage').set_value('acceptance').run()
        self.assertFalse(at.exception)
        self.assertEqual(self.current(at),['PANEL:acceptance'])
        self.assertEqual(at.segmented_control(key='workflow_manage').value,'acceptance')
        self.assertEqual(sum(visible for _path,_title,_icon,_default,visible in nav.PAGES),5)
        self.network.assert_not_called()

    def test_shared_match_day_survives_secondary_and_main_page_navigation(self):
        from datetime import date
        at = self.open()
        target = date(2027, 1, 3)
        at.date_input(key='analysis_day_widget').set_value(target).run()
        for path in ('ui_pages/high_probabilities.py', 'ui_pages/competition_trends.py', 'ui_pages/club_trends.py',
                     'ui_pages/argumented_selection.py', 'ui_pages/daily.py',
                     'ui_pages/journal.py', 'ui_pages/settings.py', 'ui_pages/analyze.py'):
            at.switch_page(path).run()
            self.assertFalse(at.exception, path)
            self.assertEqual(at.date_input(key='analysis_day_widget').value, target)
            self.assertEqual(at.session_state['analysis_selected_day'], target)
        self.network.assert_not_called()

    def test_competition_trends_tab_and_deep_link_only_run_their_panel(self):
        at = self.open()
        self.renders['competition_trends'].assert_not_called()
        at.segmented_control(key='workflow_today').set_value('competition_trends').run()
        self.assertFalse(at.exception)
        self.assertEqual(self.current(at), ['PANEL:competition_trends'])
        self.assertEqual(at.segmented_control(key='workflow_today').value, 'competition_trends')
        for label in ('probabilities', 'argumented_selection', 'daily'):
            self.renders[label].assert_not_called()
        # A direct URL remains authoritative after visiting another workflow.
        at.switch_page('ui_pages/settings.py').run()
        self.assertFalse(at.exception)
        at.switch_page('ui_pages/competition_trends.py').run()
        self.assertFalse(at.exception)
        self.assertEqual(self.current(at), ['PANEL:competition_trends'])
        self.assertEqual(at.segmented_control(key='workflow_today').value, 'competition_trends')
        self.assertEqual(sum(visible for _path, _title, _icon, _default, visible in nav.PAGES), 5)
        self.network.assert_not_called()

    def test_club_trends_tab_and_direct_route_do_not_run_other_panels(self):
        at = self.open()
        self.renders['club_trends'].assert_not_called()
        at.segmented_control(key='workflow_today').set_value('club_trends').run()
        self.assertFalse(at.exception)
        self.assertEqual(self.current(at), ['PANEL:club_trends'])
        self.renders['competition_trends'].assert_not_called()
        at.switch_page('ui_pages/settings.py').run()
        at.switch_page('ui_pages/club_trends.py').run()
        self.assertFalse(at.exception)
        self.assertEqual(self.current(at), ['PANEL:club_trends'])
        self.assertEqual(at.segmented_control(key='workflow_today').value, 'club_trends')
        self.network.assert_not_called()

    def test_argumented_selection_tab_routes_without_running_other_panels(self):
        at=self.open()
        at.segmented_control(key='workflow_today').set_value('argumented_selection').run()
        self.assertFalse(at.exception)
        self.assertEqual(self.current(at),['PANEL:argumented_selection'])
        self.renders['probabilities'].assert_not_called()
        self.renders['daily'].assert_not_called()
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
