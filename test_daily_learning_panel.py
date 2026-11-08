"""Browsing daily learning never prepares a model, captures, or calls an API."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import daily_learning_panel as panel
import daily_learning_models as models


def script():
    from daily_learning_panel import render
    render()


class DailyLearningPanelTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'data'
        self.report=panel.tracking.summary(self.root)
        self.patch(panel.api,'DATA_DIR',self.root)
        self.read=self.patch(panel.tracking,'summary',side_effect=lambda **kw:deepcopy(self.report))
        self.train=self.patch(models,'train',side_effect=AssertionError('No training'))
        self.capture=self.patch(panel.tracking,'capture',side_effect=AssertionError('No capture'))
        self.reconcile=self.patch(panel.tracking,'reconcile',side_effect=AssertionError('No writes'))
        self.network=self.patch(panel.api.ApiFootballClient,'get',side_effect=AssertionError('No API'))
        panel._read.clear();self.addCleanup(panel._read.clear)

    def patch(self,obj,name,*args,**kwargs):
        p=patch.object(obj,name,*args,**kwargs);v=p.start();self.addCleanup(p.stop);return v

    def readonly(self):
        for v in (self.train,self.capture,self.reconcile,self.network):v.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_empty_view_leaves_rates_empty_and_does_not_activate(self):
        at=AppTest.from_function(script,default_timeout=10).run()
        self.assertFalse(at.exception)
        self.assertEqual(at.metric[0].value,'0')
        self.assertTrue(at.dataframe[0].value['Réussite (%)'].isna().all())
        self.assertTrue(at.dataframe[1].value['Confiance moyenne annoncée (%)'].isna().all())
        self.readonly()

    def test_switching_tables_uses_cache_without_side_effects(self):
        at=AppTest.from_function(script,default_timeout=10).run()
        at.segmented_control(key='daily_learning_view').set_value('training').run()
        self.assertFalse(at.exception)
        self.assertTrue(any('Aucun candidat' in v.value for v in at.info))
        at.segmented_control(key='daily_learning_view').set_value('forecasts').run()
        self.assertFalse(at.exception)
        self.assertTrue(any('Aucune capture' in v.value for v in at.info))
        self.read.assert_called_once()
        self.readonly()

    def test_unreadable_evidence_does_not_invent_metrics(self):
        self.read.side_effect=ValueError('broken')
        at=AppTest.from_function(script,default_timeout=10).run()
        self.assertFalse(at.exception)
        self.assertTrue(at.error)
        self.assertFalse(at.metric)
        self.readonly()

    def test_evidence_route_does_not_load_other_journals(self):
        import daily_evaluation_panel as evidence
        def route():
            import streamlit as st
            from daily_evaluation_panel import render_evaluation
            st.session_state['daily_audit_view']='learning'
            render_evaluation()
        with patch.object(panel,'render') as render,patch.object(evidence,'render_pending_overview') as pending:
            at=AppTest.from_function(route,default_timeout=15).run()
            self.assertFalse(at.exception)
            render.assert_called_once();pending.assert_not_called()
        self.readonly()


if __name__=='__main__':unittest.main()
