"""The future comparison view never starts inference or enrollment on open."""
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import candidate_tracking_panel as panel


def script():
    import candidate_tracking_panel as panel
    panel.render(dict(league='39',run_id='a'*64,report=dict(policy=dict(evaluation_procedure='native'))))


class TrackingPanelTests(unittest.TestCase):
    def setUp(self):
        panel._summary.clear()
        self.addCleanup(panel._summary.clear)
        self.registered=[]
        self.enterContext(patch.object(panel.tracking,'_implementation',return_value={}))
        self.enterContext(patch.object(panel.tracking,'studies',side_effect=lambda:list(self.registered)))
        self.study=dict(id='b'*64,run_id='a'*64,league='39',registered_at='2026-10-25T00:00:00+00:00',sizes=[2,4],implementation={})
        self.enroll=self.enterContext(patch.object(panel.tracking,'register',side_effect=self.activate))
        self.enterContext(patch.object(panel.admin_access,'can_admin',return_value=True))
        self.auth=self.enterContext(patch.object(panel.admin_access,'require_admin'))
        def size(n):
            pair=dict(method='catboost',blocks=0,groups=0,status='insufficient',
                      delta_loss=dict(estimate=None),reason='Il faut davantage de blocs clos.')
            return dict(size=n,counts={'pending':1},metrics=[dict(method=name,groups=0,loss=None,top1=0,top3=0)
                for name in panel.tracking.METHODS], evidence=dict(comparisons=[pair]),candidate_vs_incumbent=pair,
                calibration={name:dict(matches=0,logloss=None,brier=None,reliability={label:[] for label in ('1st','Égal','2nd')})
                             for name in panel.tracking.METHODS})
        self.summary=self.enterContext(patch.object(panel.tracking,'summary',return_value=dict(study=self.study,
            total=2,unique_matches=4,sizes=[size(2),size(4)],entries=[],promoted=False)))

    def activate(self,*args,**kwargs):
        self.registered[:]=[self.study]
        return self.study

    def test_opening_does_not_activate_or_run_a_cycle(self):
        with patch.object(panel.tracking,'run_cycle',side_effect=AssertionError('No inference')):
            at=AppTest.from_function(script).run()
            self.assertFalse(at.exception)
            self.enroll.assert_not_called();self.summary.assert_not_called()
            at.button(key='candidate_enroll').click().run()
            self.assertFalse(at.exception)
            self.enroll.assert_called_once_with('39','a'*64)
            self.auth.assert_called_once()
            self.assertEqual(at.metric[0].value,'1')
            self.assertTrue(any('blocs clos' in c.value for c in at.caption))

    def test_size_change_keeps_same_study_without_new_enrollment(self):
        self.activate()
        at=AppTest.from_function(script).run()
        at.segmented_control(key='candidate_size_'+'b'*64).set_value(2).run()
        self.assertFalse(at.exception)
        self.enroll.assert_not_called()
        self.assertEqual(at.metric[1].value,'0')

    def test_readonly_admin_cannot_enroll(self):
        with patch.object(panel.admin_access,'can_admin',return_value=False):
            at=AppTest.from_function(script).run()
        self.assertFalse(at.exception)
        self.assertTrue(at.button(key='candidate_enroll').disabled)
        self.enroll.assert_not_called()


if __name__=='__main__': unittest.main()
