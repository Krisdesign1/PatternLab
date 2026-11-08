"""Execution control checks on disposable files; no API or production data."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from deployment.linux import run_scoped_worker as runner
from deployment.linux import run_guarded_worker as guarded
import automation_worker_scoped as worker


class ScopedRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in (*runner.SOURCES, 'automation_worker.py'):
            path = self.root/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# qualified fixture\n')
        self.manifest = dict(version=runner.VERSION,
            source_sha256={name:self.digest(self.root/name) for name in runner.SOURCES},
            preserved_dependency_sha256={'automation_worker.py':self.digest(self.root/'automation_worker.py')},
            qualification={'tests_passed':True, 'evidence_sha256':'a'*64})
        self.save_manifest()

    def digest(self, path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def save_manifest(self):
        (self.root/runner.MANIFEST).write_text(json.dumps(self.manifest))

    def verify(self):
        return runner.verify_execution(self.root, require_root_owned=False)

    def test_manifest_distinguishes_executed_source_from_registered_dependencies(self):
        result = self.verify()
        self.assertEqual(set(result['source_sha256']), set(runner.SOURCES))
        self.assertFalse(result['actual_orchestrator_matches_registered'])
        self.assertEqual(result['preserved_dependency_sha256'], self.manifest['preserved_dependency_sha256'])

    def test_changed_source_or_dependency_refuses_execution_before_receipt(self):
        for name in ('automation_worker_scoped.py', 'automation_worker.py'):
            with self.subTest(name=name):
                path = self.root/name
                previous = path.read_bytes()
                path.write_text('# changed\n')
                with self.assertRaises(ValueError): self.verify()
                path.write_bytes(previous)
        self.assertFalse((self.root/'data').exists())

    def test_symlink_unqualified_manifest_and_unknown_source_fail_closed(self):
        path = self.root/runner.SOURCES[0]
        path.unlink()
        path.symlink_to(self.root/'automation_worker.py')
        with self.assertRaises(ValueError): self.verify()
        path.unlink(); path.write_text('# qualified fixture\n')
        self.manifest['qualification']['tests_passed'] = False
        self.save_manifest()
        with self.assertRaises(ValueError): self.verify()
        self.manifest['qualification']['tests_passed'] = True
        self.manifest['source_sha256']['surprise.py'] = 'a'*64
        self.save_manifest()
        with self.assertRaises(ValueError): self.verify()

    def test_actual_runner_uses_existing_guard_and_attests_new_worker(self):
        execution = self.verify()
        service, provider = object(), object()
        captured = []
        def old_guard(root, **arguments):
            self.assertEqual(root, self.root)
            self.assertIs(arguments['service'], service)
            self.assertIs(arguments['provider'], provider)
            self.assertEqual(arguments['max_duration_seconds'], 60)
            arguments['worker'].run_cycle(root=self.root/'data/api_football', client=provider)
            captured.append(arguments)
            return {'status':'completed', 'requests':2}
        with patch.object(guarded, 'run_once', side_effect=old_guard), \
             patch.object(worker, 'run_cycle', return_value={'status':'completed'}) as cycle:
            result = runner.run_once(self.root, service=service, provider=provider,
                max_duration_seconds=60, execution=execution)
        self.assertEqual(result['requests'], 2)
        receipt = cycle.call_args.kwargs['execution_io']
        self.assertFalse(receipt['actual_orchestrator_matches_registered'])
        self.assertEqual(receipt['source_sha256'], self.manifest['source_sha256'])
        path = self.root/'data/api_football/worker_executions'/(receipt['id']+'.json')
        self.assertEqual(json.loads(path.read_text()), receipt)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result['execution_io']['receipt_id'], receipt['id'])
        self.assertEqual(len(captured), 1)

    def test_deployment_override_takes_precedence_over_existing_season_intake(self):
        path=Path(__file__).parent/'deployment/linux/install_scoped_worker.py'
        specification=importlib.util.spec_from_file_location('qualified_scoped_installer',path)
        installer=importlib.util.module_from_spec(specification)
        specification.loader.exec_module(installer)
        target=installer.target(installer.DROPIN)
        self.assertGreater(target.name,'season-intake.conf')
        override=(Path(__file__).parent/installer.DROPIN).read_text()
        self.assertIn('ExecStart=\nExecStart=/opt/patternlab/.venv/bin/python /opt/patternlab/deployment/linux/run_scoped_worker.py --once',override)


if __name__ == '__main__':
    unittest.main()
