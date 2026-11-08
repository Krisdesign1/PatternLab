from copy import deepcopy
from datetime import datetime,timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import api_football as api
import fixture_store
import argumented_selection_study as study
from test_high_probability_engine import history,fixture


class StudyTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'api';self.root.mkdir()
        self.directory=Path(self.temp.name)/'study'
        raw=history(equal=True)+[fixture(100001,status='FT',stamp='2026-10-22T15:00:00+00:00',ht=(0,0),ft=(0,0))]
        api.save_fixtures(raw,self.root/'fixtures.json')
        fixture_store.migrate_json(self.root/'fixtures.json')
        self.network=patch.object(api.ApiFootballClient,'get',side_effect=AssertionError('Offline only'))
        self.network.start();self.addCleanup(self.network.stop)

    def prepare(self):return study.prepare(self.directory,root=self.root,start='2026-10-22',end='2026-10-23')
    def evaluate(self):return study.run(self.directory,root=self.root,start='2026-10-22',end='2026-10-23')

    def test_snapshot_is_frozen_indexed_and_does_not_rewrite_provider_data(self):
        before=api.data_signature(self.root/'fixtures.json')
        plan=self.prepare();data=study._read(self.directory/'input.json.gz')
        self.assertEqual(plan['manifest']['rows'],37)
        self.assertEqual(len(data['rows']),37)
        self.assertEqual(plan['manifest']['source_signature']['revision'],before[1])
        self.assertEqual(data['plan_sha256'],study._hash(self.directory/'plan.json'))
        self.assertEqual(before,api.data_signature(self.root/'fixtures.json'))
        self.assertFalse(plan['plan']['network']);self.assertFalse(plan['plan']['promotion'])

    def test_real_end_to_end_offline_replay_resume_and_verified_reader(self):
        before=api.data_signature(self.root/'fixtures.json')
        report=self.evaluate()
        self.assertTrue(report['exploratory'])
        self.assertEqual(report['policies']['argumented']['overall']['selected']['N'],1)
        with patch.object(study,'_snapshot',side_effect=AssertionError('Frozen data')):
            self.assertEqual(self.evaluate(),report)
        self.assertEqual(study.load_report(self.directory),report)
        self.assertEqual(api.data_signature(self.root/'fixtures.json'),before)

    def test_reading_before_any_run_is_side_effect_free(self):
        self.assertIsNone(study.load_report(self.directory))
        self.assertEqual(study.signature(self.directory),())
        self.assertFalse(self.directory.exists())

    def test_input_tampering_and_different_period_are_rejected(self):
        self.prepare()
        with self.assertRaises(ValueError):study.prepare(self.directory,root=self.root,start='2026-01-01',end='2026-10-23')
        with (self.directory/'input.json.gz').open('ab') as h:h.write(b'changed')
        with self.assertRaises(ValueError):self.evaluate()

    def test_code_changes_cannot_reuse_same_experiment(self):
        self.prepare()
        with patch.object(study,'_sources',return_value={'changed':'0'*64}):
            with self.assertRaises(ValueError):self.evaluate()

    def test_completed_report_tampering_is_detected_without_loading_history(self):
        self.evaluate()
        (self.directory/'report.json').write_text('{}')
        with patch.object(study,'_snapshot',side_effect=AssertionError('Read-only')):
            with self.assertRaises(ValueError):study.load_report(self.directory)

    def test_orphan_replay_with_wrong_input_is_not_blessed_on_resume(self):
        self.prepare()
        study._new(self.directory/'replay.json.gz',dict(records=[],plan_sha256='wrong',input_sha256='wrong'),compressed=True)
        with self.assertRaises(ValueError):self.evaluate()
        self.assertFalse((self.directory/'replay_manifest.json').exists())

    def test_orphan_input_cannot_be_bound_to_different_plan(self):
        self.prepare()
        (self.directory/'input_manifest.json').unlink()
        data=study._read(self.directory/'input.json.gz')
        (self.directory/'input.json.gz').unlink();data['plan_sha256']='wrong'
        study._new(self.directory/'input.json.gz',data,compressed=True)
        with self.assertRaises(ValueError):self.prepare()


if __name__=='__main__':unittest.main()
