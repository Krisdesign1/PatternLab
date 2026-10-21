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
            self.frozen=models.build_methods(sample(),fixtures())
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

    def test_selected_context_is_required_before_any_provider_request(self):
        with self.assertRaises(journal.JournalError):
            tracking.register(sample(),fixtures(),{},'context_neighbors')
        self.client.fixtures.assert_not_called()
        self.assertFalse(journal.DB_PATH.exists())
        self.assertFalse(api.STORE_PATH.exists())
        estimate = dict(eligible=True, generation='selected-context')
        self.frozen['methods']['context_neighbors'] = context_method()
        result = tracking.register(sample(),fixtures(),estimate,'context_neighbors')
        self.assertTrue(result['created'])
        self.assertIn('context_neighbors',result['methods'])
        entry=journal.list_entries()[0]
        self.assertEqual(entry['metadata']['selected_engine'],'context_neighbors')
        self.build.assert_called_with(sample(),fixtures(),estimate=estimate,engine='context_neighbors')
        self.client.fixtures.assert_called_once()


if __name__=='__main__':unittest.main()
