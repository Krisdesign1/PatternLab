from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import operational_acceptance as acceptance


NOW=datetime(2026,11,2,18,tzinfo=timezone.utc)


def sample_inputs():
    pid='a'*64
    selected=dict(id=pid,candidate_id='b'*64,registered_at='2026-11-02T17:00:00+00:00',followup={})
    archived=dict(rows=[dict(id=lot,state='verified',tests_passed=5,recorded_at=NOW.isoformat(),facts={})
                         for lot in ('lot11','lot12')],source_sha256='c'*64)
    windows=dict(protocols=[dict(id=pid,candidate_id='b'*64,implementation_compatible=True,
        start='2026-11-09T00:00:00+00:00',end='2027-01-04T00:00:00+00:00')],
        days=[dict(protocol_id=pid,decision_at='2026-11-09T08:00:00+00:00',
            deadline_at='2026-11-09T08:30:00+00:00',registration_ready=True,state='upcoming',
            missing_known_targets=None)],source_sha256='d'*64)
    op=dict(state='available',truncated=False,source_sha256='e'*64,issues=[],
        latest_operations={phase:dict(state='recent',observed_at=NOW.isoformat(),day='2026-11-02',age_seconds=0)
                           for phase in ('calendar','results','forecasts','reconcile')},
        cycles=[dict(id='f'*32,origin='service',started_at=NOW.isoformat(),status='running',signal_state='recent')])
    artifact=dict(status='valid',source_sha256='f'*64)
    return archived,windows,op,artifact,selected


def sample_report():
    values=sample_inputs()
    return dict(version=acceptance.VERSION,as_of=NOW.isoformat(),interval_minutes=15,
        evidence=values[0],windows=values[1],operations=values[2],candidate=values[3],source_failures=[],
        **acceptance.build(*values,now=NOW,interval_minutes=15),report_sha256='0'*64,
        atomic_across_sources=False)


class OperationalAcceptanceTests(unittest.TestCase):
    def build(self,values=None,now=NOW):
        return acceptance.build(*(values or sample_inputs()),now=now,interval_minutes=15)

    def row(self,result,name):return next(r for r in result['criteria'] if r['id']==name)

    def test_valid_current_sources_and_archives_do_not_automatically_accept_application(self):
        r=self.build()
        self.assertEqual(r['overall_decision'],'reception_open');self.assertFalse(r['automatic_acceptance'])
        self.assertFalse(r['predictive_improvement_demonstrated']);self.assertFalse(r['continuous_availability_proven'])
        self.assertEqual(self.row(r,'candidate')['state'],'verified')
        self.assertEqual(self.row(r,'followup')['state'],'not_due')
        self.assertEqual(self.row(r,'continuous')['state'],'not_demonstrated')

    def test_recent_heartbeat_does_not_hide_stale_calendar(self):
        values=sample_inputs();values[2]['latest_operations']['calendar']['state']='stale'
        r=self.build(values)
        self.assertEqual(self.row(r,'process')['state'],'observed')
        self.assertEqual(self.row(r,'freshness')['state'],'attention')

    def test_recent_prior_day_operation_or_truncated_window_never_confirms_freshness(self):
        for mutation in ('day','truncated'):
            values=sample_inputs()
            if mutation=='day':values[2]['latest_operations']['calendar']['day']='2026-11-01'
            else:values[2]['truncated']=True
            self.assertEqual(self.row(self.build(values),'freshness')['state'],'attention')

    def test_missed_window_is_attention_and_not_a_prediction_loss(self):
        values=sample_inputs();values[1]['days'][0]['state']='missed'
        result=self.build(values,now=datetime(2026,11,9,9,tzinfo=timezone.utc))
        row=self.row(result,'followup')
        self.assertEqual(row['state'],'attention');self.assertNotIn('perdu',row['observation'])

    def test_bad_candidate_bytes_do_not_get_a_keep_frozen_success_action(self):
        values=sample_inputs();values[3]['status']='invalid'
        row=self.row(self.build(values),'candidate')
        self.assertEqual(row['state'],'attention');self.assertIn('Examiner',row['next_action'])

    def test_incompatible_protocol_is_read_only_and_not_an_executable_future_period(self):
        values=sample_inputs();values[1]['protocols'][0]['implementation_compatible']=False
        result=self.build(values)
        self.assertEqual(self.row(result,'candidate')['state'],'attention')
        self.assertEqual(self.row(result,'followup')['state'],'attention')
        self.assertIn('sans nouvelle capture',self.row(result,'followup')['observation'])

    def test_different_protocol_metadata_does_not_get_registration_confirmation(self):
        values=sample_inputs();values[4]['candidate_id']='1'*64
        result=self.build(values)
        self.assertEqual(self.row(result,'registration')['state'],'attention')

    def test_absent_receipt_does_not_invent_zero_successful_tests(self):
        values=sample_inputs();values[0]['rows'][0].update(state='missing',tests_passed=None)
        row=self.row(self.build(values),'lot11')
        self.assertEqual(row['state'],'unavailable');self.assertNotIn('0 tests',row['observation'])

    def test_collect_keeps_latest_incompatible_protocol_instead_of_hiding_it(self):
        with tempfile.TemporaryDirectory() as folder:
            archived,windows,op,artifact,old=sample_inputs()
            new=dict(old,id='2'*64,registered_at='2026-11-02T17:30:00+00:00')
            windows['protocols'].append(dict(windows['protocols'][0],id=new['id'],implementation_compatible=False))
            windows['days'].append(dict(windows['days'][0],protocol_id=new['id']))
            with patch.object(acceptance.evidence,'load',return_value=archived), \
                 patch.object(acceptance.deadlines,'load',return_value=windows), \
                 patch.object(acceptance.operations,'load',return_value=op), \
                 patch.object(acceptance.confirmation,'protocols',return_value=[old,new]), \
                 patch.object(acceptance.candidate,'inspect',return_value=artifact) as inspect:
                r=acceptance.collect(folder,now=NOW)
            self.assertEqual(r['selected_protocol_id'],new['id'])
            inspect.assert_called_once_with(Path(folder).resolve()/'data/api_football',new)
            self.assertEqual(self.row(r,'candidate')['state'],'attention')
            self.assertFalse(list(Path(folder).iterdir()))

    def test_partial_source_failure_does_not_hide_missing_sources_or_run_actions(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(acceptance.evidence,'load',return_value=sample_inputs()[0]), \
             patch.object(acceptance.deadlines,'load',side_effect=ValueError('private token')), \
             patch.object(acceptance.confirmation,'protocols',return_value=[]), \
             patch.object(acceptance.operations,'load',return_value=sample_inputs()[2]):
            r=acceptance.collect(folder,now=NOW)
        self.assertIn('capture_windows_unavailable',r['source_failures'])
        self.assertEqual(self.row(r,'read_failures')['state'],'unavailable')
        self.assertNotIn('private token',str(r));self.assertFalse(r['api_requested']);self.assertFalse(r['fit_requested'])

    def test_naive_cutoff_rejected_before_any_source_read(self):
        with patch.object(acceptance.evidence,'load') as read:
            with self.assertRaises(ValueError):acceptance.collect(now=NOW.replace(tzinfo=None))
        read.assert_not_called()

    def test_service_order_compares_instants_not_offset_text(self):
        values=sample_inputs();values[2]['cycles']=[
            dict(id='1'*32,origin='service',started_at='2026-11-02T19:00:00+02:00',status='running',signal_state='stale'),
            dict(id='2'*32,origin='service',started_at='2026-11-02T17:30:00+00:00',status='running',signal_state='recent')]
        result=self.build(values)
        self.assertEqual(result['latest_service_cycle']['id'],'2'*32)
        self.assertEqual(self.row(result,'process')['state'],'observed')


if __name__=='__main__':unittest.main()
