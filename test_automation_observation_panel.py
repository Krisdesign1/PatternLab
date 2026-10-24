from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import automation_observation_panel as panel
import local_operation_report as observations


NOW=datetime(2026,10,24,8,tzinfo=timezone.utc)


class AutomationObservationPanelTests(unittest.TestCase):
    def setUp(self):
        temporary=TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        panel._report.clear(); self.addCleanup(panel._report.clear)
        self.value=observations.build(now=NOW,days=2)
        self.read=self.stub(observations,'collect',side_effect=lambda *args,**kwargs:deepcopy(self.value))
        self.network=self.stub(__import__('api_football').ApiFootballClient,'get',side_effect=AssertionError('No API'))
        self.runner=self.stub(__import__('automation_worker'),'run_cycle',side_effect=AssertionError('No cycle'))
        self.writer=self.stub(__import__('automation_worker'),'_write',side_effect=AssertionError('No data write'))

    def stub(self, target, name, **kwargs):
        mock=patch.object(target,name,**kwargs); value=mock.start(); self.addCleanup(mock.stop)
        return value

    def app(self,days=7):
        return AppTest.from_string('from pathlib import Path\nfrom automation_observation_panel import render\n'
            f'render(Path({str(self.root)!r}),days={days})',default_timeout=15).run()

    def open(self,app):
        app.session_state['automation_daily_observations']=True
        return app.run()

    def assert_readonly(self):
        self.network.assert_not_called(); self.runner.assert_not_called(); self.writer.assert_not_called()
        self.assertEqual(list(self.root.iterdir()),[])

    def test_collapsed_expander_performs_no_reads_and_no_actions(self):
        app=self.app()
        self.assertFalse(app.exception)
        self.read.assert_not_called()
        self.assertFalse(app.dataframe)
        self.assertFalse(app.get('download_button'))
        self.assert_readonly()

    def test_explicit_open_shows_daily_counts_pending_not_failures_and_download(self):
        today=self.value['daily'][-1]
        today['completed_refreshes']['calendar']['count']=3
        today.update(forecast_unique_matches=4,forecasts_before_kickoff=4,
                     forecast_lead_seconds_min=900,result_states={'pending':3,'excluded':1})
        app=self.open(self.app(days=60))
        self.assertFalse(app.exception)
        self.assertEqual(self.read.call_args.kwargs,dict(days=31,include_evidence=True))
        operations=next(node.value for node in app.dataframe if 'Calendrier' in node.value.columns)
        forecasts=next(node.value for node in app.dataframe if 'Matchs uniques' in node.value.columns)
        self.assertEqual(operations.iloc[0]['Calendrier'],3)
        self.assertEqual(forecasts.iloc[0]['En attente'],3)
        self.assertEqual(forecasts.iloc[0]['Exclus du bilan'],1)
        self.assertEqual(forecasts.iloc[0]['Avance minimale (min)'],15)
        self.assertEqual(len(app.get('download_button')),1)
        self.assertTrue(any('disponibilité continue' in node.value for node in app.caption))
        self.assertTrue(any('n’est pas un échec' in node.value for node in app.caption))
        app.run()
        self.assertEqual(self.read.call_count,1)
        self.assert_readonly()

    def test_predictive_effectifs_remain_unique_and_maturity_is_explicit_without_pooled_score(self):
        self.value['predictive_evidence']=dict(state='available',coverage=dict(unique_matches=294,
            evaluated_unique_matches=238,pending_unique_matches=55,mature_cohorts=0,cohorts=293,
            max_closed_blocks=0,required_blocks=20))
        app=self.open(self.app())
        self.assertFalse(app.exception)
        self.assertEqual(next(node.value for node in app.metric if node.label=='Matchs comparables uniques'),'294')
        self.assertEqual(next(node.value for node in app.metric if node.label=='Cohortes avec assez de blocs clos'),'0 / 293')
        self.assertTrue(any('mélangeant les cohortes' in node.value for node in app.caption))
        self.assert_readonly()

    def test_unavailable_data_error_does_not_expose_exception_or_trigger_recovery_action(self):
        self.read.side_effect=ValueError('private-token-must-not-appear')
        app=self.open(self.app())
        self.assertFalse(app.exception)
        self.assertTrue(any('ne peuvent pas être lues' in node.value for node in app.warning))
        self.assertFalse(any('private-token' in node.value for node in app.warning))
        self.assertFalse(app.dataframe)
        self.assert_readonly()


if __name__=='__main__':
    unittest.main()
