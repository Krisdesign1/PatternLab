import copy
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import daily_selection_study as study
import daily_selection_replay as replay
import daily_selection_statistics as statistics


class StudyStorageTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.root=Path(folder.name)
        (self.root/'fixtures.json').write_text(json.dumps(dict(version=1,fixtures=[])))
        self.pre=[dict(id='validation',phase='selection')]
        self.test=[dict(id='reserved',phase='test')]
        self.rules=dict(by_size=[],fitted_input_sha256='test-fixture')
        def fake_replay(raw, *, today, protocol, progress, before_test):
            directories=list((self.root/study.RUNS_NAME).glob('*/manifest.json'))
            self.assertEqual(len(directories),1)
            directory=directories[0].parent
            self.assertTrue((directory/'input.json').is_file())
            self.assertFalse((directory/'rules.json').exists())
            before_test(copy.deepcopy(self.pre))
            self.assertTrue((directory/'rules.json').is_file())
            envelope=json.loads((directory/'rules.json').read_text())
            self.assertTrue(envelope['chosen_before_test'])
            return dict(records=copy.deepcopy(self.pre+self.test),inventory={'raw_fixtures':0},
                        method_details=[],warnings=[])
        self.fit=patch.object(statistics,'fit_rules',return_value=self.rules).start()
        self.addCleanup(patch.stopall)
        self.evaluate=patch.object(statistics,'evaluate_rules',return_value={'by_size':[]}).start()
        self.replay=patch.object(replay,'replay',side_effect=fake_replay).start()

    def run_study(self):
        return study.run_study(root=self.root,today=date(2026,10,22))

    def test_frozen_input_protocol_and_rules_precede_test_and_completed_run_is_reused(self):
        report=self.run_study()
        self.fit.assert_called_once()
        self.assertEqual(self.fit.call_args.args[0],self.pre)
        self.assertEqual(self.evaluate.call_args.args[0],self.pre+self.test)
        self.assertEqual(self.evaluate.call_args.args[1],self.rules)
        directory=Path(report['report_path']).parent
        frozen={path.name:sha256(path.read_bytes()).hexdigest() for path in directory.iterdir() if path.is_file()}
        self.assertEqual(study.load_latest(self.root),report)
        self.assertEqual(self.run_study(),report)
        self.replay.assert_called_once()
        self.assertEqual(frozen,{path.name:sha256(path.read_bytes()).hexdigest() for path in directory.iterdir() if path.is_file()})

    def test_loading_absent_is_readonly_and_never_runs_models(self):
        self.assertIsNone(study.load_latest(self.root))
        self.assertFalse((self.root/study.RUNS_NAME).exists())
        self.replay.assert_not_called();self.fit.assert_not_called();self.evaluate.assert_not_called()

    def test_changed_rules_report_input_or_manifest_are_rejected(self):
        report=self.run_study();directory=Path(report['report_path']).parent
        for name in ('rules.json','report.json','input.json','manifest.json'):
            original=(directory/name).read_text()
            value=json.loads(original)
            if name=='manifest.json':value['protocol']['production_changed']=True
            else:value['unexpected_mutation']=True
            (directory/name).write_text(json.dumps(value))
            with self.subTest(name=name),self.assertRaises(study.StudyError):study.load_latest(self.root)
            (directory/name).write_text(original)
        self.assertEqual(study.load_latest(self.root),report)

    def test_callback_must_exclude_test_and_test_cannot_run_without_rule_freeze(self):
        self.replay.side_effect=lambda raw,**kw: kw['before_test'](self.test)
        with self.assertRaises(study.StudyError):self.run_study()
        self.fit.assert_not_called();self.evaluate.assert_not_called()
        self.assertIsNone(study.load_latest(self.root))

    def test_incomplete_replay_never_publishes_latest(self):
        self.replay.side_effect=lambda raw,**kw: dict(records=[],inventory={})
        with self.assertRaises(study.StudyError):self.run_study()
        self.assertFalse((self.root/study.RUNS_NAME/'latest.json').exists())

    def test_test_end_cannot_be_future(self):
        with self.assertRaises(ValueError):
            study.run_study(root=self.root,today=date(2026,10,22),end=date(2026,10,23))
        self.replay.assert_not_called()


if __name__=='__main__':unittest.main()
