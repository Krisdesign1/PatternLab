import copy
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import api_football as api
import catboost_scenarios as ml
import journal_tracking as tracking
import prediction_journal as journal
import prospective_models as models
from test_catboost_scenarios import fixtures, sample
from test_prediction_journal import BEFORE, AFTER, raw_fixtures
from test_prospective_models import context_method


class FixedTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return BEFORE.astimezone(tz) if tz else BEFORE.replace(tzinfo=None)


class TrackingTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.root=Path(folder.name)
        with patch.object(ml,'load_report',return_value=None), patch.object(models,'_context_neighbors',
                side_effect=models.SnapshotError('Configuration par contexte absente.')):
            self.frozen=models.build_methods(sample(),fixtures(),decision_at=BEFORE)
        self.client=MagicMock()
        self.client.fixtures.return_value=dict(errors=[],results=4,response=raw_fixtures())
        for patcher in (patch.object(tracking,'datetime',FixedTime),patch.object(journal,'_now',return_value=BEFORE),
                        patch.object(journal,'DB_PATH',self.root/'journal.sqlite3'),
                        patch.object(api,'STORE_PATH',self.root/'fixtures.json'),patch.object(api,'load_key',return_value='test-token'),
                        patch.object(api,'ApiFootballClient',return_value=self.client)):
            patcher.start();self.addCleanup(patcher.stop)
        self.builder=patch.object(models,'build_methods',return_value=self.frozen)
        self.build=self.builder.start();self.addCleanup(self.builder.stop)

    def test_all_methods_checked_once_and_first_prediction_not_replaced(self):
        result=tracking.register(sample(),fixtures(),{'eligible':True},'statistical')
        self.assertEqual(result['state'],'recorded')
        self.assertTrue(result['created'])
        self.assertEqual(set(result['methods']),{'frequency','clubs','combined'})
        self.client.fixtures.assert_called_once_with(ids='9000-9001-9002-9003',timezone='UTC')
        again=tracking.register(sample(),fixtures(),{'eligible':True},'catboost')
        self.assertFalse(again['created'])
        self.assertEqual(again['id'],result['id'])
        self.assertEqual(self.build.call_count,1)
        self.assertEqual(self.client.fixtures.call_count,1)

    def test_started_or_incomplete_response_never_creates_a_forecast(self):
        self.client.fixtures.return_value['response'][0]['fixture']['status']['short']='1H'
        with self.assertRaises(journal.JournalError):tracking.register(sample(),fixtures(),{},'statistical')
        self.assertFalse(journal.DB_PATH.exists())
        self.client.fixtures.return_value=dict(errors=[],results=3,response=raw_fixtures()[:3])
        with self.assertRaises(journal.JournalError):tracking.register(sample(),fixtures(),{},'statistical')
        self.assertFalse(journal.DB_PATH.exists())

    def test_retro_simulation_never_builds_or_fetches_or_creates_a_database(self):
        with patch.object(tracking,'datetime',wraps=datetime) as now:
            now.now.return_value=AFTER
            result=tracking.register(sample(),fixtures(),{},'statistical')
        self.assertEqual(result['state'],'retrospective')
        self.build.assert_not_called();self.client.fixtures.assert_not_called()
        self.assertFalse(journal.DB_PATH.exists())

    def test_explicit_result_refresh_reconciles_without_changing_methods(self):
        tracking.register(sample(),fixtures(),{},'statistical')
        entry=journal.list_entries()[0]
        self.client.fixtures.return_value=dict(errors=[],results=4,response=raw_fixtures(status='FT'))
        with patch.object(journal,'_now',return_value=AFTER):
            tracking.refresh_entry(entry)
        final=journal.list_entries()[0]
        self.assertEqual(final['status'],'settled')
        self.assertEqual(final['methods'],entry['methods'])
        self.assertEqual(self.build.call_count,1)

    def test_page_reconciliation_reuses_local_revision_and_rechecks_new_api_scores(self):
        tracking.register(sample(),fixtures(),{},'statistical')
        before=journal.list_entries()[0]
        self.client.fixtures.reset_mock()
        with patch.object(journal,'_reconcile_snapshot',wraps=journal._reconcile_snapshot) as reconcile, \
             patch.object(api,'load_fixtures',wraps=api.load_fixtures) as read:
            tracking.reconcile_local(if_changed=True)
            signature=tracking._reconciliation_signature()
            again=tracking.reconcile_local(if_changed=True)
            self.assertEqual(again['changed'],0)
            self.assertEqual(reconcile.call_count,1)
            self.assertEqual(read.call_count,1)
            self.assertEqual(tracking._reconciliation_signature(),signature)
            api.save_fixtures(raw_fixtures(status='FT'),api.STORE_PATH)
            with patch.object(journal,'_now',return_value=AFTER):
                settled=tracking.reconcile_local(if_changed=True)
                tracking.reconcile_local(if_changed=True)
            self.assertEqual(settled['changed'],1)
            self.assertEqual(reconcile.call_count,2)
        after=journal.list_entries()[0]
        self.assertEqual(after['status'],'settled')
        self.assertEqual(after['methods'],before['methods'])
        self.client.fixtures.assert_not_called()

    def test_page_reconciliation_detects_journal_wal_revision_without_minute_timer(self):
        tracking.register(sample(),fixtures(),{},'statistical')
        with patch.object(journal,'_reconcile_snapshot',wraps=journal._reconcile_snapshot) as reconcile:
            tracking.reconcile_local(if_changed=True)
            # A WAL revision is input even when the main SQLite file is unchanged.
            wal=Path(str(journal.DB_PATH)+'-wal')
            original=tracking._file_revision
            with patch.object(tracking,'_file_revision',side_effect=lambda path:
                ('wal-revision',2) if Path(path)==wal else original(path)):
                tracking.reconcile_local(if_changed=True)
                tracking.reconcile_local(if_changed=True)
            self.assertEqual(reconcile.call_count,2)

    def test_explicit_reconciliation_is_never_skipped_and_failure_is_retried(self):
        tracking.register(sample(),fixtures(),{},'statistical')
        with patch.object(journal,'_reconcile_snapshot',wraps=journal._reconcile_snapshot) as reconcile, \
             patch.object(journal,'reconcile',wraps=journal.reconcile) as explicit:
            tracking.reconcile_local(if_changed=True)
            tracking.reconcile_local()
            self.assertEqual(reconcile.call_count,2)
            explicit.assert_called_once_with()
        original=journal._reconcile_snapshot
        failed=[False]
        def fail_once(**kwargs):
            if not failed[0]:
                failed[0]=True
                raise journal.JournalError('retry')
            return original(**kwargs)
        with patch.object(journal,'_reconcile_snapshot',side_effect=fail_once) as reconcile:
            with self.assertRaises(journal.JournalError):tracking.reconcile_local(if_changed=True)
            tracking.reconcile_local(if_changed=True)
            self.assertEqual(reconcile.call_count,2)

    def test_clock_crossing_a_kickoff_rechecks_unchanged_saved_results(self):
        tracking.register(sample(),fixtures(),{},'statistical')
        with patch.object(journal,'_reconcile_snapshot',wraps=journal._reconcile_snapshot) as reconcile:
            tracking.reconcile_local(if_changed=True)
            with patch.object(tracking,'datetime',wraps=datetime) as clock, \
                 patch.object(journal,'_now',return_value=AFTER):
                clock.now.return_value=AFTER
                tracking.reconcile_local(if_changed=True)
                tracking.reconcile_local(if_changed=True)
            self.assertEqual(reconcile.call_count,2)
        self.assertEqual(journal.list_entries()[0]['status'],'pending')

    def test_concurrent_result_revision_is_not_marked_as_our_reconciled_input(self):
        tracking.register(sample(),fixtures(),{},'statistical')
        original=journal._reconcile_snapshot
        def another_writer(**kwargs):
            result=original(**kwargs)
            # Another writer appends scores after our transaction commits,
            # before the page samples the resulting file signature.
            with patch.object(journal,'_now',return_value=AFTER):
                original(raw_fixtures(status='FT'),now=AFTER)
            return result
        with patch.object(journal,'_reconcile_snapshot',side_effect=another_writer) as reconcile:
            tracking.reconcile_local(if_changed=True)
            tracking.reconcile_local(if_changed=True)
            self.assertEqual(reconcile.call_count,2)

    def test_changed_page_reconciliation_reuses_its_two_existing_snapshots(self):
        tracking.register(sample(),fixtures(),{},'statistical')
        methods=copy.deepcopy(journal.list_entries()[0]['methods'])
        with patch.object(journal,'list_entries',wraps=journal.list_entries) as read:
            tracking.reconcile_local(if_changed=True)
            self.assertEqual(read.call_count,2)
            tracking.reconcile_local(if_changed=True)
            self.assertEqual(read.call_count,2)
            api.save_fixtures(raw_fixtures(status='FT'),api.STORE_PATH)
            with patch.object(journal,'_now',return_value=AFTER):
                result=tracking.reconcile_local(if_changed=True)
                tracking.reconcile_local(if_changed=True)
            self.assertEqual(result['settled'],1)
            self.assertEqual(read.call_count,4)
        self.assertEqual(journal.list_entries()[0]['methods'],methods)

    def test_concurrent_revision_between_final_read_signature_and_read_forces_retry(self):
        tracking.register(sample(),fixtures(),{},'statistical')
        original=journal._reconcile_snapshot
        first=[True]
        def another_writer(**kwargs):
            before_read=kwargs['_before_final_read']
            def append_after_signature():
                before_read()
                if first[0]:
                    first[0]=False
                    with patch.object(journal,'_now',return_value=AFTER):
                        original(raw_fixtures(status='FT'),now=AFTER)
            return original(**dict(kwargs,_before_final_read=append_after_signature))
        with patch.object(journal,'_reconcile_snapshot',side_effect=another_writer) as reconcile:
            tracking.reconcile_local(if_changed=True)
            tracking.reconcile_local(if_changed=True)
            tracking.reconcile_local(if_changed=True)
            self.assertEqual(reconcile.call_count,2)
        self.assertEqual(journal.list_entries()[0]['status'],'settled')

    def test_api_import_during_final_snapshot_is_not_hidden_by_cached_input(self):
        tracking.register(sample(),fixtures(),{},'statistical')
        original=journal._reconcile_snapshot
        first=[True]
        def concurrent_import(**kwargs):
            before_read=kwargs['_before_final_read']
            def import_after_signature():
                before_read()
                if first[0]:
                    first[0]=False
                    api.save_fixtures(raw_fixtures(status='FT'),api.STORE_PATH)
            return original(**dict(kwargs,_before_final_read=import_after_signature))
        with patch.object(journal,'_reconcile_snapshot',side_effect=concurrent_import) as reconcile:
            tracking.reconcile_local(if_changed=True)
            with patch.object(journal,'_now',return_value=AFTER):
                result=tracking.reconcile_local(if_changed=True)
                tracking.reconcile_local(if_changed=True)
            self.assertEqual(reconcile.call_count,2)
            self.assertEqual(result['settled'],1)

    def test_first_page_on_empty_journal_never_creates_storage(self):
        with patch.object(journal,'list_entries',wraps=journal.list_entries) as read:
            result=tracking.reconcile_local(if_changed=True)
        self.assertEqual(result['entries'],0)
        self.assertEqual(read.call_count,1)
        self.assertFalse(journal.DB_PATH.exists())

    def test_selected_context_is_required_before_any_provider_request(self):
        with self.assertRaises(journal.JournalError):
            tracking.register(sample(),fixtures(),{},'context_neighbors')
        self.client.fixtures.assert_not_called()
        self.assertFalse(journal.DB_PATH.exists())
        self.assertFalse(api.STORE_PATH.exists())
        estimate = dict(eligible=True, generation='selected-context')
        self.frozen['methods']['context_neighbors'] = context_method()
        self.frozen['methods']['context_neighbors']['training_before'] = BEFORE.date().isoformat()
        self.frozen['unavailable'].pop('context_neighbors', None)
        result = tracking.register(sample(),fixtures(),estimate,'context_neighbors')
        self.assertTrue(result['created'])
        self.assertIn('context_neighbors',result['methods'])
        entry=journal.list_entries()[0]
        self.assertEqual(entry['metadata']['selected_engine'],'context_neighbors')
        self.build.assert_called_with(sample(),fixtures(),estimate=estimate,engine='context_neighbors',decision_at=BEFORE)
        self.client.fixtures.assert_called_once()


if __name__=='__main__':unittest.main()
