"""Prospective individual tracking: synthetic fixtures only, no network or fit."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import api_match_workspace as calendar
import daily_learning_models as models
import daily_learning_tracking as tracking
from test_high_probability_engine import fixture


NOW = datetime(2026, 10, 22, 9, tzinfo=timezone.utc)
KICKOFF = '2026-10-22T15:00:00+00:00'
P = {'1st': .25, 'Égal': .25, '2nd': .5}


class DailyLearningTrackingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)/'daily'
        self.protocol = dict(id='protocol-test', version='synthetic',
            created_at=(NOW-timedelta(days=10)).isoformat())
        self.manifest = dict(candidate_id='candidate-1', protocol_id=self.protocol['id'],
            status='completed', league='61', completed_at=(NOW-timedelta(minutes=1)).isoformat(),
            cutoff_exclusive=NOW.date().isoformat(), trained_through='2026-10-21',
            model_sha256='a'*64, params={'depth': 3}, history_fingerprint='b'*64,
            history_matches=90, incumbent_generation=None)
        self.raw = [fixture(100001, stamp=KICKOFF), fixture(100002, stamp=KICKOFF)]
        self.report = dict(before=NOW.date().isoformat(), rows=[dict(
            fixture=calendar.fixture_from_raw(raw), probabilities={'1st': .2, 'Égal': .3, '2nd': .5},
            baseline={'1st': .3, 'Égal': .3, '2nd': .4}, before=NOW.date().isoformat()) for raw in self.raw])
        self.predictions = patch.object(models, 'predict', side_effect=lambda m,r,f,**kw: [dict(P) for _ in f]).start()
        self.incumbent = patch.object(models, 'predict_incumbent', return_value=None).start()
        patch.object(models, 'load_protocol', side_effect=lambda root: deepcopy(self.protocol)).start()
        patch.object(tracking.cloud_storage, 'configured', return_value=False).start()
        patch.object(tracking.cloud_storage, '_assert_writable').start()
        self.addCleanup(patch.stopall)

    def capture(self, report=None, *, clock=None, refresh=True, candidates=None):
        fn = (lambda **kw: deepcopy(self.raw)) if refresh is True else refresh
        return tracking.capture(self.report if report is None else report,
            {'61': self.manifest} if candidates is None else candidates, [], self.root,
            clock=clock or (lambda: NOW), refresh=fn)

    def entries(self, now=NOW):
        return tracking.entries(self.root, now=now)

    def reconcile(self, raw, now=NOW+timedelta(hours=10)):
        return tracking.reconcile(self.root, raw_fixtures=raw, now=now)

    def test_readonly_empty_summary_does_not_create_storage(self):
        self.assertEqual(tracking.signature(self.root), ())
        self.assertEqual(self.entries(), [])
        report=tracking.summary(self.root, now=NOW)
        self.assertEqual(report['counts']['matches'], 0)
        self.assertEqual(report['metrics']['candidate']['matches'], 0)
        self.assertFalse(report['promotion'])
        self.assertFalse(self.root.exists())
        self.assertEqual(self.reconcile([])['changed'], 0)

    def test_capture_is_first_per_procedure_not_daily_generation(self):
        with patch.object(api.ApiFootballClient, 'get', side_effect=AssertionError('No network')):
            self.assertEqual(self.capture()['created'], 2)
        original=self.entries()
        self.assertEqual(original[0]['methods']['candidate'], P)
        self.assertEqual(set(original[0]['methods']), {'candidate', 'clubs', 'frequency'})
        self.manifest.update(candidate_id='candidate-2', model_sha256='c'*64)
        self.report['rows'][0]['probabilities']={'1st': .8, 'Égal': .1, '2nd': .1}
        result=self.capture(clock=lambda: NOW+timedelta(minutes=1))
        self.assertEqual((result['created'],result['existing']), (0,2))
        self.assertEqual(self.entries(), original)
        self.assertEqual(self.predictions.call_count, 1)

    def test_no_capture_without_fresh_provider_confirmation(self):
        result=self.capture(refresh=None)
        self.assertEqual(result['created'], 0)
        self.assertEqual(result['unavailable'], 2)
        self.assertFalse(self.root.exists())
        for change in (
            lambda raw: raw['fixture']['status'].update(short='PST'),
            lambda raw: raw['fixture'].update(date='2026-10-22T16:00:00+00:00'),
            lambda raw: raw['teams']['home'].update(id=999),
            lambda raw: raw['league'].update(season=2025),
            lambda raw: raw.update(provider='other'),
        ):
            with self.subTest(change=change):
                raw=deepcopy(self.raw)
                for item in raw: change(item)
                self.assertEqual(self.capture(refresh=lambda **kw:raw)['created'], 0)
        self.assertFalse(self.root.exists())

    def test_duplicate_contradictory_scan_and_provider_payloads_are_rejected(self):
        self.report['rows']=self.report['rows'][:1]
        other=deepcopy(self.report['rows'][0])
        other['probabilities']={'1st': .5,'Égal': .3,'2nd': .2}
        self.report['rows'].append(other)
        self.assertEqual(self.capture()['created'], 0)
        self.report['rows']=self.report['rows'][:1]
        altered=deepcopy(self.raw[0]);altered['fixture']['status']['short']='PST'
        self.assertEqual(self.capture(refresh=lambda **kw:[self.raw[0],altered])['created'], 0)
        self.assertEqual(self.capture(refresh=lambda **kw:[])['created'], 0)

    def test_current_and_target_day_cutoffs_are_strict(self):
        for change in (
            lambda: self.manifest.update(trained_through='2026-10-22'),
            lambda: self.manifest.update(cutoff_exclusive='2026-10-23'),
            lambda: self.manifest.update(completed_at=(NOW+timedelta(seconds=1)).isoformat()),
        ):
            original=deepcopy(self.manifest)
            change()
            self.assertEqual(self.capture()['created'], 0)
            self.manifest=original
        self.report['rows'][0]['before']='2026-10-23'
        with self.assertRaises(tracking.TrackingError):
            self.capture()
        self.assertEqual(self.entries(), [])

    def test_scores_of_target_are_not_inputs_and_forecasts_are_not_mutated(self):
        original=deepcopy(self.report)
        for row in self.report['rows']:
            row['fixture'].update(score={'halftime':{'home':99}},goals={'away':99},value='1st')
        self.capture()
        arguments=self.predictions.call_args.args[2]
        for value in arguments+self.entries():
            payload=value.get('fixture',value)
            self.assertNotIn('score',payload)
            self.assertNotIn('goals',payload)
            self.assertNotIn('value',payload)
        self.assertEqual(self.report['rows'][0]['probabilities'],original['rows'][0]['probabilities'])

    def test_fresh_check_batching_has_no_population_cap(self):
        self.raw=[fixture(200000+i, stamp=KICKOFF) for i in range(47)]
        self.report['rows']=[dict(self.report['rows'][0],fixture=calendar.fixture_from_raw(raw)) for raw in self.raw]
        batches=[]
        def refresh(**kwargs):
            ids=kwargs['ids'].split('-');batches.append(ids)
            self.assertEqual(kwargs['timezone'],'UTC')
            return [raw for raw in self.raw if str(raw['fixture']['id']) in ids]
        self.assertEqual(self.capture(refresh=refresh)['created'],47)
        self.assertEqual(list(map(len,batches)),[20,20,7])

    def test_tomorrow_capture_uses_actual_capture_date_and_day_boundary(self):
        self.raw=[fixture(100001, stamp='2026-10-23T01:00:00+00:00')]
        self.report['rows']=self.report['rows'][:1]
        self.report['rows'][0]['fixture']=calendar.fixture_from_raw(self.raw[0])
        self.assertEqual(self.capture()['created'],1)
        row=self.entries()[0]
        self.assertEqual(row['captured_at'],NOW.isoformat())
        self.assertEqual(row['before'],'2026-10-22')
        self.assertEqual(row['day'],'2026-10-23')

    def test_clock_after_kickoff_cannot_capture_or_commit(self):
        self.assertEqual(self.capture(clock=lambda: NOW+timedelta(hours=7))['created'],0)
        calls=[]
        def advancing_clock():
            calls.append(1)
            # Two confirmations, two insert clocks, then final transaction clock.
            return NOW if len(calls)<7 else NOW+timedelta(hours=7)
        with self.assertRaises(tracking.TrackingError):
            self.capture(clock=advancing_clock)
        self.assertEqual(self.entries(),[])

    def test_incumbent_failure_does_not_remove_primary_common_cohort(self):
        self.incumbent.side_effect=ValueError('Pinned history changed')
        self.assertEqual(self.capture()['created'],2)
        self.assertNotIn('incumbent',self.entries()[0]['methods'])
        self.assertEqual(self.entries()[0]['incumbent_unavailable'],'Pinned history changed')

    def test_frozen_protocol_forecast_and_revisions_reject_updates_and_deletes(self):
        self.capture()
        self.reconcile([fixture(100001,status='FT'),fixture(100002,status='FT')])
        with sqlite3.connect(tracking.database_path(self.root)) as con:
            for table in ('protocols','forecasts','results'):
                for sql in (f'UPDATE {table} SET payload=payload',f'DELETE FROM {table}'):
                    with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                        con.execute(sql)

    def test_completed_scores_are_regulation_scores_even_after_extra_time(self):
        self.capture()
        raw=fixture(100001,status='AET',ht=(0,0),ft=(1,0))
        raw['score']['extratime']={'home':1,'away':4}
        result=self.reconcile([raw,fixture(100002,status='FT',ht=(1,0),ft=(1,0))])
        self.assertEqual(result['settled'],2)
        rows=self.entries(NOW+timedelta(hours=10))
        self.assertEqual([row['actual'] for row in rows],['2nd','1st'])
        self.assertEqual(rows[0]['ft'],'1-0')
        self.assertEqual(tracking.summary(self.root,now=NOW+timedelta(hours=10))['metrics']['candidate']['correct'],1)

    def test_missing_half_time_postponed_cancelled_and_old_ns_are_not_losses(self):
        self.capture()
        missing=fixture(100001,status='FT');missing['score']['halftime']={'home':None,'away':None}
        self.reconcile([missing,fixture(100002,status='PST')])
        report=tracking.summary(self.root,now=NOW+timedelta(hours=10))
        self.assertEqual(report['counts']['unresolved'],2)
        self.assertEqual(report['evaluated'],0)
        self.reconcile([fixture(100001,status='CANC'),fixture(100002,status='NS')],now=NOW+timedelta(hours=15))
        report=tracking.summary(self.root,now=NOW+timedelta(hours=15))
        self.assertEqual((report['counts']['void'],report['counts']['unresolved']),(1,1))
        self.assertEqual(report['metrics']['candidate']['matches'],0)

    def test_local_reconcile_reads_only_tracked_fixture_ids(self):
        self.capture()
        with patch.object(api,'load_fixtures',return_value=self.raw) as read:
            tracking.reconcile(self.root,now=NOW+timedelta(minutes=1))
        read.assert_called_once_with(self.root.resolve()/'fixtures.json',ids=['100001','100002'])

    def test_result_corrections_append_and_asof_excludes_later_observations(self):
        self.capture()
        t1=NOW+timedelta(hours=9);t2=t1+timedelta(hours=1)
        first=[fixture(100001,status='FT',ht=(0,0),ft=(1,0)),fixture(100002,status='FT')]
        self.assertEqual(self.reconcile(first,t1)['changed'],2)
        self.assertEqual(self.reconcile(first,t2)['changed'],0)
        corrected=deepcopy(first);corrected[0]['score']['halftime']={'home':1,'away':0}
        self.assertEqual(self.reconcile(corrected,t2)['changed'],1)
        self.assertEqual(self.entries(t1)[0]['actual'],'2nd')
        self.assertEqual(self.entries(t2)[0]['actual'],'1st')
        self.assertEqual(len(self.entries(t2)[0]['result_history']),2)
        self.assertEqual(self.entries()[0]['status'],'pending')

    def test_identity_and_schedule_changes_are_explicit_and_safe(self):
        self.capture()
        raw=fixture(100001,status='FT');raw['teams']['home']['id']=999
        self.reconcile([raw,fixture(100002,status='FT',stamp='2026-10-22T16:00:00+00:00')])
        rows=self.entries(NOW+timedelta(hours=10))
        self.assertEqual(rows[0]['status'],'unresolved')
        self.assertEqual(rows[1]['status'],'settled')
        self.assertEqual(rows[1]['schedule_resolution']['effective_timestamp'],'2026-10-22T16:00:00')
        self.reconcile([fixture(100001,status='FT',stamp='2026-10-22T08:00:00+00:00')])
        self.assertEqual(self.entries(NOW+timedelta(hours=10))[0]['status'],'unresolved')

    def test_summary_uses_identical_settled_cohort_and_separate_incumbent_subset(self):
        self.report['rows']=self.report['rows'][:1]
        self.incumbent.return_value=[{'1st':.6,'Égal':.2,'2nd':.2}]
        self.capture()
        self.report['rows']=[dict(self.report['rows'][0],fixture=calendar.fixture_from_raw(self.raw[1]))]
        self.incumbent.return_value=None
        self.capture()
        self.reconcile([fixture(100001,status='FT',ht=(1,0),ft=(1,0)),fixture(100002,status='NS')])
        report=tracking.summary(self.root,now=NOW+timedelta(hours=10))
        self.assertEqual(report['evaluated'],1)
        self.assertTrue(all(report['metrics'][m]['matches']==1 for m in tracking.METHODS))
        self.assertEqual(report['comparisons']['candidate_vs_clubs']['matches'],1)
        self.assertEqual(report['incumbent']['matches'],1)
        self.assertEqual(report['incumbent']['metrics']['incumbent']['accuracy'],1.)
        self.assertEqual(report['thresholds'][0]['matches'],1)
        self.assertEqual(report['thresholds'][1]['matches'],0)
        json.dumps(report,allow_nan=False)

    def test_utc_week_is_closed_only_when_all_observations_resolved_and_no_boundary(self):
        self.capture()
        next_monday=datetime(2026,10,26,tzinfo=timezone.utc)
        self.reconcile([fixture(100001,status='FT'),fixture(100002,status='PST')])
        report=tracking.summary(self.root,now=next_monday)
        self.assertTrue(report['weekly'][0]['calendar_closed'])
        self.assertFalse(report['weekly'][0]['admissible'])
        self.reconcile([fixture(100001,status='FT'),fixture(100002,status='CANC')],now=next_monday)
        self.assertEqual(tracking.summary(self.root,now=next_monday)['closed_weeks'],1)


if __name__ == '__main__':
    unittest.main()
