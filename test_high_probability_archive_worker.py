"""Unselected archived matches share the existing calendar/results service."""
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import automation_worker as worker
import high_probability_archive as archive
import candidate_tracking
from test_high_probability_archive import report
from test_high_probability_engine import fixture, history, NOW


class Gate:
    def __init__(self,raw):self.raw=raw;self.calls=[];self.state={}
    def call(self,**params):
        self.calls.append(params)
        return [item for item in self.raw if 'ids' not in params or
                str(item['fixture']['id']) in params['ids'].split('-')]
    def save(self):pass


class ProbabilityArchiveWorkerTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup);self.root=Path(folder.name)
        self.raw=[fixture(100001,status='FT',ht=(1,0),ft=(1,0)),
                  fixture(100002,status='FT',ht=(0,0),ft=(1,0))]

    def capture(self):archive.capture(report(),self.root,clock=lambda:NOW)

    def test_calendar_service_archives_without_any_selected_or_recorded_group(self):
        api.save_fixtures(history(),self.root/'fixtures.json')
        gate=Gate([fixture(100001),fixture(100002)])
        result=worker._calendar(self.root,gate,lambda:NOW,worker._local_day(NOW))
        self.assertEqual(result['created'],2)
        self.assertEqual(len(gate.calls),1)
        self.assertFalse((self.root/'predictions.sqlite3').exists())
        self.assertEqual(len(archive.list_entries(self.root)),2)

    def test_results_queue_fetches_archive_only_matches_and_settles_each_one(self):
        self.capture();gate=Gate(self.raw)
        worker._refresh_results(self.root,gate,lambda:NOW+timedelta(hours=10),{})
        self.assertEqual(len(gate.calls),1)
        self.assertEqual(set(gate.calls[0]['ids'].split('-')),{'100001','100002'})
        with patch.object(worker.cloud_storage,'configured',return_value=False):
            result={};worker._reconcile(self.root,result,NOW+timedelta(hours=10))
        self.assertEqual(result['probability_archive_results']['settled'],2)
        self.assertEqual({row['actual'] for row in archive.list_entries(self.root)},{'1st','2nd'})
        self.assertFalse((self.root/'predictions.sqlite3').exists())

    def test_shared_fixture_ids_use_only_one_existing_results_batch(self):
        self.capture();gate=Gate(self.raw)
        def entries(path):
            return [dict(status='pending',fixtures=[dict(match_id='100001',timestamp='2026-10-22T15:00:00Z')])] if Path(path).name=='predictions.sqlite3' else []
        with patch.object(worker.journal,'list_entries',side_effect=entries):
            worker._refresh_results(self.root,gate,lambda:NOW+timedelta(hours=10),{})
        self.assertEqual(len(gate.calls),1)
        self.assertEqual(set(gate.calls[0]['ids'].split('-')),{'100001','100002'})

    def test_corrupt_secondary_archive_does_not_block_existing_results_queue(self):
        gate=Gate(self.raw)
        entry=dict(status='pending',fixtures=[dict(match_id='100001',timestamp='2026-10-22T15:00:00Z')])
        with patch.object(archive,'list_entries',side_effect=archive.ArchiveError('Archive invalide')), \
                patch.object(worker.journal,'list_entries',side_effect=lambda path:[entry] if Path(path).name=='predictions.sqlite3' else []):
            result={};worker._refresh_results(self.root,gate,lambda:NOW+timedelta(hours=10),result)
        self.assertEqual(gate.calls[0]['ids'],'100001')
        self.assertTrue(result['issues'])

    def test_corrupt_secondary_archive_does_not_block_candidate_reconciliation(self):
        with patch.object(archive,'reconcile',side_effect=archive.ArchiveError('Archive invalide')), \
                patch.object(candidate_tracking,'reconcile',return_value={}) as candidates, \
                patch.object(worker.cloud_storage,'configured',return_value=False), \
                patch.object(worker,'_candidate_observation'):
            result={};worker._reconcile(self.root,result,NOW+timedelta(hours=10))
        candidates.assert_called_once()
        self.assertEqual(result['probability_archive_results']['status'],'unavailable')


if __name__=='__main__':unittest.main()
