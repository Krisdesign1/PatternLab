from copy import deepcopy
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest
import causal_population_panel as panel


def status():
    return dict(progress=dict(state='complete', processed=2, total=2, updated_at='2026-10-30T22:00:00Z'),
                policy=dict(start='2026-10-28', end_exclusive='2026-10-30', hour_utc=8),
                summary=dict(days=[dict(day='2026-10-29', targets=2, snapshots=2, labelled=1,
                                       result_exclusions=1, personalized=2)]))


class PopulationPanelTests(unittest.TestCase):
    def setUp(self):
        self.patcher = patch.object(panel.population, 'status', return_value=status())
        self.read = self.patcher.start(); self.addCleanup(self.patcher.stop)
        self.at = AppTest.from_string('from causal_population_panel import render\nrender()', default_timeout=10).run()

    def test_opening_never_loads_preparation_or_computes_features(self):
        self.read.assert_not_called()
        self.at.run(); self.read.assert_not_called()
        with patch.object(panel.population, 'run', side_effect=AssertionError('No work on UI')):
            self.at.button(key='causal_population_read').click().run()
        self.assertFalse(self.at.exception)
        self.read.assert_called_once()
        self.assertEqual(self.at.dataframe[0].value.iloc[0]['Cibles'], 2)
        self.at.run(); self.assertEqual(self.read.call_count, 1)

    def test_failed_read_clears_prior_success(self):
        self.at.button(key='causal_population_read').click().run()
        self.assertTrue(self.at.dataframe)
        self.read.side_effect = ValueError('Code incompatible')
        self.at.button(key='causal_population_read').click().run()
        self.assertFalse(self.at.exception); self.assertTrue(self.at.error)
        self.assertFalse(self.at.dataframe)

    def test_partial_progress_has_no_result_claim(self):
        value = status(); value.pop('summary')
        value['progress'].update(state='preparing_features', processed=1)
        self.read.return_value = value
        self.at.button(key='causal_population_read').click().run()
        self.assertFalse(self.at.exception); self.assertFalse(self.at.dataframe)

    def test_population_route_returns_before_partition_reads(self):
        import causal_partition_panel as cohorts
        with patch.object(cohorts.artifacts, 'load_bundle', side_effect=AssertionError('No cohort loading')):
            at = AppTest.from_string('from causal_partition_panel import render\nrender()', default_timeout=10).run()
            at.segmented_control(key='causal_cohort_tool').set_value('population').run()
        self.assertFalse(at.exception)
        self.assertTrue(at.button(key='causal_population_read'))
        self.read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
