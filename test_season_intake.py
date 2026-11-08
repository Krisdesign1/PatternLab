"""Strict season isolation against the real importer and unchanged history queue."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import api_football as api
import automation_worker as worker
import fixture_store
import season_intake as intake
from deployment.linux import run_guarded_worker as runner
from scripts import automation_service as service
from test_high_probability_engine import fixture

NOW = datetime(2026, 11, 6, 10, tzinfo=timezone.utc)


class Provider:
    def __init__(self, batches):
        self.batches, self.calls = batches, []
        self.last_quota = {'requests_remaining_day': 999}

    def fixtures(self, **params):
        self.calls.append(params)
        return dict(response=deepcopy(self.batches[str(params.get('league', '61'))]),
                    paging=dict(current=1, total=1), errors=[])


class SeasonIntakeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.root = Path(directory.name); self.path = self.root/'fixtures.json'
        self.enterContext(patch.dict(os.environ, {'PATTERNLAB_STORAGE_MODE': 'local'}))
        self.enterContext(patch.object(api, 'urlopen', side_effect=AssertionError('No network')))
        self.before = fixture(101,league=39,status='FT',stamp='2026-10-01T12:00:00Z',ht=(1,0),ft=(2,0))
        self.changed = deepcopy(self.before); self.changed['teams']['home']['id'] = 9999
        self.valid = fixture(201,league=61,status='FT',stamp='2026-10-01T13:00:00Z',ht=(0,0),ft=(2,0))
        api.save_fixtures([self.before],self.path)

    def client(self, batches=None):
        provider = Provider(batches or {'39':[self.changed],'61':[self.valid]})
        return intake.SeasonIntakeClient(provider,self.path)

    def evidence(self):
        with sqlite3.connect(self.root/'.season_intake.sqlite3') as connection:
            return [json.loads(row[0]) for row in connection.execute('SELECT evidence FROM rejected_seasons')]

    def test_conflicting_batch_still_fails_original_importer_and_guard_leaves_store_unchanged(self):
        for sqlite in (False,True):
            if sqlite: fixture_store.migrate_json(self.path)
            original = api.load_fixtures(self.path)
            marker = [dict(competition_code='39',season=2026,match_ids=['101'])]
            with self.assertRaisesRegex(api.ApiFootballError,'manifeste'):
                api.save_fixtures([self.changed],self.path,season_imports=marker)
            client = self.client()
            with self.assertRaisesRegex(api.ApiFootballError,'conservé pour examen'):
                client.fixtures(league='39',season=2026,timezone='UTC')
            self.assertEqual(api.load_fixtures(self.path), original)
            self.assertEqual(api.load_season_imports(self.path),[])
            self.assertEqual(len(client.client.calls),1)
            self.assertEqual(client.last_quota,client.client.last_quota)
        records = self.evidence()
        self.assertEqual(len(records),2)
        self.assertEqual(records[0]['conflicts'][0]['reason'],'identity_changed')
        self.assertEqual(records[0]['conflicts'][0]['received'],self.changed)
        self.assertEqual(records[0]['conflicts'][0]['prior']['teams']['home']['id'],391)

    def test_frozen_history_continues_after_rejection_and_keeps_six_hour_deferral(self):
        client = self.client(); config=dict(worker.DEFAULT_CONFIG,history_seasons=[2026])
        state={}; report={'requests':0,'history_imports':0,'history_received':0,'issues':[]}
        gate=worker._Gate(self.root,state,config,client,lambda:NOW,report)
        checkpoints=[]
        worker._history(self.root,config,gate,report,{'39':{},'61':{}},lambda:NOW,
                        after_batch=lambda:checkpoints.append(True))
        self.assertEqual(report['requests'],2)
        self.assertEqual(report['history_imports'],1)
        self.assertEqual(report['history_received'],1)
        self.assertEqual([row['competition_code'] for row in api.load_season_imports(self.path)],['61'])
        self.assertEqual(api.load_fixtures(self.path,ids=['101'])[0]['teams']['home']['id'],391)
        self.assertEqual(state['history_attempts']['39/2026'],NOW.timestamp())
        self.assertEqual(len(checkpoints),2)
        worker._history(self.root,config,gate,report,{'39':{},'61':{}},lambda:NOW)
        self.assertEqual(len(client.client.calls),2)
        self.assertEqual(len(self.evidence()),1)

    def test_valid_new_season_is_returned_exactly_without_writes_and_can_be_imported(self):
        client=self.client()
        original=self.path.read_bytes()
        reply=client.fixtures(league='61',season=2026)
        self.assertEqual(reply['response'],[self.valid])
        self.assertEqual(self.path.read_bytes(),original)
        self.assertEqual(client.deferred,[])
        self.assertFalse((self.root/'.season_intake.sqlite3').exists())
        api.save_fixtures(reply['response'],self.path,season_imports=[dict(
            competition_code='61',season=2026,match_ids=['201'])])
        self.assertEqual(api.load_season_imports(self.path)[0]['complete'],True)

    def test_concurrent_identity_insert_after_prevalidation_still_fails_closed(self):
        # The Linux deployment has one fixture writer. If that assumption changes,
        # the final importer must still refuse a batch invalidated after intake.
        for sqlite in (False,True):
            with self.subTest(sqlite=sqlite):
                path=self.root/('sqlite' if sqlite else 'json')/'fixtures.json'
                api.save_fixtures([self.before],path)
                if sqlite: fixture_store.migrate_json(path)
                concurrent=deepcopy(self.valid)
                concurrent['teams']['home']['id']=9999
                client=intake.SeasonIntakeClient(Provider({'61':[self.valid]}),path)
                guarded_fixtures=client.fixtures
                concurrent_snapshot=[]

                def insert_after_prevalidation(**params):
                    response=guarded_fixtures(**params)
                    self.assertEqual(response['response'],[self.valid])
                    api.save_fixtures([concurrent],path)
                    concurrent_snapshot.append(api.export_store(path))
                    return response

                config=dict(worker.DEFAULT_CONFIG,history_seasons=[2026])
                state={}
                report={'requests':0,'history_imports':0,'history_received':0,'issues':[]}
                gate=worker._Gate(path.parent,state,config,client,lambda:NOW,report)
                with patch.object(client,'fixtures',side_effect=insert_after_prevalidation):
                    with self.assertRaisesRegex(api.ApiFootballError,'manifeste'):
                        worker._history(path.parent,config,gate,report,{'61':{}},lambda:NOW)

                self.assertEqual(api.export_store(path),concurrent_snapshot[0])
                self.assertEqual(api.load_season_imports(path),[])
                self.assertEqual(report['requests'],1)
                self.assertEqual(report['history_imports'],0)
                self.assertEqual(report['history_received'],0)
                self.assertEqual(state['history_attempts']['61/2026'],NOW.timestamp())
                self.assertEqual(client.deferred,[])
                self.assertFalse((path.parent/'.season_intake.sqlite3').exists())

    def test_nonseason_requests_are_passthrough_and_provider_errors_remain_errors(self):
        client=self.client()
        for params in ({'date':'2026-11-06'},{'ids':'101'},
                       {'league':'39','season':2026,'status':'FT'}):
            client.fixtures(**params)
        self.assertEqual(len(client.client.calls),3)
        self.assertFalse((self.root/'.season_intake.sqlite3').exists())
        with patch.object(client.client,'fixtures',side_effect=api.ApiFootballError('Quota provider')):
            with self.assertRaisesRegex(api.ApiFootballError,'Quota provider'):
                client.fixtures(league='39',season=2026)

    def test_empty_full_season_is_not_replaced_or_loaded_from_local_data(self):
        client=self.client({'39':[]})
        with patch.object(api,'load_fixtures',side_effect=AssertionError('No empty IDs full scan')):
            self.assertEqual(client.fixtures(league='39',season=2026)['response'],[])

    def test_incomplete_paging_duplicate_ids_and_mismatched_seasons_never_return_as_valid(self):
        for rows in ([self.valid,self.valid],[self.changed]):
            client=self.client({'61':rows})
            with self.assertRaises(api.ApiFootballError):client.fixtures(league='61',season=2026)
            # The existing response validator already refuses duplicate IDs.
            if len(rows)==1:self.assertEqual(client.deferred[0]['conflicts'],1)
        client=self.client()
        with patch.object(client.client,'fixtures',return_value=dict(response=[self.valid],paging={'total':2})):
            with self.assertRaisesRegex(api.ApiFootballError,'paginée'):
                client.fixtures(league='61',season=2026)

    def test_admin_correction_is_not_automatically_reaccepted(self):
        latest={'match_id':'101','review':{'choice':'prior'},'decision':'reviewed',
                'received_raw_sha256':intake.policy.digest(self.changed),
                'selected_raw_sha256':intake.policy.digest(self.before)}
        with patch.object(api,'load_import_observations',return_value=[latest]):
            # The selected prior version stays selected under the exact import policy.
            client=self.client();reply=client.fixtures(league='39',season=2026)
            self.assertEqual(reply['response'],[self.changed])
            self.assertEqual(api.load_fixtures(self.path)[0]['teams']['home']['id'],391)

    def test_evidence_failure_refuses_import_and_never_discloses_exception(self):
        client=self.client()
        with patch.object(intake,'_record',side_effect=OSError('private-token-details')):
            with self.assertRaisesRegex(api.ApiFootballError,'indisponible') as caught:
                client.fixtures(league='39',season=2026)
        self.assertNotIn('private-token-details',str(caught.exception))
        self.assertEqual(client.deferred,[])

    def test_linux_boundary_keeps_training_disabled_and_real_service_accounting(self):
        project=self.root/'project'; fake=Mock()
        fake.run_cycle.return_value=dict(status='completed',requests=7,created=2,issues=['private'])
        config=dict(worker.DEFAULT_CONFIG)
        with patch.object(service,'_configuration',return_value=config), \
             patch.object(service,'_prepare_models',side_effect=AssertionError('No training')):
            result=runner.run_once(project,worker=fake,provider=Provider({}))
        arguments=fake.run_cycle.call_args.kwargs
        self.assertFalse(arguments['config']['models_enabled'])
        self.assertEqual(arguments['config']['model_jobs_per_cycle'],0)
        self.assertEqual(arguments['origin'],'service')
        self.assertIsInstance(arguments['client'],intake.SeasonIntakeClient)
        self.assertTrue(config['models_enabled'])
        self.assertEqual(result['requests'],7)
        self.assertEqual(result['issues'],1)
        self.assertNotIn('private',json.dumps(result))

    def test_disabled_service_never_loads_provider_credentials(self):
        fake=Mock();fake.run_cycle.return_value={'status':'disabled'}
        with patch.object(service,'_configuration',return_value=dict(worker.DEFAULT_CONFIG,enabled=False)), \
             patch.object(api,'load_key',side_effect=AssertionError('No credential needed')):
            result=runner.run_once(self.root,worker=fake)
        self.assertEqual(result['status'],'disabled')
        self.assertIsNone(fake.run_cycle.call_args.kwargs['client'])


if __name__=='__main__': unittest.main()
