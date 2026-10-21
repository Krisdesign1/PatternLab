"""Seasonal views never train on opening or refit when filtering a report."""
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import seasonal_validation_panel as panel


def report_fixture():
    sizes = [2, 4, 6, 8]
    def metrics(n):
        return {name: dict(tested=n, top1=0, top3=1, loss=4.2) for name in panel.METHODS}
    split = {name: dict(seasons=[year], matches=380, start=f'{year}-08-01', end=f'{year+1}-05-31')
             for name, year in [('train', 2022), ('validation', 2023), ('test', 2024)]}
    return dict(league='39', source='API-Football', generated_at='2026-10-18T12:00:00Z',
                cutoff='2026-10-18', fingerprint='history', sizes=sizes,
                aggregate={str(size): metrics(207) for size in sizes},
                aggregate_complete={str(size): metrics(190) for size in sizes},
                aggregate_partial={str(size): metrics(17) for size in sizes},
                folds=[dict(test_season=year, partial=year == 2026, split=split,
                            sizes={str(size): dict(selection=dict(params={}, alpha=0, window='all'),
                                                   validation=metrics(95), test=metrics(17 if year == 2026 else 95))
                                   for size in sizes}) for year in (2024, 2025, 2026)])


class SeasonalPanelTests(unittest.TestCase):
    def setUp(self):
        self.report = report_fixture()
        partition = dict(seasons=[2022], matches=380, start='2022-08-01', end='2023-05-31')
        self.plan = dict(suitable=True, fingerprint='history', seasons=[dict(season=2022, matches=380)],
                         folds=[dict(train=partition, validation=partition, test=partition)])
        self.planner = self.patch('plan', return_value=self.plan)
        self.runner = self.patch('run', return_value=self.report)
        self.saver = self.patch('save_report')
        self.loader = self.patch('load_report', return_value=self.report)

    def patch(self, name, **kwargs):
        patcher = patch.object(panel.evaluation, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def open(self):
        return AppTest.from_string(
            "from seasonal_validation_panel import render\nrender([], '39', 'API-Football')",
            default_timeout=15).run()

    def test_opening_and_changing_scope_only_reads_saved_report(self):
        at = self.open()
        self.assertFalse(at.exception)
        table = next(item.value for item in at.dataframe if 'Groupes testés' in item.value.columns)
        self.assertEqual(set(table['Groupes testés']), {190})
        at.segmented_control(key='seasonal_scope_39').set_value('partial').run()
        self.assertFalse(at.exception)
        table = next(item.value for item in at.dataframe if 'Groupes testés' in item.value.columns)
        self.assertEqual(set(table['Groupes testés']), {17})
        self.runner.assert_not_called()
        self.saver.assert_not_called()

    def test_evaluation_and_save_require_explicit_click(self):
        at = self.open()
        at.button(key='seasonal_run_39').click().run()
        self.assertFalse(at.exception)
        self.runner.assert_called_once()
        self.saver.assert_called_once_with(self.report)
        at.segmented_control(key='seasonal_size_39').set_value(8).run()
        self.assertFalse(at.exception)
        self.assertEqual(self.runner.call_count, 1)

    def test_failed_evaluation_keeps_previous_report_visible(self):
        self.runner.side_effect = ValueError('Historique insuffisant')
        at = self.open()
        at.button(key='seasonal_run_39').click().run()
        self.assertFalse(at.exception)
        self.assertTrue(any('Historique insuffisant' in item.value for item in at.error))
        self.saver.assert_not_called()
        self.assertTrue(any('Groupes testés' in item.value.columns for item in at.dataframe))

    def test_insufficient_history_does_not_run_or_load_report(self):
        self.plan.update(suitable=False, diagnostics=['Importez davantage de saisons.'])
        at = self.open()
        self.assertFalse(at.exception)
        self.assertFalse(at.button)
        self.loader.assert_not_called()


if __name__ == '__main__':
    unittest.main()
