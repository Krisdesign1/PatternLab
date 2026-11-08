from copy import deepcopy
from datetime import datetime,timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import argumented_selection_study_panel as panel
import argumented_selection_replay as replay
import argumented_selection_statistics as statistics
from test_high_probability_engine import history,fixture
import api_football as api


class StudyPanelTests(unittest.TestCase):
    def setUp(self):
        rows=[api.normalize_fixture(x) for x in history(equal=True)+[
            fixture(100001,status='FT',stamp='2026-10-22T15:00:00+00:00',ht=(0,0),ft=(0,0))]]
        records=replay.replay(rows,start='2026-10-22',end='2026-10-23')['records']
        self.report=statistics.summarize(records)
        self.report.update(protocol=dict(start='2026-10-22',end='2026-10-23'),inventory=dict(rows=len(rows)))
        self.read=self.patch(panel.study,'load_report',side_effect=lambda:deepcopy(self.report))
        self.patch(panel.study,'signature',return_value=())
        self.run=self.patch(panel.study,'run',side_effect=AssertionError('No replay on render'))
        self.snapshot=self.patch(panel.study,'_snapshot',side_effect=AssertionError('No history load'))
        self.network=self.patch(api.ApiFootballClient,'get',side_effect=AssertionError('No network'))
        panel._read.clear();self.addCleanup(panel._read.clear)

    def patch(self,obj,name,*a,**kw):
        p=patch.object(obj,name,*a,**kw);v=p.start();self.addCleanup(p.stop);return v

    def open(self):
        at=AppTest.from_string('from argumented_selection_study_panel import render\nrender()',default_timeout=15).run()
        self.assertFalse(at.exception)
        return at

    def readonly(self):
        self.run.assert_not_called();self.snapshot.assert_not_called();self.network.assert_not_called()

    def test_real_report_schema_renders_same_n_comparisons_without_starting_compute(self):
        at=self.open()
        first=at.dataframe[0].value
        self.assertEqual(list(first['Matchs retenus']),[1,1,1])
        self.assertTrue(any('exploratoire' in x.value for x in at.info))
        at.selectbox(key='arg_study_policy').set_value('strict_recent7').run()
        self.assertFalse(at.exception)
        self.read.assert_called_once();self.readonly()

    def test_empty_view_does_not_fabricate_results(self):
        self.read.side_effect=None;self.read.return_value=None
        at=self.open()
        self.assertFalse(at.dataframe)
        self.assertTrue(any('Aucun rejeu' in x.value for x in at.info))
        self.readonly()

    def test_corrupt_report_has_no_replacement_metrics(self):
        self.read.side_effect=ValueError('broken')
        at=self.open()
        self.assertTrue(at.error);self.assertFalse(at.dataframe)
        self.readonly()

    def test_selection_tab_routes_lazily_without_reading_live_history(self):
        import argumented_selection_panel as main
        def screen():
            import streamlit as st
            from argumented_selection_panel import render
            st.session_state['arg_view']='study'
            render()
        with patch.object(panel,'render') as render,patch.object(main,'_history',side_effect=AssertionError('No live archive')):
            at=AppTest.from_function(screen,default_timeout=15).run()
            self.assertFalse(at.exception)
            render.assert_called_once()
        self.readonly()


if __name__=='__main__':unittest.main()
