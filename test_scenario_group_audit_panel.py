from copy import deepcopy
import json
import unittest
from unittest.mock import patch
from streamlit.testing.v1 import AppTest

import scenario_group_audit_panel as panel


def report():
    rows=[]
    for size,status in ((2,'valid'),(3,'invalid'),(20,'unverified')):
        rows.append(dict(id='group-'+str(size),size=size,created_at='2026-10-31T10:00:00Z',
            match_labels=['Home '+str(size)+' — Away'],leagues=['39'],status=status,
            common_cutoff='2026-10-31',decision_at=None,issues=[] if status=='valid' else [
                dict(method='clubs',code='date',message='Date non vérifiée',severity=status)],
            methods=[dict(name='clubs',top1_probability=.5**size,top3_probability=3*.5**size,
                          remaining_probability=1-3*.5**size)],input_sha256='input'))
    return dict(version='test',generated_at='2026-10-31T10:00:00Z',source_sha256='source',implementation={},
                counts=dict(total=3,valid=1,unverified=1,invalid=1),rows=rows)


class GroupAuditPanelTests(unittest.TestCase):
    def setUp(self):
        p=patch.object(panel,'_load_report',return_value=report());self.read=p.start();self.addCleanup(p.stop)
        self.at=AppTest.from_string('from scenario_group_audit_panel import render\nrender()',default_timeout=20).run()

    def submit(self):return self.at.button(key='scenario_group_audit_read').click().run()
    def groups(self):return next(d.value for d in self.at.dataframe if 'Matchs dans le groupe' in d.value.columns)

    def test_lazy_read_has_no_automatic_work(self):
        self.read.assert_not_called();self.at.run();self.read.assert_not_called()
        self.submit();self.assertFalse(self.at.exception);self.read.assert_called_once_with()
        self.at.run();self.read.assert_called_once_with()

    def test_size_filters_contained_matches_not_rows_per_page(self):
        self.submit()
        for size in (2,3,20):
            self.at.selectbox(key='scenario_group_audit_size').set_value(size).run()
            self.assertFalse(self.at.exception)
            self.assertEqual(self.groups()['Matchs dans le groupe'].tolist(),[size])
        self.at.selectbox(key='scenario_group_audit_size').set_value(4).run()
        self.assertFalse(any('Matchs dans le groupe' in d.value.columns for d in self.at.dataframe))
        self.assertTrue(any('Aucun groupe' in node.value for node in self.at.info))
        self.read.assert_called_once_with()

    def test_status_and_search_filters_keep_whole_groups(self):
        self.submit()
        self.at.selectbox(key='scenario_group_audit_status').set_value('invalid').run()
        self.assertEqual(self.groups()['Groupe'].tolist(),['group-3'])
        self.at.text_input(key='scenario_group_audit_query').set_value('absent').run()
        self.assertTrue(any('Aucun groupe' in node.value for node in self.at.info))

    def test_failed_new_read_clears_previous_report(self):
        self.submit();self.assertTrue(self.at.dataframe)
        self.read.side_effect=ValueError('Données altérées')
        self.submit();self.assertFalse(self.at.exception);self.assertTrue(self.at.error)
        self.assertFalse(self.at.dataframe)

    def test_export_keeps_parent_fingerprint_filters_and_absolute_probabilities(self):
        self.submit()
        with patch.object(panel.st,'download_button',wraps=panel.st.download_button) as download:
            self.at.selectbox(key='scenario_group_audit_size').set_value(20).run()
        value=json.loads(download.call_args.args[1])
        self.assertEqual(value['source_sha256'],'source')
        self.assertEqual(value['filters']['size'],20)
        self.assertEqual(len(value['rows']),1)
        self.assertEqual(value['rows'][0]['methods'][0]['top1_probability'],.5**20)
        self.assertEqual(value['complete_ledger_counts']['total'],3)

    def test_cohorts_route_does_not_load_population_or_audit_until_request(self):
        import causal_partition_panel as cohorts
        with patch.object(cohorts.artifacts,'load_bundle',side_effect=AssertionError('No history')):
            at=AppTest.from_string('from causal_partition_panel import render\nrender()',default_timeout=20).run()
            at.segmented_control(key='causal_cohort_tool').set_value('scenarios').run()
        self.assertFalse(at.exception)
        self.assertTrue(at.button(key='scenario_group_audit_read'))
        self.read.assert_not_called()


if __name__=='__main__':unittest.main()
