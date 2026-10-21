"""The context comparison is explicit and report filters do not retrain."""
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import contextual_validation_panel as panel


def fixture_report():
    def metrics(n):
        return {name: dict(tested=n, top1=2, top3=5, loss=4.2) for name in panel.METHODS}
    sizes = [2, 4, 6, 8]
    partition = dict(seasons=[2022], matches=380, start='2022-08-01', end='2023-05-31')
    params = dict(k=20, half_life_days=None, mix=0.0)
    support = dict(mean_neighbors=20, mean_ess=19.0, mean_eta=0.0, active_groups=0)
    return dict(source='API-Football', league='39', generated_at='2026-10-18T18:00:00Z',
                cutoff='2026-10-18', fingerprint='same-history', exploratory=True, sizes=sizes,
                aggregate={str(size): metrics(207) for size in sizes},
                aggregate_complete={str(size): metrics(190) for size in sizes},
                aggregate_partial={str(size): metrics(17) for size in sizes},
                folds=[dict(test_season=year, partial=year == 2026,
                            split={name: partition for name in ('train', 'validation', 'test')},
                            sizes={str(size): dict(selection=dict(params=params), validation=metrics(95),
                                                   test=metrics(17 if year == 2026 else 95),
                                                   support_summary=dict(test=support, validation=support))
                                   for size in sizes}) for year in (2024, 2025, 2026)],
                deployment=dict(selection_through='2026-05-24', generation='config-generation',
                                parameters={str(size): params for size in sizes}))


class ContextualValidationPanelTests(unittest.TestCase):
    def setUp(self):
        self.report = fixture_report()
        self.plan = dict(suitable=True, fingerprint='same-history', seasons=[dict(season=2022, matches=380)])
        self.patch('plan', return_value=self.plan)
        self.loader = self.patch('load_report', return_value=self.report)
        self.runner = self.patch('run', return_value=self.report)
        self.saver = self.patch('save_report')
        p = patch.object(panel.campaign, 'load_manifest', return_value=None)
        self.campaign = p.start(); self.addCleanup(p.stop)

    def patch(self, name, **kwargs):
        patcher = patch.object(panel.evaluation, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def open(self):
        return AppTest.from_string("from contextual_validation_panel import render\nrender([], '39', 'API-Football')",
                                  default_timeout=15).run()

    def test_opening_and_filtering_do_not_start_training_or_save(self):
        at = self.open()
        self.assertFalse(at.exception)
        self.assertTrue(any('exploratoire' in item.value for item in at.info))
        at.segmented_control(key='contextual_scope_39').set_value('partial').run()
        self.assertFalse(at.exception)
        table = next(item.value for item in at.dataframe if 'Groupes testés' in item.value.columns)
        self.assertEqual(set(table['Groupes testés']), {17})
        self.runner.assert_not_called()
        self.saver.assert_not_called()

    def test_run_is_explicit_and_size_change_only_reads_saved_report(self):
        at = self.open()
        at.button(key='contextual_run_39').click().run()
        self.assertFalse(at.exception)
        self.runner.assert_called_once()
        self.saver.assert_called_once_with(self.report)
        at.segmented_control(key='contextual_size_39').set_value(8).run()
        self.assertFalse(at.exception)
        self.assertEqual(self.runner.call_count, 1)

    def test_error_preserves_previous_report_and_zero_weight_is_explained(self):
        self.runner.side_effect = ValueError('Historique insuffisant')
        at = self.open()
        at.button(key='contextual_run_39').click().run()
        self.assertFalse(at.exception)
        self.saver.assert_not_called()
        self.assertTrue(any('Historique insuffisant' in item.value for item in at.error))
        self.assertTrue(any('poids nul' in item.value for item in at.caption))
        self.assertTrue(any('Groupes testés' in item.value.columns for item in at.dataframe))

    def test_short_history_never_reads_or_runs_a_model(self):
        self.plan.update(suitable=False, diagnostics=['Importez davantage de saisons.'])
        at = self.open()
        self.assertFalse(at.exception)
        self.assertFalse(at.button)
        self.runner.assert_not_called()
        self.loader.assert_not_called()

    def test_campaign_keeps_context_parameters_read_only(self):
        self.campaign.return_value = dict(status='frozen')
        self.plan['fingerprint'] = 'updated-history'
        at = self.open()
        self.assertFalse(at.exception)
        self.assertTrue(at.button(key='contextual_run_39').disabled)
        self.assertTrue(any('réglages de la campagne restent figés' in item.value for item in at.warning))
        self.runner.assert_not_called(); self.saver.assert_not_called()


if __name__ == '__main__':
    unittest.main()
