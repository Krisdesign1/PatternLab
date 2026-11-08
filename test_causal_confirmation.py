from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import causal_confirmation as confirmation
import causal_confirmation_store as store
import fixture_store
import test_causal_feature_workspace as helpers


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.path=self.root/'fixtures.json'
        self.source=self.root/'source_candidate';self.source.mkdir()
        for name in ('candidate.cbm','report.json','COMPLETE.json'):(self.source/name).write_text('synthetic')
        self.candidate=dict(report=dict(id='a'*64,completed_at='2026-10-30T17:00:00Z',
            plan=dict(windows=dict(confirmation=dict(start='2026-10-31T00:00:00+00:00',end='2026-11-01T00:00:00+00:00')))),
            package=dict(id='b'*64,files={'candidate.cbm':'c'*64,'report.json':'d'*64}),model=None)
        load=patch.object(confirmation.candidates,'load_candidate',return_value=self.candidate)
        self.loader=load.start();self.addCleanup(load.stop)
        predict=patch.object(confirmation.candidates,'predict_candidate',side_effect=lambda model,rows:[{'1st':.2,'Égal':.3,'2nd':.5} for _ in rows])
        self.predict=predict.start();self.addCleanup(predict.stop)
        network=patch.object(api.ApiFootballClient,'get',side_effect=AssertionError('No API client'))
        self.network=network.start();self.addCleanup(network.stop)
        self.raw=[self.target(100001),self.target(100002)]
        self.save(helpers.history()+self.raw,'2026-10-30T12:00:00+00:00')
        fixture_store.migrate_json(self.path)
        self.protocol=confirmation.register(self.source,root=self.root,clock=self.clock('2026-10-30T18:00:00Z'))

    def target(self,mid,status='NS'):
        value=helpers.target(mid,status=status)
        value['fixture']['date']='2026-10-31T16:00:00+00:00'
        return value

    @staticmethod
    def clock(stamp):
        return lambda:datetime.fromisoformat(stamp.replace('Z','+00:00'))

    def save(self,values,stamp):
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return datetime.fromisoformat(stamp).astimezone(tz)
        with patch.object(api,'datetime',Clock),patch.object(fixture_store,'datetime',Clock):
            api.save_fixtures(values,self.path)

    def capture(self,**kwargs):
        return confirmation.capture_due(root=self.root,clock=self.clock('2026-10-31T08:01:00Z'),
            refresh=kwargs.pop('refresh',lambda **params:self.raw),**kwargs)

    def test_registration_pins_candidate_is_idempotent_and_never_fits(self):
        with patch.object(confirmation.candidates,'fit_candidate',side_effect=AssertionError('No fit')):
            again=confirmation.register(self.source,root=self.root,clock=self.clock('2026-10-30T19:00:00Z'))
        self.assertEqual(again,self.protocol)
        self.assertEqual(len(confirmation.protocols(self.root)),1)
        self.assertTrue((self.root/'causal_confirmation'/'candidates'/('a'*64)/'candidate.cbm').exists())
        self.network.assert_not_called()

    def test_interrupted_registration_uses_a_new_receipt_instead_of_backdating(self):
        other=self.root/'other'
        with patch.object(confirmation,'_attempt',side_effect=InterruptedError('Synthetic interruption')):
            with self.assertRaises(InterruptedError):
                confirmation.register(self.source,root=other,clock=self.clock('2026-10-30T18:00:00Z'))
        saved=confirmation.register(self.source,root=other,clock=self.clock('2026-10-30T19:00:00Z'))
        events=store.attempts(saved['id'],root=other)
        self.assertEqual(events[0]['observed_at_utc'],'2026-10-30T19:00:00+00:00')
        self.assertEqual(events[0]['time_quality'],'synthetic_unattested')

    def test_before_decision_and_after_deadline_never_backfill_or_call_provider(self):
        for stamp in ('2026-10-31T07:20:00Z','2026-10-31T08:30:00Z'):
            with patch.object(confirmation,'_anchor',side_effect=AssertionError('No late reconstruction')), \
                    patch.object(self,'capture'):
                refresh=lambda **params:(_ for _ in ()).throw(AssertionError('No API'))
                confirmation.capture_due(root=self.root,clock=self.clock(stamp),refresh=refresh)
        self.assertFalse(store.forecasts(root=self.root))
        self.assertIsNone(store.anchor(self.protocol['id'],'2026-10-31',root=self.root))
        self.assertTrue(any(row['kind']=='window_missed' for row in store.attempts(self.protocol['id'],root=self.root)))

    def test_utc_calendar_warmup_is_once_before_decision_without_any_forecast(self):
        from unittest.mock import Mock
        refresh=Mock(return_value=self.raw)
        for _ in range(2):
            confirmation.capture_due(root=self.root,clock=self.clock('2026-10-31T07:45:00Z'),refresh=refresh)
        refresh.assert_called_once_with(date='2026-10-31',timezone='UTC')
        self.assertFalse(store.forecasts(root=self.root));self.predict.assert_not_called()
        self.assertTrue(any(row['kind']=='calendar_prepared' for row in store.attempts(self.protocol['id'],root=self.root)))

    def test_real_directory_ignores_injected_clock(self):
        stamp=confirmation._now(api.DATA_DIR,self.clock('2099-01-01T00:00:00Z'))
        self.assertLess(abs((stamp-datetime.now(timezone.utc)).total_seconds()),2)

    def test_first_capture_only_and_synthetic_clock_never_attests_prospective_timing(self):
        first=self.capture();self.assertEqual(first['created'],2)
        self.assertEqual(self.capture()['created'],0)
        rows=store.forecasts(root=self.root)
        self.assertEqual(len(rows),2)
        self.assertTrue(all(not row['ack']['admissible'] and row['ack']['before_kickoff'] for row in rows))
        view=confirmation.report(self.root)['protocols'][0]
        self.assertEqual(view['admissible'],0);self.assertFalse(view['metrics'])
        self.assertTrue(all(row['status']=='unattested_capture' for row in view['rows']))

    def test_anchor_freezes_targets_and_histories_across_later_imports(self):
        def failure(**params):raise api.ApiFootballError('Quota synthetic')
        with self.assertRaises(api.ApiFootballError):self.capture(refresh=failure)
        anchor=store.anchor(self.protocol['id'],'2026-10-31',root=self.root)
        snapshots=[confirmation._snapshot(anchor,row,self.root) for row in anchor['targets']]
        changed=deepcopy(helpers.history()[0]);changed['score']['halftime']={'home':5,'away':0};changed['score']['fulltime']={'home':5,'away':0}
        self.save([changed,self.target(100003)],'2026-10-31T08:02:00+00:00')
        self.capture(refresh=lambda **params:self.raw+[self.target(100003)])
        saved=store.forecasts(root=self.root)
        self.assertEqual({row['payload']['fixture']['match_id'] for row in saved},{'100001','100002'})
        self.assertEqual({row['payload']['snapshot']['id'] for row in saved},{row['id'] for row in snapshots})
        self.assertTrue(any(row['kind']=='provider_unavailable' for row in store.attempts(self.protocol['id'],root=self.root)))

    def test_source_failure_is_not_an_empty_successful_population(self):
        with patch.object(confirmation.temporal,'state_as_of',side_effect=ValueError('Source unavailable')):
            result=self.capture()
        self.assertTrue(result['issues'])
        self.assertIsNone(store.anchor(self.protocol['id'],'2026-10-31',root=self.root))
        self.assertFalse(store.forecasts(root=self.root))

    def test_changed_provider_status_or_identity_is_not_captured(self):
        changed=deepcopy(self.raw)
        changed[0]['fixture']['status']['short']='PST'
        changed[1]['teams']['away']['id']=999
        self.assertEqual(self.capture(refresh=lambda **params:changed)['created'],0)
        self.assertFalse(store.forecasts(root=self.root))

    def test_implementation_change_blocks_capture_before_provider_call(self):
        with patch.object(confirmation,'implementation',return_value={}), \
                patch.object(confirmation,'_anchor',side_effect=AssertionError('No changed code')):
            result=self.capture()
        self.assertTrue(result['issues']);self.predict.assert_not_called()

    def test_results_append_revisions_without_rewriting_probabilities(self):
        self.capture();before=store.forecasts(root=self.root)
        finished=self.target(100001,'FT')
        finished['score']['halftime']={'home':1,'away':0};finished['score']['fulltime']={'home':2,'away':2}
        self.save([finished],'2026-10-31T19:00:00+00:00')
        result=confirmation.reconcile(self.root,now=self.clock('2026-10-31T20:00:00Z')())
        self.assertEqual(result['settled'],1)
        forecast=next(row for row in before if row['payload']['fixture']['match_id']=='100001')
        first=store.results(forecast['payload']['id'],root=self.root)
        self.assertEqual(first[-1]['payload']['actual'],'2nd')
        finished['score']['halftime']={'home':2,'away':0};finished['score']['fulltime']={'home':2,'away':0}
        self.save([finished],'2026-10-31T19:30:00+00:00')
        confirmation.reconcile(self.root,now=self.clock('2026-10-31T20:30:00Z')())
        revisions=store.results(forecast['payload']['id'],root=self.root)
        self.assertEqual(len(revisions),2);self.assertEqual(revisions[-1]['payload']['actual'],'1st')
        self.assertEqual(store.forecasts(root=self.root),before)

    def test_administrative_or_missing_scores_never_count_as_losses(self):
        self.capture()
        self.save([self.target(100001,'WO'),self.target(100002,'PST')],'2026-10-31T19:00:00+00:00')
        confirmation.reconcile(self.root,now=self.clock('2026-10-31T20:00:00Z')())
        states={row['fixtures'][0]['match_id']:row['status'] for row in confirmation.refresh_entries(self.root)}
        self.assertEqual(states,{'100001':'void','100002':'unresolved'})
        self.assertFalse(confirmation.report(self.root)['protocols'][0]['metrics'])

    def test_commit_ack_after_capture_deadline_is_not_an_admissible_comparison(self):
        self.capture();saved=store.heads(root=self.root)
        changed=deepcopy(saved)
        for row in changed:
            row['ack'].update(admissible=True,time_quality='local_commit_ack',observed_at_utc='2026-10-31T08:31:00+00:00')
        with patch.object(store,'heads',return_value=changed),patch.object(confirmation,'_registration_ready',return_value=True):
            rows=confirmation._entries(self.root,self.protocol['id'])
        self.assertTrue(all(not row['admissible'] and row['status']=='late_capture' for row in rows))

    def test_synthetic_registration_cannot_be_upgraded_by_a_real_clock_receipt(self):
        self.capture();changed=deepcopy(store.heads(root=self.root))
        for row in changed:
            row['ack'].update(admissible=True,time_quality='local_commit_ack',observed_at_utc='2026-10-31T08:02:00+00:00')
        with patch.object(store,'heads',return_value=changed):
            rows=confirmation._entries(self.root,self.protocol['id'])
        self.assertTrue(all(not row['admissible'] and row['status']=='unattested_capture' for row in rows))

    def test_saved_result_label_must_match_the_halftime_and_fulltime_scores(self):
        self.capture();forecast=store.heads(root=self.root)[0]['payload']
        result=confirmation._seal(dict(status='settled',ht='0-0',ft='0-1',actual='1st',issues=[],
            checked_at='2026-10-31T20:00:00Z',source_ref=dict(match_id=forecast['fixture']['match_id'],
                observed_at_utc='2026-10-31T19:00:00Z')))
        store.put_result(forecast['id'],result,root=self.root,clock=self.clock('2026-10-31T20:00:01Z'))
        with patch.object(confirmation, '_now', return_value=self.clock('2026-10-31T20:01:00Z')()), self.assertRaises(ValueError):
            confirmation.report(self.root)

    def test_readers_reject_unanchored_metadata_and_backwards_save_times(self):
        self.capture();original=store.heads(root=self.root)
        changed=deepcopy(original);changed[0]['payload']['snapshot']['target_observation']['goals']['home']=999
        with patch.object(store,'heads',return_value=changed),self.assertRaises(ValueError):
            confirmation._entries(self.root,self.protocol['id'])
        changed=deepcopy(original);changed[0]['ack']['transaction_at_utc']='2026-10-31T08:00:00Z'
        with patch.object(store,'heads',return_value=changed),self.assertRaises(ValueError):
            confirmation._entries(self.root,self.protocol['id'])

    def test_queue_reconciliation_and_ui_report_do_not_rebuild_feature_histories(self):
        self.capture()
        with patch.object(store,'forecasts',side_effect=AssertionError('Full audit only')), \
                patch.object(confirmation.snapshots,'validate',side_effect=AssertionError('No feature rebuild')):
            confirmation.refresh_entries(self.root)
            confirmation.reconcile(self.root,now=self.clock('2026-10-31T20:00:00Z')())
            confirmation.report(self.root)


if __name__=='__main__':unittest.main()
