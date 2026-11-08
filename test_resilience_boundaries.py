"""Offline failure injection at application storage/publication boundaries.

Every store is temporary. These checks exercise actual app writers and readers,
not a live outage, a full disk, a production restoration or a provider request.
"""
from contextlib import ExitStack, closing
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import automation_worker as worker
import causal_confirmation as confirmation
import causal_confirmation_store as confirmation_store
import fixture_store
import high_probability_archive as archive
import prediction_journal as journal
import prospective_models as models
import catboost_scenarios as ml
import test_causal_confirmation as causal_helpers
from test_api_football import fixture
from test_catboost_scenarios import sample, fixtures
from test_high_probability_archive import report as probability_report
from test_high_probability_engine import fixture as target, NOW
from test_prediction_journal import BEFORE, raw_fixtures
from test_result_refresh_resilience import Gate


class StorageBoundaryTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.path=self.root/'fixtures.json'
        self.patches=ExitStack();self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(api,'load_key',side_effect=AssertionError('No credentials')))
        self.network=self.patches.enter_context(patch.object(api.ApiFootballClient,'get',
                                                            side_effect=AssertionError('No network')))
        self.patches.enter_context(patch.object(worker.cloud_storage,'configured',return_value=False))

    def migrate(self):
        api.save_fixtures([fixture()],self.path)
        fixture_store.migrate_json(self.path)
        return fixture_store.database_path(self.path)

    def test_failed_batch_rolls_back_correction_projection_and_import_proofs_then_retries(self):
        database=self.migrate()
        with closing(sqlite3.connect(database)) as con:
            con.execute("CREATE TRIGGER fault_before_second_insert BEFORE INSERT ON fixtures "
                        "WHEN NEW.match_id='789' BEGIN SELECT RAISE(ABORT,'synthetic storage fault'); END")
            con.commit()
        before=api.export_store(self.path);signature=api.data_signature(self.path)
        correction=fixture();correction['score']['halftime']={'home':2,'away':0}
        correction['score']['fulltime']={'home':2,'away':0}
        incoming=[correction,fixture(456),fixture(789)]
        with self.assertRaises(api.ApiFootballError):api.save_fixtures(incoming,self.path)
        self.assertEqual(api.export_store(self.path),before)
        self.assertEqual(api.data_signature(self.path),signature)
        self.assertEqual(fixture_store.import_observations(database),before['import_revisions'])
        with closing(sqlite3.connect(database)) as con:
            con.execute('DROP TRIGGER fault_before_second_insert');con.commit()
        self.assertEqual(api.save_fixtures(incoming,self.path),3)
        rows={r['match_id']:r for r in api.load_matches(self.path)[0]}
        self.assertEqual(rows['123']['value'],'1st')
        self.assertEqual(rows['123']['ht'],rows['123']['ft'])
        saved=api.export_store(self.path)
        self.assertGreater(len(saved['import_revisions']),len(before['import_revisions']))
        self.assertEqual({r['fixture']['id'] for r in saved['fixtures']},{123,456,789})

    def test_real_writer_lock_preserves_wal_reads_and_retry_after_release(self):
        database=self.migrate();before=api.export_store(self.path)
        real_connect=sqlite3.connect
        class ShortBusy(sqlite3.Connection):
            def execute(self,sql,*args,**kwargs):
                if sql=='PRAGMA busy_timeout=30000':sql='PRAGMA busy_timeout=10'
                return super().execute(sql,*args,**kwargs)
        with closing(real_connect(database,isolation_level=None)) as writer:
            writer.execute('BEGIN IMMEDIATE')
            writer.execute("UPDATE metadata SET value='uncommitted' WHERE key='updated_at'")
            self.assertEqual(api.export_store(self.path),before)
            def short_connect(*args,**kwargs):
                return real_connect(*args,**dict(kwargs,factory=ShortBusy))
            with patch.object(fixture_store.sqlite3,'connect',side_effect=short_connect), \
                    self.assertRaises(api.ApiFootballError):
                api.save_fixtures([fixture(456)],self.path)
            self.assertEqual(api.export_store(self.path),before)
            writer.rollback()
        self.assertEqual(api.save_fixtures([fixture(456)],self.path),2)
        self.assertEqual({r['fixture']['id'] for r in api.load_fixtures(self.path)},{123,456})

    def test_corrupt_active_store_does_not_fall_back_to_obsolete_json(self):
        database=self.migrate();backup=self.root/'good.sqlite3'
        with closing(sqlite3.connect(database)) as src,closing(sqlite3.connect(backup)) as dst:src.backup(dst)
        original_json=self.path.read_bytes()
        database.write_bytes(b'Synthetic corrupted SQLite header')
        with self.assertRaises(api.ApiFootballError):api.load_fixtures(self.path)
        self.assertEqual(self.path.read_bytes(),original_json)
        shutil.copy2(backup,database)
        self.assertEqual(api.load_fixtures(self.path),[fixture()])
        self.network.assert_not_called()


class JournalPublicationBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.object(ml,'load_report',return_value=None), \
                patch.object(models,'_context_neighbors',side_effect=models.SnapshotError('Synthetic absent model')):
            cls.methods=models.build_methods(sample(),fixtures(),decision_at=BEFORE)['methods']

    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.path=Path(temp.name)/'predictions.sqlite3'
        key=patch.object(api,'load_key',side_effect=AssertionError('No credentials'))
        key.start();self.addCleanup(key.stop)
        network=patch.object(api.ApiFootballClient,'get',side_effect=AssertionError('No network'))
        self.network=network.start();self.addCleanup(network.stop)

    def record(self):
        return journal.record(fixtures(),self.methods,now=BEFORE,path=self.path,raw_fixtures=raw_fixtures())

    def test_commit_failure_publishes_no_forecast_and_safe_retry_keeps_first_prediction(self):
        real_connect=sqlite3.connect
        class FailCommit(sqlite3.Connection):
            def commit(self):raise sqlite3.OperationalError('synthetic disk full PRIVATE_TEST_TEXT')
        def failed_connect(*args,**kwargs):return real_connect(*args,**dict(kwargs,factory=FailCommit))
        with patch.object(journal.sqlite3,'connect',side_effect=failed_connect):
            with self.assertRaises(journal.JournalError) as error:self.record()
        self.assertNotIn('PRIVATE_TEST_TEXT',str(error.exception))
        self.assertEqual(journal.list_entries(self.path),[])
        first=self.record();self.assertTrue(first['created'])
        before=journal.list_entries(self.path)
        second=self.record();self.assertFalse(second['created'])
        self.assertEqual(first['id'],second['id'])
        self.assertEqual(journal.list_entries(self.path),before)
        self.network.assert_not_called()


class CausalAcknowledgmentBoundaryTests(unittest.TestCase):
    setUp=causal_helpers.ConfirmationTests.setUp
    target=causal_helpers.ConfirmationTests.target
    clock=staticmethod(causal_helpers.ConfirmationTests.clock)
    save=causal_helpers.ConfirmationTests.save
    capture=causal_helpers.ConfirmationTests.capture

    def test_forecast_commit_then_ack_failure_never_creates_a_retroactive_receipt_on_retry(self):
        with patch.object(confirmation_store,'_write_ack',side_effect=sqlite3.OperationalError('Synthetic receipt disk fault')):
            with self.assertRaises(sqlite3.OperationalError):self.capture()
        rows=confirmation_store.heads(self.root)
        self.assertEqual(len(rows),1);self.assertIsNone(rows[0]['ack'])
        preserved=deepcopy(rows[0]['payload'])
        result=confirmation.capture_due(self.root,clock=self.clock('2026-10-31T08:02:00Z'),refresh=lambda **kw:self.raw)
        self.assertEqual(result['created'],1)
        rows=confirmation_store.heads(self.root)
        first=next(r for r in rows if r['payload']['id']==preserved['id'])
        self.assertEqual(first['payload'],preserved);self.assertIsNone(first['ack'])
        view=confirmation.report(self.root)['protocols'][0]
        self.assertEqual(view['missing_ack'],1);self.assertEqual(view['admissible'],0)
        self.assertEqual(len(rows),2);self.network.assert_not_called()

    def test_result_commit_then_ack_failure_stays_unattested_after_replay(self):
        self.capture();forecast=confirmation_store.heads(self.root)[0]['payload']
        result=confirmation._seal(dict(status='settled',ht='0-0',ft='0-1',actual='2nd',issues=[],
            checked_at='2026-10-31T19:00:00+00:00'))
        with patch.object(confirmation_store,'_write_result_ack',side_effect=sqlite3.OperationalError('Synthetic result receipt fault')):
            with self.assertRaises(sqlite3.OperationalError):
                confirmation_store.put_result(forecast['id'],result,self.root,clock=self.clock('2026-10-31T19:01:00Z'))
        saved=confirmation_store.results(forecast['id'],self.root)
        self.assertEqual(len(saved),1);self.assertIsNone(saved[0]['ack'])
        replay=confirmation_store.put_result(forecast['id'],result,self.root,clock=self.clock('2026-10-31T19:05:00Z'))
        self.assertTrue(replay['existing']);self.assertIsNone(replay['ack'])
        self.assertEqual(replay['revision'],1)
        self.assertEqual(confirmation_store.results(forecast['id'],self.root),saved)
        self.network.assert_not_called()


class ProviderResumeBoundaryTests(unittest.TestCase):
    def test_partial_sqlite_import_and_transport_failure_resume_without_fabricated_losses(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(worker.cloud_storage,'configured',return_value=False), \
                patch.object(api,'load_key',side_effect=AssertionError('No credentials')), \
                patch.object(api.ApiFootballClient,'get',side_effect=AssertionError('No network')) as network:
            root=Path(folder);path=root/'fixtures.json';now=NOW+timedelta(hours=10)
            selected=[target(100001+i) for i in range(25)]
            finals=[target(100001+i,status='FT',ht=(1,0),ft=(1,0)) for i in range(25)]
            api.save_fixtures(selected,path);fixture_store.migrate_json(path)
            archive.capture(probability_report(),root,clock=lambda:NOW)
            probabilities={r['fixture']['match_id']:deepcopy(r['probabilities']) for r in archive.list_entries(root)}
            tracked=[dict(status='pending',fixtures=[dict(match_id=str(r['fixture']['id']),timestamp=r['fixture']['date']) for r in selected])]
            class NetworkGate(Gate):
                def call(self,**params):
                    rows=super().call(**params)
                    if len(self.calls)>1:raise api.ApiFootballError('Synthetic transport failure')
                    return rows
            gate=NetworkGate(root,finals,omitted={'100001'})
            with patch.object(journal,'list_entries',return_value=tracked):
                with self.assertRaises(api.ApiFootballError):worker._refresh_results(root,gate,lambda:now,{})
                state=json.loads((root/'state.json').read_text())
                self.assertEqual(len(state['result_checks']),20)
                self.assertEqual(state['result_missing']['100001']['attempts'],1)
                archive.reconcile(root=root,now=now)
                rows={r['fixture']['match_id']:r for r in archive.list_entries(root)}
                self.assertEqual(rows['100001']['status'],'pending');self.assertIsNone(rows['100001']['actual'])
                self.assertEqual(rows['100002']['status'],'settled')
                resumed=Gate(root,finals,state=state)
                worker._refresh_results(root,resumed,lambda:now+timedelta(minutes=16),{})
            archive.reconcile(root=root,now=now+timedelta(minutes=16))
            rows=archive.list_entries(root)
            self.assertTrue(all(r['status']=='settled' for r in rows))
            self.assertEqual({r['fixture']['match_id']:r['probabilities'] for r in rows},probabilities)
            self.assertEqual(len(resumed.state['result_checks']),25)
            self.assertFalse(resumed.state['result_missing'])
            self.assertTrue(all(api._status_short(r)=='FT' for r in api.load_fixtures(path)))
            network.assert_not_called()


if __name__=='__main__':
    unittest.main()
