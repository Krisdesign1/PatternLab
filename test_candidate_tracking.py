"""Prospective shadow forecasts stay causal, paired, pinned and append-only."""
from datetime import datetime, timedelta, timezone
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import automation_worker as worker
import candidate_tracking as tracking
import catboost_scenarios as ml
import cloud_storage
import model_reassessment as review
import prediction_journal as journal
import prospective_models as models
from test_catboost_scenarios import sample, fixtures
from test_model_campaign import fake_train
from test_prediction_journal import raw_fixtures

NOW = datetime(2025,3,20,9,tzinfo=timezone.utc)


class CandidateTrackingTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(cloud_storage,'configured',return_value=False))
        self.enterContext(patch.object(api,'ApiFootballClient',side_effect=AssertionError('No hidden API')))
        self.rows = sample()
        self.future = raw_fixtures()
        api.save_fixtures(self.future, self.root/'fixtures.json')
        self.run_id = 'a'*64
        native_dir = review._run_path('39',self.run_id,self.root)/'candidate_models'
        self.native = fake_train(self.rows,'39','API-Football',native_dir,before='2025-03-19')
        self.incumbent = fake_train(self.rows,'39','API-Football',self.root/'models',before='2025-03-15')
        self.item = dict(status='completed', source='API-Football', league='39',run_id=self.run_id,
            frozen_at='2025-03-19T09:00:00+00:00',sizes=[2,4],
            candidate_generation=self.native['deployment']['generation'],
            incumbent_generation=self.incumbent['deployment']['generation'],
            report=dict(native=self.native,policy=dict(evaluation_procedure='native')))
        self.enterContext(patch.object(review,'load_run',return_value=self.item))
        self.enterContext(patch.object(api,'load_matches',return_value=(self.rows,{})))

    def enroll(self):
        return tracking.register('39',self.run_id,self.root,now=NOW)

    def methods(self, rows, chosen, study, root):
        values = tracking._baselines(rows,chosen)
        for name in ('candidate','incumbent'):
            values[name] = dict(copy.deepcopy(values['clubs']),version=ml.VERSION,
                                generation=study[name+'_generation'])
        return values

    def run_cycle(self, clock=None, refresh=None):
        def fresh(**params):
            ids=params['ids'].split('-')
            return [r for r in self.future if str(r['fixture']['id']) in ids]
        with patch.object(tracking,'_methods',side_effect=self.methods):
            return tracking.run_cycle(self.root,clock=clock or (lambda:NOW),refresh=refresh or fresh)

    def test_read_only_empty_views_create_nothing(self):
        path = tracking.database_path(self.root)
        self.assertEqual(tracking.studies(self.root),[])
        self.assertFalse(path.exists())

    def test_enrollment_pins_versions_and_original_state_stays_identical(self):
        original = {p:p.read_bytes() for p in (self.root/'models').rglob('*') if p.is_file()}
        first = self.enroll()
        second = tracking.register('39',self.run_id,self.root,now=NOW+timedelta(hours=1))
        self.assertEqual(first,second)
        self.assertEqual(len(tracking.studies(self.root)),1)
        for p,data in original.items(): self.assertEqual(p.read_bytes(),data)
        for name in ('candidate','incumbent'):
            loaded = ml.load_report('API-Football','39',self.root/'candidate_studies'/first['id']/name)
            self.assertEqual(loaded['deployment']['generation'],first[name+'_generation'])
        with sqlite3.connect(tracking.database_path(self.root)) as con:
            with self.assertRaises(sqlite3.IntegrityError): con.execute("UPDATE studies SET payload='{}'")

    def test_old_procedure_and_changed_incumbent_cannot_enroll(self):
        self.item['report']['policy']['evaluation_procedure']='legacy'
        with self.assertRaisesRegex(ValueError,'alignée'): self.enroll()
        self.item['report']['policy']['evaluation_procedure']='native'
        self.item['incumbent_generation']='0'*64
        with self.assertRaisesRegex(ValueError,'changé'): self.enroll()
        self.assertEqual(tracking.studies(self.root),[])

    def test_daily_forecasts_all_four_methods_same_groups_idempotent(self):
        study = self.enroll()
        first=self.run_cycle()
        self.assertEqual(first['created'],3,first)
        entries=journal.list_entries(tracking.database_path(self.root))
        self.assertEqual({e['size'] for e in entries},{2,4})
        self.assertTrue(all(set(e['methods'])==set(tracking.METHODS) for e in entries))
        self.assertTrue(all(journal._stamp(e['created_at'])<journal._stamp(e['fixtures'][0]['timestamp']) for e in entries))
        second=self.run_cycle()
        self.assertEqual(second['created'],0)
        self.assertEqual(second['existing'],3)
        self.assertEqual(entries,journal.list_entries(tracking.database_path(self.root)))
        summary=tracking.summary(study['id'],self.root,now=NOW)
        self.assertEqual(summary['unique_matches'],4)
        self.assertFalse(summary['promoted'])
        self.assertEqual([s['counts'] for s in summary['sizes']],[{'pending':2},{'pending':1}])

    def test_after_compute_clock_crossing_kickoff_refuses_commit(self):
        self.enroll()
        def refresh(**params): return self.future
        # The explicit commit checks reject a calculation that finishes late.
        study=tracking.studies(self.root)[0]
        chosen=fixtures()
        methods=self.methods(self.rows,chosen,study,self.root)
        values=iter([NOW,datetime(2025,3,20,12,tzinfo=timezone.utc)])
        with self.assertRaises(journal.JournalError):
            tracking._record(study,chosen,methods,self.future,self.root,lambda:next(values))
        self.assertEqual(journal.list_entries(tracking.database_path(self.root)),[])

    def test_live_changed_identity_or_missing_response_no_forecast(self):
        self.enroll()
        study=tracking.studies(self.root)[0]
        chosen=fixtures()
        methods=self.methods(self.rows,chosen,study,self.root)
        for kind in ('team','live','missing','time'):
            raw=copy.deepcopy(self.future)
            if kind=='team': raw[0]['teams']['home']['id']=99999
            elif kind=='live': raw[0]['fixture']['status']['short']='1H'
            elif kind=='missing': raw.pop()
            else: raw[0]['fixture']['date']='2025-03-20T17:00:00Z'
            with self.subTest(kind=kind),self.assertRaises((ValueError,KeyError)):
                tracking._record(study,chosen,methods,raw,self.root,lambda:NOW)
        self.assertEqual(journal.list_entries(tracking.database_path(self.root)),[])

    def test_results_reconcile_without_model_or_history_and_corrections_append(self):
        study=self.enroll();self.run_cycle()
        before=journal.list_entries(tracking.database_path(self.root))
        raw=raw_fixtures(status='FT')
        api.save_fixtures(raw,self.root/'fixtures.json')
        with patch.object(tracking,'_methods',side_effect=AssertionError('No later inference')):
            result=tracking.reconcile(self.root,now=NOW+timedelta(days=1))
        self.assertEqual(result['settled'],3)
        summary=tracking.summary(study['id'],self.root,now=NOW+timedelta(days=1))
        for value in summary['sizes']:
            self.assertTrue(all(m['groups']>0 for m in value['metrics']))
            self.assertTrue(all(c['blocks']==0 for c in value['evidence']['comparisons']))
            self.assertTrue(all(m['matches']==4 for m in value['calibration'].values()))
            self.assertTrue(all(sum(b['count'] for b in m['reliability']['1st'])==4
                                for m in value['calibration'].values()))
        raw[0]['score']['halftime']=dict(home=0,away=0)
        api.save_fixtures(raw,self.root/'fixtures.json')
        tracking.reconcile(self.root,now=NOW+timedelta(days=2))
        after=journal.list_entries(tracking.database_path(self.root))
        self.assertEqual([e['methods'] for e in before],[e['methods'] for e in after])
        self.assertEqual(after[0]['result_history'][0]['revision'],1)

    def test_code_change_stops_new_forecasts_and_pending_is_not_failure(self):
        study=self.enroll()
        with patch.object(tracking,'_implementation',return_value={}):
            with self.assertRaisesRegex(ValueError,'code'):
                tracking._methods(self.rows,fixtures(),study,self.root)
        self.run_cycle()
        raw=raw_fixtures(status='PST');api.save_fixtures(raw,self.root/'fixtures.json')
        tracking.reconcile(self.root,now=NOW+timedelta(days=1))
        summary=tracking.summary(study['id'],self.root,now=NOW+timedelta(days=9))
        self.assertTrue(all(m['groups']==0 for s in summary['sizes'] for m in s['metrics']))
        self.assertTrue(all(c['blocks']==0 for s in summary['sizes'] for c in s['evidence']['comparisons']))

    def test_day_anchor_survives_restart_and_late_added_fixture_never_reshuffles(self):
        study=self.enroll();self.run_cycle()
        anchored=tracking._anchors(study,self.future,'2025-03-20',self.root)
        new=copy.deepcopy(self.future[0]);new['fixture']['id']=99999;new['fixture']['date']='2025-03-20T11:00:00Z'
        self.assertEqual(anchored,tracking._anchors(study,[new]+self.future,'2025-03-20',self.root))

    def test_worker_results_queue_includes_shadow_only_matches(self):
        self.enroll();self.run_cycle()
        class Gate:
            state={}
            def call(owner,**params):
                owner.ids=params['ids'].split('-')
                return raw_fixtures(status='FT')
            def save(owner): pass
        gate=Gate()
        worker._refresh_results(self.root,gate,lambda:NOW+timedelta(days=1),{})
        self.assertEqual(set(gate.ids),{'9000','9001','9002','9003'})

    def test_resumed_history_queue_cannot_precede_first_shadow_pass(self):
        from test_automation_worker import Client
        import daily_analysis_worker
        (self.root/'automation_state.json').write_text(json.dumps(dict(version=worker.VERSION,next_phase='history')))
        order=[]
        with patch.object(tracking,'run_cycle',side_effect=lambda *a,**k:order.append('shadow') or {'status':'completed'}), \
             patch.object(worker,'_history',side_effect=lambda *a,**k:order.append('history')), \
             patch.object(daily_analysis_worker,'run',return_value=dict(status='completed',created=0)):
            result=worker.run_cycle(root=self.root,client=Client(self.future),now=NOW,
                config=dict(worker.DEFAULT_CONFIG,sizes=[4],history_enabled=False,models_enabled=False))
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(order[0],'shadow')
        self.assertGreater(order.index('history'),order.index('shadow'))


if __name__=='__main__': unittest.main()
