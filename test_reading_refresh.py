"""Reading refreshes keep user work and never start a provider collection."""
import ast
from copy import deepcopy
from pathlib import Path
import unittest

import test_daily_analysis_panel as daily
import test_high_probability_panel as probabilities
import test_journal_panel as journal


class ReadingRefreshTests(unittest.TestCase):
    def fixture(self, cls):
        case = cls()
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def test_reading_views_have_no_periodic_whole_panel_redraw(self):
        for source, name in (
                ('journal_panel.py', 'render_journal'),
                ('daily_analysis_panel.py', 'render_daily'),
                ('high_probability_panel.py', 'render_high_probabilities')):
            with self.subTest(source=source):
                tree = ast.parse(Path(source).read_text())
                function = next(node for node in ast.walk(tree)
                                if isinstance(node, ast.FunctionDef) and node.name == name)
                timers = [node for decorator in function.decorator_list for node in ast.walk(decorator)
                          if isinstance(node, ast.keyword) and node.arg == 'run_every']
                self.assertFalse(timers, 'A clock must not redraw a reading or composition panel')

    def test_journal_refresh_keeps_opened_analysis_and_frozen_probabilities(self):
        case = self.fixture(journal.JournalPanelTests)
        saved = case.seed(size=3)
        before = deepcopy(saved['methods'])
        at = case.view(case.open(), saved['id'])
        at.button(key='journal_view_refresh').click().run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state['journal_selected_id'], saved['id'])
        self.assertEqual(journal.journal.get_entry(saved['id'])['methods'], before)
        case.network.assert_not_called()

    def test_daily_refresh_keeps_detail_and_return_filters_without_preparing(self):
        case = self.fixture(daily.DailyPanelTests)
        case.entries = [daily.entry('retained', size=2), daily.entry('other', size=3)]
        before = deepcopy(case.entries)
        at = case.open()
        at.selectbox(key='daily_group_size').set_value(2).run()
        next(button for button in at.button if button.label == 'Voir l’analyse').click().run()
        chosen = at.session_state['daily_selected']
        at.button(key='daily_view_refresh').click().run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state['daily_selected'], chosen)
        self.assertEqual(at.session_state['daily_preferences']['daily_group_size'], 2)
        self.assertEqual(case.entries, before)
        case.prepare.assert_not_called()
        case.refresh.assert_not_called()
        case.network.assert_not_called()

    def test_probability_refresh_keeps_mixed_draft_and_calculation_without_new_api(self):
        case = self.fixture(probabilities.HighProbabilityPanelTests)
        at = case.open()
        at.multiselect(key='hp_mix_widget_ids').set_value(['1', '2']).run()
        at.segmented_control(key='hp_mix_choice_1').set_value('Égal').run()
        at.button(key='hp_mix_prepare').click().run()
        prepared = deepcopy(at.session_state['hp_prepared'])
        at.button(key='hp_view_refresh').click().run()
        self.assertFalse(at.exception)
        self.assertEqual(at.multiselect(key='hp_mix_widget_ids').value, ['1', '2'])
        self.assertEqual(at.session_state['hp_prepared'], prepared)
        case.prepare.assert_called_once()
        case.refresh.assert_not_called()
        case.record.assert_not_called()
        case.readonly()


if __name__ == '__main__':
    unittest.main()
