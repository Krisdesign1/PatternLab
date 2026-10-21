from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import daily_evaluation_store as store
from test_daily_analysis_engine import history, future


class DailyEvaluationStoreTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.root=Path(folder.name)
        self.raw=history()+future()
        (self.root/'fixtures.json').write_text(json.dumps(dict(version=1,fixtures=self.raw,updated_at='2026-10-21T08:00:00Z')))
        self.clock=patch.object(store,'_utc_now',return_value=datetime(2026,10,21,10,tzinfo=timezone.utc))
        self.clock.start();self.addCleanup(self.clock.stop)
        self.result=dict(version='synthetic',paired_records=[],protocol={},inventory={},warnings=[])

    def run_audit(self):
        return store.run_audit(root=self.root,today='2026-10-21',start='2026-10-01')

    def test_input_and_protocol_exist_before_calculation_and_finished_run_reuses_cache(self):
        calls=[]
        def evaluate(raw,**kwargs):
            manifest=next((self.root/store.RUNS_NAME).glob('*/manifest.json'))
            self.assertTrue((manifest.parent/'input.json').exists())
            calls.append(raw)
            return self.result
        with patch.object(store,'_evaluate',side_effect=evaluate):
            first=self.run_audit();second=self.run_audit()
        self.assertEqual(first,second)
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0],self.raw)
        self.assertEqual(store.load_latest(self.root),first)
        directory=Path(first['report_path']).parent
        self.assertTrue((directory/'review.md').is_file())
        self.assertTrue((directory/'completed.json').is_file())

    def test_readonly_absence_and_invalid_future_dates_do_not_create_runs(self):
        absent=self.root/'absent'
        self.assertIsNone(store.load_latest(absent));self.assertFalse(absent.exists())
        for kwargs in [dict(today='2026-10-22'),dict(end='2026-10-22'),dict(start='2026-10-21')]:
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):store.run_audit(root=self.root,**kwargs)
        self.assertFalse((self.root/store.RUNS_NAME).exists())

    def test_report_and_input_tampering_are_detected(self):
        with patch.object(store,'_evaluate',return_value=self.result):report=self.run_audit()
        path=Path(report['report_path'])
        path.write_text('{}')
        with self.assertRaises(ValueError):store.load_latest(self.root)

    def test_changed_source_produces_new_run_without_replacing_frozen_inputs(self):
        with patch.object(store,'_evaluate',return_value=self.result):first=self.run_audit()
        old=Path(first['report_path']).parent/'input.json';original=old.read_bytes()
        payload=json.loads((self.root/'fixtures.json').read_text());payload['fixtures']=payload['fixtures'][:-1]
        (self.root/'fixtures.json').write_text(json.dumps(payload))
        with patch.object(store,'_evaluate',return_value=self.result):second=self.run_audit()
        self.assertNotEqual(first['run_id'],second['run_id'])
        self.assertEqual(old.read_bytes(),original)

    def test_manifest_protocol_tampering_is_detected_on_readonly_load(self):
        with patch.object(store,'_evaluate',return_value=self.result):report=self.run_audit()
        path=Path(report['report_path']).parent/'manifest.json'
        value=json.loads(path.read_text());value['protocol']['sizes']=[2]
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):store.load_latest(self.root)

    def test_evaluator_input_mutation_cannot_change_frozen_snapshot(self):
        def evaluate(raw,**kwargs):
            raw[0]['fixture']['id']=1
            return self.result
        with patch.object(store,'_evaluate',side_effect=evaluate):report=self.run_audit()
        frozen=json.loads((Path(report['report_path']).parent/'input.json').read_text())
        self.assertEqual(frozen['fixtures'],self.raw)


if __name__=='__main__':unittest.main()
