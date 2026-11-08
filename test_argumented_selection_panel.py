from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import argumented_selection_panel as panel
import argumented_selection_engine as engine
from test_argumented_selection_engine import NOW,scan,history


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'absent'
        self.report=engine.build(scan(),history(),NOW)
        self.saved=panel.store.summary(self.root)
        self.patch(panel.api,'DATA_DIR',self.root)
        self.patch(panel,'_now',return_value=NOW)
        self.load=self.patch(panel.workspace,'load_report',side_effect=lambda **kw:deepcopy(self.report))
        self.read=self.patch(panel.store,'summary',side_effect=lambda **kw:deepcopy(self.saved))
        self.network=self.patch(panel.api.ApiFootballClient,'get',side_effect=AssertionError('No API on read'))
        self.capture=self.patch(panel.workspace,'capture_now',side_effect=AssertionError('No automatic write'))
        self.patch(panel.admin_access,'can_admin',return_value=False)
        panel._history.clear();self.addCleanup(panel._history.clear)

    def patch(self,obj,name,*a,**kw):
        p=patch.object(obj,name,*a,**kw);v=p.start();self.addCleanup(p.stop);return v

    def open(self):
        at=AppTest.from_string('from argumented_selection_panel import render\nrender()',default_timeout=15).run()
        self.assertFalse(at.exception)
        return at

    def readonly(self):
        self.network.assert_not_called();self.capture.assert_not_called()
        self.assertFalse(self.root.exists())

    def captured(self,choice=None,status='settled',correct=False,version='argumented-selection-v1'):
        row=deepcopy(self.report['rows'][0])
        if choice:row['choice']=choice
        row.update(id=version,selection_version=version,day='2026-10-22',created_at='2026-10-22T08:00:00+00:00',
            status=status,actual=row['choice'] if correct else '1st',ht='0-0',ft='1-0',correct=correct,issues=[])
        return row

    def test_table_contains_reasons_all_probabilities_spans_and_no_network(self):
        at=self.open()
        table=at.dataframe[0].value
        self.assertEqual(len(table),1)
        self.assertEqual(table.iloc[0]['10 précédents domicile'],'10/10')
        self.assertIn('Pourquoi retenu',table.columns)
        self.assertIn('Période des 10 domicile',table.columns)
        self.assertIn('Écart (points)',table.columns)
        for label in engine.VALUES:self.assertIn(label+' (%)',table.columns)
        self.readonly()

    def test_category_filter_and_all_page_size_preserve_counts(self):
        at=self.open()
        at.selectbox(key='arg_current_outcome').set_value('1st').run()
        self.assertFalse(at.exception)
        self.assertFalse(at.dataframe)
        at.selectbox(key='arg_current_outcome').set_value('Tous').run()
        at.selectbox(key='arg_current_table_size').set_value('Tous').run()
        self.assertFalse(at.exception)
        self.assertEqual(len(at.dataframe[0].value),1)
        self.readonly()

    def test_history_uses_original_probability_and_pending_is_not_a_loss(self):
        row=self.captured(status='pending')
        self.saved.update(rows=[row],first_matches=[row])
        at=self.open()
        at.segmented_control(key='arg_view').set_value('history').run()
        self.assertFalse(at.exception)
        self.assertEqual([v.value for v in at.metric],['1','0','0','—'])
        self.assertEqual(at.dataframe[0].value.iloc[0]['Verdict'],['En attente'])
        self.assertEqual(at.dataframe[0].value.iloc[0]['Probabilité (%)'],60)
        self.readonly()

    def test_filters_cannot_substitute_later_generation_for_first_match(self):
        first=self.captured(choice='1st',correct=False)
        later=self.captured(choice='2nd',correct=True,version='v2')
        later['created_at']='2026-10-22T08:05:00+00:00'
        self.saved.update(rows=[first,later],first_matches=[first])
        at=self.open()
        at.segmented_control(key='arg_view').set_value('history').run()
        at.selectbox(key='arg_history_outcome').set_value('2nd').run()
        self.assertFalse(at.exception)
        self.assertEqual(at.metric[0].value,'0')
        self.assertEqual(at.metric[3].value,'—')
        self.readonly()

    def test_saved_current_match_keeps_first_probability_even_if_live_scan_changes(self):
        row=self.captured(status='pending');self.saved.update(rows=[row],first_matches=[row])
        self.report['rows'][0]['probabilities']={'1st':.1,'Égal':.1,'2nd':.8}
        at=self.open()
        self.assertEqual(at.dataframe[0].value.iloc[0]['Probabilité (%)'],60)
        self.assertEqual(at.metric[1].value,'1')
        self.readonly()

    def test_restore_columns_recreates_table_without_writes(self):
        at=self.open()
        at.button(key='arg_current_table_restore').click().run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state['arg_current_table_columns'],1)
        self.readonly()

    def test_missing_history_shows_empty_metrics(self):
        at=self.open()
        at.segmented_control(key='arg_view').set_value('history').run()
        self.assertFalse(at.exception)
        self.assertEqual(at.metric[3].value,'—')
        self.assertFalse(at.dataframe)
        self.readonly()


if __name__=='__main__':unittest.main()
