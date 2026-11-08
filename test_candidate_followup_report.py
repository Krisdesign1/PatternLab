"""Period observations preserve cohorts, corrections and original engine rules."""
from datetime import timedelta
import copy
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

import api_football as api
import automation_worker as worker
import candidate_followup_report as report
import candidate_tracking as tracking
import journal_uncertainty
import prediction_journal as journal
import test_candidate_tracking as fixtures
from test_candidate_tracking import NOW
from test_prediction_journal import raw_fixtures


class FollowupReportTests(unittest.TestCase):
    setUp=fixtures.CandidateTrackingTests.setUp
    enroll=fixtures.CandidateTrackingTests.enroll
    methods=fixtures.CandidateTrackingTests.methods
    run_cycle=fixtures.CandidateTrackingTests.run_cycle

    def prepare(self):
        study=self.enroll();self.run_cycle()
        return study

    def test_empty_reads_create_nothing_and_no_zero_report_for_unknown_study(self):
        before={p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        value=report.collect(self.root,now=NOW)
        self.assertEqual(value['source_state'],'not_started')
        self.assertEqual(value['studies'],[])
        self.assertEqual(report.snapshots(self.root),[])
        self.assertEqual(report.capture(self.root,now=NOW)['status'],'idle')
        self.assertFalse(report.snapshot_path(self.root).exists())
        with self.assertRaises(ValueError):report.collect(self.root,study_id='f'*64,now=NOW)
        for p,data in before.items():self.assertEqual(p.read_bytes(),data)

    def test_current_period_counts_do_not_multiply_shared_matches_or_turn_pending_into_loss(self):
        study=self.prepare()
        value=report.collect(self.root,study_id=study['id'],now=NOW)['studies'][0]
        self.assertEqual(value['unique_matches'],4)
        self.assertEqual(value['groups'],3)
        self.assertEqual([(r['size'],r['groups'],r['pending'],r['settled']) for r in value['daily']],[(2,2,2,0),(4,1,1,0)])
        self.assertTrue(all(r['before_kickoff']==r['groups'] and r['late']==0 for r in value['daily']))
        self.assertTrue(all(r['eligible_groups']==0 for r in value['weekly']))
        self.assertTrue(all(m['loss'] is None and m['groups']==0 for r in value['weekly'] for m in r['metrics']))
        self.assertEqual({r['week_utc'] for r in value['weekly']},{'2025-03-17'})

    def test_collect_and_capture_never_retrain_fetch_or_reconcile_and_preserve_source_bytes(self):
        self.prepare()
        source=tracking.database_path(self.root)
        before=source.read_bytes()
        with patch.object(journal,'reconcile',side_effect=AssertionError('No reconciliation')), \
             patch.object(tracking,'run_cycle',side_effect=AssertionError('No inference')):
            value=report.collect(self.root,now=NOW)
            result=report.capture(self.root,now=NOW)
        self.assertEqual(result['created'],1)
        self.assertEqual(source.read_bytes(),before)
        self.assertTrue(value['studies'][0]['implementation_compatible'])

    def test_capture_is_idempotent_within_day_and_retains_first_observation_time(self):
        study=self.prepare()
        first=report.capture(self.root,now=NOW)
        saved=report.snapshots(self.root,study['id'])
        with patch.object(report,'collect',side_effect=AssertionError('Unchanged state stays cheap')):
            repeat=report.capture(self.root,now=NOW+timedelta(hours=1))
        self.assertEqual(first['created'],1)
        self.assertEqual(repeat['status'],'unchanged')
        self.assertEqual(saved,report.snapshots(self.root,study['id']))
        self.assertEqual(report.load_snapshot(saved[0]['snapshot_id'],self.root)['as_of'],NOW.isoformat())
        self.assertEqual(report.capture(self.root,now=NOW+timedelta(days=1))['created'],1)
        self.assertEqual(len(report.snapshots(self.root)),2)

    def test_score_revision_creates_new_snapshot_and_does_not_rewrite_the_pending_one(self):
        study=self.prepare();report.capture(self.root,now=NOW)
        old=report.snapshots(self.root)[0]
        pending=report.load_snapshot(old['snapshot_id'],self.root)
        api.save_fixtures(raw_fixtures(status='FT'),self.root/'fixtures.json')
        tracking.reconcile(self.root,now=NOW+timedelta(days=1))
        report.capture(self.root,now=NOW+timedelta(days=1))
        revised=report.snapshots(self.root)[0]
        self.assertNotEqual(old['snapshot_id'],revised['snapshot_id'])
        self.assertNotEqual(old['input_fingerprint'],revised['input_fingerprint'])
        self.assertEqual(pending,report.load_snapshot(old['snapshot_id'],self.root))
        current=report.load_snapshot(revised['snapshot_id'],self.root)
        self.assertTrue(all(r['settled']==r['groups'] for r in current['study']['daily']))
        self.assertEqual(len(current['study']['source_revisions']),3)
        self.assertTrue(all(row[2]==1 for row in current['study']['source_revisions']))

    def test_open_or_incomplete_dependency_weeks_never_become_eligible_after_period_filter(self):
        study=self.prepare()
        raw=raw_fixtures(status='FT')
        raw[0]['fixture']['date']='2025-03-27T12:00:00Z'
        api.save_fixtures(raw,self.root/'fixtures.json')
        # The same fixture connects its original and actual UTC weeks.
        tracking.reconcile(self.root,now=NOW+timedelta(days=8))
        summary=tracking.summary(study['id'],self.root,now=NOW+timedelta(days=8))
        before=report.build(summary,now=NOW+timedelta(days=8))
        self.assertTrue(all(row['calendar_closed'] for row in before['weekly']))
        self.assertTrue(all(row['settled']>0 and row['eligible_groups']==0 for row in before['weekly']))
        # Once the whole dependency component is closed the original week gets
        # its admissible groups; no separate weekly interval was calculated.
        summary=tracking.summary(study['id'],self.root,now=NOW+timedelta(days=12))
        after=report.build(summary,now=NOW+timedelta(days=12))
        self.assertTrue(all(row['eligible_groups']==row['groups'] for row in after['weekly']))

    def test_snapshot_immutability_and_digest_validation(self):
        self.prepare();report.capture(self.root,now=NOW)
        saved=report.snapshots(self.root)[0]
        path=report.snapshot_path(self.root)
        with sqlite3.connect(path) as con:
            with self.assertRaises(sqlite3.IntegrityError):con.execute('DELETE FROM snapshots')
            con.execute('DROP TRIGGER snapshots_no_update')
            con.execute("UPDATE snapshots SET payload='{}'")
        with self.assertRaises(ValueError):report.load_snapshot(saved['snapshot_id'],self.root)
        with self.assertRaises(ValueError):report.snapshots(self.root)
        with self.assertRaises(ValueError):report.capture(self.root,now=NOW)

    def test_retry_samples_observation_time_again_after_a_concurrent_score_revision(self):
        self.prepare()
        original=report._inputs
        calls=0
        def revised(root):
            nonlocal calls
            calls+=1
            if calls==2:
                api.save_fixtures(raw_fixtures(status='FT'),self.root/'fixtures.json')
                tracking.reconcile(self.root,now=NOW+timedelta(days=1))
            return original(root)
        times=iter([NOW,NOW+timedelta(days=1)])
        def clock(value=None):return next(times) if value is None else value
        with patch.object(report,'_inputs',side_effect=revised),patch.object(report,'_now',side_effect=clock):
            observed=report.collect(self.root)
        self.assertEqual(observed['as_of'],(NOW+timedelta(days=1)).isoformat())
        self.assertTrue(all(row['settled']==row['groups'] for row in observed['studies'][0]['daily']))

    def test_capture_utc_day_follows_the_actual_report_if_read_crosses_midnight(self):
        self.prepare()
        before=NOW.replace(hour=23,minute=59,second=59)
        after=before+timedelta(seconds=2)
        times=iter([before,after])
        def clock(value=None):return next(times) if value is None else value
        with patch.object(report,'_now',side_effect=clock):
            report.capture(self.root)
        self.assertEqual(report.snapshots(self.root)[0]['utc_day'],after.date().isoformat())

    def test_changing_source_during_read_is_reported_without_false_empty_counters(self):
        study=self.prepare()
        original=report._inputs
        calls=0
        def moving(root):
            nonlocal calls
            calls+=1
            result=original(root)
            result[study['id']]['forecasts'].append(['moving',str(calls),calls])
            return result
        with patch.object(report,'_inputs',side_effect=moving):
            with self.assertRaisesRegex(ValueError,'évoluent'):
                report.collect(self.root,now=NOW)
        self.assertFalse(report.snapshot_path(self.root).exists())

    def test_worker_observation_failure_does_not_abort_forecasts(self):
        self.prepare()
        for error in (ValueError('unavailable'),sqlite3.OperationalError('locked'),KeyError('missing')):
            value={}
            with self.subTest(error=type(error).__name__),patch.object(report,'capture',side_effect=error):
                worker._candidate_observation(self.root,value,NOW)
            self.assertEqual(value['candidate_observation']['status'],'unavailable')

    def test_code_compatibility_change_is_a_new_observation_without_reregistering_rules(self):
        study=self.prepare();report.capture(self.root,now=NOW)
        original=report.snapshots(self.root)[0]
        rules=tracking.studies(self.root)
        with patch.object(tracking,'_implementation',return_value={}):
            value=report.capture(self.root,now=NOW+timedelta(hours=1))
        self.assertEqual(value['created'],1)
        self.assertEqual(tracking.studies(self.root),rules)
        new=report.load_snapshot(report.snapshots(self.root)[0]['snapshot_id'],self.root)
        self.assertFalse(new['study']['implementation_compatible'])
        old=report.load_snapshot(original['snapshot_id'],self.root)
        self.assertTrue(old['study']['implementation_compatible'])
        self.assertEqual(old['study']['rules_fingerprint'],new['study']['rules_fingerprint'])

    def test_incomplete_source_schema_is_unavailable_and_never_a_zero_observation(self):
        with sqlite3.connect(tracking.database_path(self.root)) as con:con.execute('CREATE TABLE unrelated(x)')
        with self.assertRaisesRegex(ValueError,'incomplète'):report.collect(self.root,now=NOW)
        self.assertFalse(report.snapshot_path(self.root).exists())


if __name__=='__main__':unittest.main()
