from copy import deepcopy
from datetime import datetime,timedelta,timezone
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import high_probability_archive as archive
import high_probability_workspace as workspace
import probability_time_evidence as evidence
from test_high_probability_archive import report
from test_high_probability_engine import NOW,fixture,history


class ProbabilityTimeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.report=report()
        self.row=self.report['rows'][0]

    def receipt(self,**values):
        value=evidence.read_receipt(NOW-timedelta(seconds=3),NOW-timedelta(seconds=1),
            ('not_exported',1,'uuid'),('not_exported',1,'uuid'),self.report['before'],self.report['rows'])
        value.update(values)
        return value

    def test_current_read_window_is_bound_but_not_upgraded_to_historical_availability(self):
        self.report['source_read_receipt']=self.receipt()
        provenance=evidence.qualify(self.report,self.row,NOW)
        self.assertEqual(provenance['quality'],'local_read_window')
        self.assertFalse(provenance['strict_as_of_used'])
        self.assertFalse(provenance['historical_availability_proven'])
        self.assertNotIn('not_exported',str(provenance))
        evidence.validate_saved(provenance,created_at=NOW,before=self.report['before'],
            league=self.row['fixture']['league'],history_fingerprint=self.row['history_fingerprint'])

    def test_future_read_mismatched_history_and_false_revision_claim_are_rejected(self):
        for receipt in (self.receipt(read_finished_at=(NOW+timedelta(seconds=1)).isoformat()),
                        self.receipt(history_fingerprints={}),
                        self.receipt(history_fingerprints=[]),
                        self.receipt(store_revision_end=[2,'uuid'])):
            self.report['source_read_receipt']=receipt
            with self.assertRaises(ValueError):
                evidence.qualify(self.report,self.row,NOW)

    def test_isolated_test_clock_cannot_manufacture_positive_read_evidence(self):
        self.report['source_read_receipt']=self.receipt(read_finished_at=(NOW+timedelta(days=1)).isoformat())
        result=evidence.qualify(self.report,self.row,NOW,isolated_clock=True)
        self.assertEqual(result['quality'],'isolated_clock_unattested')
        self.assertNotIn('source_read',result)
        self.report['source_read_receipt']=self.receipt()
        self.assertEqual(evidence.qualify(self.report,self.row,NOW,isolated_clock=True)['quality'],
                         'isolated_clock_unattested')

    def test_concurrent_revision_is_visible_and_saved_future_times_are_refused(self):
        self.report['source_read_receipt']=evidence.read_receipt(NOW-timedelta(seconds=3),NOW-timedelta(seconds=1),
            ('path',1,'uuid'),('path',2,'uuid'),self.report['before'],self.report['rows'])
        result=evidence.qualify(self.report,self.row,NOW)
        self.assertEqual(result['quality'],'concurrent_store_read')
        result['source_read']['read_finished_at']=(NOW+timedelta(seconds=1)).isoformat()
        with self.assertRaises(ValueError):
            evidence.validate_saved(result,created_at=NOW,before=self.report['before'],
                league=self.row['fixture']['league'],history_fingerprint=self.row['history_fingerprint'])

    def test_unchanged_probabilities_do_not_multiply_captures_when_read_times_advance(self):
        with tempfile.TemporaryDirectory() as directory:
            current=datetime.now(timezone.utc)
            day=(current+timedelta(days=1)).date().isoformat()
            self.report['day']=day
            for row in self.report['rows']:
                row['fixture']['timestamp']=day+'T15:00:00+00:00'
            self.report['source_read_receipt']=evidence.read_receipt(current-timedelta(seconds=3),
                current-timedelta(seconds=1),('path',1,'uuid'),('path',1,'uuid'),self.report['before'],self.report['rows'])
            first=archive.capture(self.report,root=directory)
            self.assertEqual(first['created'],2)
            stored=archive.list_entries(directory)
            self.assertTrue(all(item['temporal_provenance']['quality']=='local_read_window' for item in stored))
            self.report['source_read_receipt']['read_finished_at']=current.isoformat()
            second=archive.capture(self.report,root=directory)
            self.assertEqual(second['created'],0)
            self.assertEqual(archive.list_entries(directory),stored)

    def test_legacy_capture_without_evidence_remains_readable_unchanged(self):
        saved=archive._snapshot(self.report,self.row,NOW)
        saved.pop('temporal_provenance')
        saved['id']=archive._identity(saved)
        original=deepcopy(saved)
        self.assertEqual(archive._validate(saved),original)
        self.assertEqual(saved,original)

    def test_cached_scan_keeps_actual_read_window_instead_of_synthetic_period_start(self):
        import api_football as api
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            api.save_fixtures(history()+[fixture(100001)],root/'fixtures.json')
            workspace._cached_scan.cache_clear();workspace._cached_targets.cache_clear()
            self.addCleanup(workspace._cached_scan.cache_clear)
            first=workspace.load_scan(root,NOW)
            repeated=workspace.load_scan(root,NOW+timedelta(seconds=2))
            self.assertEqual(first['source_read_receipt'],repeated['source_read_receipt'])
            self.assertEqual(first['source_read_receipt']['read_finished_at'],first['generated_at'])
            self.assertTrue(first['source_read_receipt']['consistent_store_revision'])
            self.assertNotEqual(first['source_read_receipt']['read_started_at'],NOW.replace(hour=0).isoformat())

    def test_old_target_cache_cannot_attest_a_new_revision_as_a_coherent_read(self):
        import api_football as api
        import fixture_store
        import api_match_workspace as calendar
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'fixtures.json'
            targets=[fixture(100001)]
            api.save_fixtures(history()+targets,path)
            fixture_store.migrate_json(path)
            workspace._cached_scan.cache_clear();workspace._cached_targets.cache_clear()
            self.addCleanup(workspace._cached_scan.cache_clear)
            _path,revision,identity=api.data_signature(path)
            workspace._cached_targets(str(path),revision,identity,self.report['day'])
            changed=deepcopy(targets[0]);changed['teams']['home']['name']='Renamed before the new read'
            api.save_fixtures([changed],path)
            result=workspace._cached_scan(str(path),revision,identity,NOW.isoformat(),self.report['day'],self.report['before'])
            self.assertEqual(result['rows'][0]['fixture']['home'],'Renamed before the new read')
            self.assertTrue(result['source_read_receipt']['consistent_store_revision'])


if __name__=='__main__':
    unittest.main()
