"""Operational window evidence with synthetic compact SQLite, no API or fit."""
from copy import deepcopy
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import scheduler_deadlines as deadlines
import causal_confirmation_store as store
from causal_feature_workspace import digest


def seal(value):
    value = deepcopy(value)
    value.pop('id', None)
    return dict(value, id=digest(value))


class DeadlineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)/'data'
        self.protocol = seal(dict(version='causal-confirmation-v1', decision_hour_utc=8,
            capture_grace_minutes=30, calendar_warmup_minutes=30,
            methods=['candidate', 'clubs', 'frequency'], automatic_promotion=False,
            source_visibility_at_T_proven=False, source_time_quality='local_import_recorded',
            synthetic_clock=False, candidate_completed_at='2026-10-30T17:00:00Z',
            implementation=deadlines.confirmation.implementation(),
            registered_at='2026-10-30T18:00:00Z', candidate_id='a'*64,
            confirmation_start='2026-10-31T00:00:00Z', confirmation_end='2026-11-01T00:00:00Z'))
        self.fixture = dict(provider='api-football', match_id='123', league='39', home_id='1', away_id='2',
            home='Home', away='Away', timestamp='2026-10-31T16:00:00')
        self.target = dict(match_id='123', fixture=deepcopy(self.fixture), target_ref={'id':'ref'}, target_raw={'id':123})
        self.anchor = seal(dict(protocol_id=self.protocol['id'], day='2026-10-31',
            decision_at='2026-10-31T08:00:00Z', targets=[self.target],
            read_started_at='2026-10-31T08:00:01Z', read_finished_at='2026-10-31T08:00:02Z',
            contexts={'39': {'context_id':'c'*64}}))
        self.forecast = dict(id='f'*64, protocol_id=self.protocol['id'],
            version=deadlines.confirmation.VERSION,candidate_id=self.protocol['candidate_id'],fixture=deepcopy(self.fixture),
            prepared_at='2026-10-31T08:00:04Z', planned_decision_at='2026-10-31T08:00:00Z',
            snapshot=dict(id='b'*64, fixture=deepcopy(self.fixture), decision_at='2026-10-31T08:00:00Z',
                target_ref={'id':'ref'}, target_observation={'id':123}),
            source_read={'start':'2026-10-31T08:00:01Z','end':'2026-10-31T08:00:02Z'},
            confirmation={'requested_at':'2026-10-31T08:00:02Z','received_at':'2026-10-31T08:00:03Z'})
        self.transaction = '2026-10-31T08:00:05Z'
        self.receipt = seal(dict(version=store.VERSION, forecast_id='f'*64,
            transaction_at_utc=self.transaction, observed_at_utc='2026-10-31T08:00:06Z',
            before_kickoff=True, time_quality='local_commit_ack', admissible=True, reason='saved_before_kickoff'))
        self.registration = self.event('registration_ack','2026-10-30T18:00:01Z',
                                       time_quality='local_registration_ack')
        self.warmup = self.event('calendar_prepared','2026-10-31T07:40:00Z',day='2026-10-31')
        self.closure = self.event('window_closed','2026-10-31T08:35:00Z',day='2026-10-31', missing_match_ids=[])
        self.closure_ack = self.event('window_closed_ack','2026-10-31T08:35:01Z',day='2026-10-31',
            closure_id=self.closure['id'],time_quality='local_closure_commit_ack')
        self.events=[self.registration,self.warmup,self.closure,self.closure_ack]
        self.anchors=[self.anchor]; self.forecasts=[self.forecast]; self.receipts=[self.receipt]
        network=patch.object(deadlines.confirmation.api.ApiFootballClient,'get',side_effect=AssertionError('No API'))
        self.network=network.start(); self.addCleanup(network.stop)
        fit=patch.object(deadlines.confirmation.candidates,'fit_candidate',side_effect=AssertionError('No fit'))
        self.fit=fit.start(); self.addCleanup(fit.stop)

    def event(self,kind,when,**fields):
        return seal(dict(version='causal-confirmation-v1',protocol_id=self.protocol['id'],kind=kind,
                         observed_at_utc=when,**fields))

    def save(self):
        path=store.database_path(self.root);self.root.mkdir(exist_ok=True)
        path.unlink(missing_ok=True)
        with sqlite3.connect(path) as con:
            store._schema(con)
            con.execute('INSERT INTO protocols VALUES(?,?,?)',(self.protocol['id'],store._json(self.protocol),digest(self.protocol)))
            for item in self.anchors:
                con.execute('INSERT INTO anchors VALUES(?,?,?,?)',
                    (self.protocol['id'],item['day'],store._json(item),digest(item)))
            for item in self.events:
                con.execute('INSERT INTO attempts VALUES(?,?,?,?)',
                    (item['id'],self.protocol['id'],store._json(item),digest(item)))
            for item in self.forecasts:
                con.execute('INSERT INTO forecasts VALUES(?,?,?,?,?,?,?)',
                    (item['id'],self.protocol['id'],item['fixture']['match_id'],self.transaction,'c'*64,store._json(item),digest(item)))
            for item in self.receipts:
                con.execute('INSERT INTO receipts VALUES(?,?,?)',(item['forecast_id'],store._json(item),digest(item)))
        return path

    def report(self,when='2026-10-31T09:00:00Z'):
        self.save()
        return deadlines.load(self.root,now=when)

    def test_missing_source_does_not_create(self):
        report=deadlines.load(self.root,now='2026-10-31T09:00:00Z')
        self.assertEqual(report['source_states'],{'confirmation':'missing'})
        self.assertFalse(self.root.exists()); self.assertEqual(report['days'],[])

    def test_verified_closed_window_absolute_counts_and_delay(self):
        report=self.report(); row=report['days'][0]
        self.assertEqual(row['state'],'closed')
        self.assertEqual((row['target_count'],row['capture_count'],row['timing_admissible_count']),(1,1,1))
        self.assertEqual(row['missing_known_targets'],0)
        self.assertEqual(row['closure_delay_seconds'],300)
        self.assertEqual(row['issues'],[])
        self.network.assert_not_called(); self.fit.assert_not_called()

    def test_exact_utc_boundaries_future_records_not_counted(self):
        for when,state,count in [('2026-10-31T07:29:59Z','upcoming',0),
                                 ('2026-10-31T07:30:00Z','warmup',0),
                                 ('2026-10-31T08:00:00Z','open',0),
                                 ('2026-10-31T08:00:05Z','open',1),
                                 ('2026-10-31T08:30:00Z','closed_unverified',1)]:
            with self.subTest(when=when):
                row=self.report(when)['days'][0]
                self.assertEqual(row['state'],state);self.assertEqual(row['capture_count'],count)
                if when=='2026-10-31T08:00:05Z':self.assertEqual(row['timing_admissible_count'],0)

    def test_missing_census_is_unknown_not_zero_and_never_backfilled(self):
        self.anchors=[];self.forecasts=[];self.receipts=[]
        self.events=[self.registration]
        row=self.report()['days'][0]
        self.assertEqual(row['state'],'missed');self.assertIsNone(row['target_count'])
        self.assertIsNone(row['missing_known_targets']);self.assertEqual(row['capture_count'],0)
        self.assertIn('unknown_census',row['issues'])
        with sqlite3.connect(store.database_path(self.root)) as con:
            self.assertEqual(con.execute('SELECT count(*) FROM anchors').fetchone()[0],0)

    def test_missing_receipt_is_not_repaired_or_timing_admissible(self):
        self.receipts=[];row=self.report()['days'][0]
        self.assertEqual(row['timing_admissible_count'],0)
        self.assertIn('capture_timing_unverified',row['issues'])
        self.assertEqual(row['state'],'closed_unverified')

    def test_late_ack_is_saved_but_not_window_admissible(self):
        self.receipts=[seal(dict(self.receipt,observed_at_utc='2026-10-31T08:30:00Z'))]
        row=self.report()['days'][0]
        self.assertEqual(row['timing_admissible_count'],0)
        self.assertEqual(row['last_capture_ack_at'],'2026-10-31T08:30:00+00:00')

    def test_legacy_missing_closure_ack_stays_unverified(self):
        self.events.remove(self.closure_ack);row=self.report()['days'][0]
        self.assertEqual(row['state'],'closed_unverified')
        self.assertIn('closure_timing_unverified',row['issues'])

    def test_unknown_freeform_reason_never_reaches_export(self):
        self.events.append(self.event('new_unrecognized_kind','2026-10-31T08:01:00Z',day='2026-10-31',reason='SECRET-VALUE'))
        report=self.report()
        self.assertNotIn('SECRET-VALUE',json.dumps(report))
        self.assertNotIn('new_unrecognized_kind',json.dumps(report))
        self.assertEqual(report['days'][0]['event_counts']['other'],1)

    def test_hash_stable_across_display_time_and_no_database_writes(self):
        path=self.save();before=path.read_bytes()
        a=deadlines.load(self.root,now='2026-10-31T09:00:00Z')
        b=deadlines.load(self.root,now='2026-10-31T10:00:00Z')
        self.assertEqual(a['source_sha256'],b['source_sha256'])
        self.assertEqual(path.read_bytes(),before)

    def test_compatibility_change_is_explicit_without_changing_registered_windows(self):
        self.save()
        when='2026-10-31T07:00:00Z'
        original=deadlines.load(self.root,now=when)
        self.assertTrue(original['protocols'][0]['implementation_compatible'])
        changed=deepcopy(original['current_implementation'])
        changed['automation_worker.py']='0'*64
        with patch.object(deadlines.confirmation,'implementation',return_value=changed):
            report=deadlines.load(self.root,now=when)
        self.assertFalse(report['protocols'][0]['implementation_compatible'])
        self.assertEqual(report['days'],original['days'])
        self.assertEqual(report['days'][0]['state'],'upcoming')
        self.assertNotEqual(report['source_sha256'],original['source_sha256'])
        self.assertEqual(report['current_implementation'],changed)

    def test_runtime_compatibility_inputs_are_exported_and_fingerprinted(self):
        self.save()
        original=deadlines.load(self.root,now='2026-10-31T09:00:00Z')
        changed=deepcopy(original['current_runtime']);changed['python']='test-version'
        with patch.object(deadlines.confirmation,'_runtime',return_value=changed):
            report=deadlines.load(self.root,now='2026-10-31T09:00:00Z')
        self.assertEqual(report['current_runtime'],changed)
        self.assertNotEqual(report['source_sha256'],original['source_sha256'])
        # The original one-day contract does not pin runtime versions.
        self.assertTrue(report['protocols'][0]['implementation_compatible'])

    def test_followup_runtime_mismatch_blocks_compatibility_and_preserves_days(self):
        from causal_confirmation_followup import make_plan
        plan=make_plan('2026-11-02T00:00:00Z','2026-12-28T00:00:00Z')
        runtime=deadlines.confirmation._runtime()
        self.protocol=seal(dict(self.protocol,followup=plan,runtime=runtime,
            confirmation_start=plan['confirmation_start'],confirmation_end=plan['confirmation_end'],
            candidate_original_confirmation={'start':'2026-10-31T00:00:00Z','end':'2026-11-01T00:00:00Z'}))
        self.events=[seal(dict(self.registration,protocol_id=self.protocol['id']))]
        self.anchors=[];self.forecasts=[];self.receipts=[]
        self.save()
        original=deadlines.load(self.root,now='2026-11-01T09:00:00Z')
        self.assertTrue(original['protocols'][0]['implementation_compatible'])
        changed=deepcopy(runtime);changed['python']='incompatible-test-version'
        with patch.object(deadlines.confirmation,'_runtime',return_value=changed):
            report=deadlines.load(self.root,now='2026-11-01T09:00:00Z')
        self.assertFalse(report['protocols'][0]['implementation_compatible'])
        self.assertEqual(report['days'],original['days'])
        self.assertEqual(len(report['days']),56)
        self.assertEqual(set(row['state'] for row in report['days']),{'upcoming'})

    def test_foreign_capture_and_changed_target_fail_closed(self):
        for field,value in [('home_id','99'),('timestamp','2026-10-31T17:00:00'),('home','Other')]:
            with self.subTest(field=field):
                self.anchor=deepcopy(self.anchors[0]);self.anchor['targets'][0]['fixture'][field]=value
                self.anchors=[seal(self.anchor)]
                with self.assertRaises(deadlines.DeadlineError):self.report()
                self.anchors=[deepcopy(self.anchor)]

    def test_whitespace_canonicalization_matches_snapshot_writer(self):
        self.anchor['targets'][0]['fixture']['home']=' Home '
        self.anchors=[seal(self.anchor)]
        self.assertEqual(self.report()['days'][0]['state'],'closed')

    def test_changed_digest_and_unknown_schema_are_sanitized(self):
        path=self.save()
        with sqlite3.connect(path) as con:
            con.execute('DROP TRIGGER confirmation_attempts_no_update')
            con.execute("UPDATE attempts SET payload='SECRET-VALUE'")
        with self.assertRaisesRegex(deadlines.DeadlineError,'indisponible ou incohérent'):
            deadlines.load(self.root)
        path.unlink()
        with sqlite3.connect(path) as con:con.execute('CREATE TABLE other(x)')
        with self.assertRaises(deadlines.DeadlineError):deadlines.load(self.root)

    def test_naive_clock_rejected(self):
        with self.assertRaises(deadlines.DeadlineError):deadlines.load(self.root,now='2026-10-31T09:00:00')

    def test_registration_ack_and_closure_ack_proofs_are_separate(self):
        self.events.remove(self.registration)
        row=self.report()['days'][0]
        self.assertFalse(row['registration_ready']);self.assertEqual(row['timing_admissible_count'],0)
        self.assertEqual(row['state'],'closed_unverified')
        self.events=[self.registration,self.warmup,self.closure_ack]
        with self.assertRaises(deadlines.DeadlineError):self.report()

    def test_closure_target_inventory_must_match_saved_captures(self):
        changed=seal(dict(self.closure,missing_match_ids=['123']))
        self.events=[self.registration,self.warmup,changed]
        with self.assertRaises(deadlines.DeadlineError):self.report()

    def test_late_empty_census_stays_known_but_unqualified(self):
        self.anchor['targets']=[]
        self.anchor['read_finished_at']='2026-10-31T08:30:00Z'
        self.anchors=[seal(self.anchor)];self.forecasts=[];self.receipts=[]
        row=self.report()['days'][0]
        self.assertEqual(row['target_count'],0)
        self.assertEqual(row['state'],'closed_unverified')
        self.assertIn('census_timing_unverified',row['issues'])

    def test_resealed_forecast_candidate_version_and_source_interval_must_match(self):
        original=deepcopy(self.forecast)
        for field,value in [('candidate_id','9'*64),('version','unknown-version'),
                            ('source_read',{'start':'2026-10-31T08:00:00Z','end':'2026-10-31T08:00:02Z'}),
                            ('source_read',{'start':'2026-10-31T08:00:01Z','end':'2026-10-31T08:00:01Z'})]:
            with self.subTest(field=field,value=value):
                changed=deepcopy(original);changed[field]=value;self.forecasts=[changed]
                with self.assertRaises(deadlines.DeadlineError):self.report()


if __name__=='__main__':
    unittest.main()
