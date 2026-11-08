import unittest
from unittest.mock import patch
from streamlit.testing.v1 import AppTest

import causal_candidate_panel as panel


def report():
    metrics = dict(count=3, logloss=1.1, brier=.66, accuracy=1/3)
    return dict(id='a'*64, plan=dict(windows={name:dict(start='2026-01-01T00:00:00Z', end='2026-02-01T00:00:00Z')
                                           for name in ('train','tuning','calibration')}),
        inputs=dict(phases={name:dict(count=3) for name in ('train','tuning','calibration')}),
        selection=dict(params=dict(depth=3)), trials=[dict(params=dict(depth=3),metrics=metrics)],
        references={name:dict(clubs=metrics,frequency=metrics) for name in ('tuning','calibration')},
        calibration=dict(raw_metrics=metrics,calibrated_metrics=metrics,temperature=1.0),
        final_training=dict(count=6),completed_at='2026-10-30T23:00:00Z')


class CandidatePanelTests(unittest.TestCase):
    def setUp(self):
        self.patcher=patch.object(panel.candidates,'load_candidate',return_value=dict(report=report()))
        self.load=self.patcher.start();self.addCleanup(self.patcher.stop)
        self.at=AppTest.from_string('from causal_candidate_panel import render\nrender()',default_timeout=10).run()

    def test_opening_and_rerun_do_not_load_or_fit_until_explicit_read(self):
        self.load.assert_not_called();self.at.run();self.load.assert_not_called()
        with patch.object(panel.candidates,'fit_candidate',side_effect=AssertionError('No fit')):
            self.at.button(key='causal_candidate_read').click().run()
        self.assertFalse(self.at.exception);self.load.assert_called_once()
        self.assertEqual(len(self.at.dataframe[0].value),3)
        self.assertEqual(len(self.at.dataframe[1].value),7)
        self.assertTrue(any('aucune confirmation' in row.value for row in self.at.info))
        self.at.run();self.assertEqual(self.load.call_count,1)

    def test_failed_new_read_clears_previous_report(self):
        self.at.button(key='causal_candidate_read').click().run();self.assertTrue(self.at.dataframe)
        self.load.side_effect=ValueError('Version incompatible')
        self.at.button(key='causal_candidate_read').click().run()
        self.assertFalse(self.at.exception);self.assertTrue(self.at.error);self.assertFalse(self.at.dataframe)

    def test_candidate_route_returns_before_history_or_partition_reads(self):
        import causal_partition_panel as cohorts
        with patch.object(cohorts.artifacts,'load_bundle',side_effect=AssertionError('No cohort load')):
            at=AppTest.from_string('from causal_partition_panel import render\nrender()',default_timeout=10).run()
            at.segmented_control(key='causal_cohort_tool').set_value('candidate').run()
        self.assertFalse(at.exception);self.assertTrue(at.button(key='causal_candidate_read'))
        self.load.assert_not_called()


if __name__=='__main__':
    unittest.main()
