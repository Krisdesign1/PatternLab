"""Exact-scope cleanup using temporary independent copies; no real server."""
from contextlib import ExitStack
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('prune_initial_staging',
    Path(__file__).parent/'deployment/linux/prune_initial_staging.py')
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


def dump(path, value):
    path.write_text(json.dumps(value, sort_keys=True))


class InitialStagingCleanupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.live = self.root/'live'
        self.live.mkdir(mode=0o700)
        (self.live/'production-data').write_bytes(b'current-data')
        self.cutover = self.root/'cutover'
        self.cutover.mkdir(mode=0o700)
        (self.cutover/'rollback').write_bytes(b'retained')
        self.targets = (self.root/'incoming'/'stage-first', self.root/'incoming'/'stage-final',
                        self.root/'qualification'/'replay-first', self.root/'qualification'/'replay-final')
        anchors = {}
        for target in self.targets:
            target.parent.mkdir(mode=0o700, exist_ok=True)
            target.mkdir(mode=0o700)
            if target in self.targets[:2]:
                (target/'files').mkdir(mode=0o700)
                (target/'files'/'data.bin').write_bytes(b'disposable-copy')
                (target/'files'/'source.py').write_text('OLD_SOURCE = True\n')
                dump(target/'manifest.json', dict(state='complete',files=[dict(path='data.bin',bytes=15)]))
                dump(target/'linux-bundle-manifest.json', dict(state='complete',
                     recovery_manifest_sha256=cleanup.digest(target/'manifest.json'),
                     files=[dict(path='source.py',bytes=(target/'files'/'source.py').stat().st_size)]))
                dump(target/'verification.json', dict(state='verified_received'))
                anchors[target] = cleanup.digest(target/'linux-bundle-manifest.json')
            else:
                (target/'test-data').write_bytes(b'discardable')
                dump(target/'.linux-replay-sandbox.json', dict(
                    version='patternlab-linux-frozen-replay-sandbox-v1', project_root=str(target),
                    disposable=True, production=False))
        backups = self.root/'backups'
        backups.mkdir(mode=0o700)
        self.first = backups/'manifest.json'
        dump(self.first, dict(state='complete',files=[]))
        self.daily = backups/'daily.tar.gz'
        self.daily.write_bytes(b'retained-archive')
        self.daily_sha = cleanup.digest(self.daily)
        dump(Path(str(self.daily)+'.verified.json'), dict(state='verified',archive_sha256=self.daily_sha,
             point_name=self.daily.name,databases_restored=14))
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.proc = self.root/'proc'
        self.proc.mkdir()
        values = dict(LIVE=self.live, TARGETS=self.targets, STAGE_ANCHORS=anchors,
                      FIRST_MANIFEST=self.first,FIRST_SHA=cleanup.digest(self.first),
                      DAILY_ARCHIVE=self.daily,DAILY_SHA=self.daily_sha,
                      ROOT_OWNER=os.getuid(), MAINTENANCE=self.root/'maintenance',CONFIG_ROOTS=(),
                      PROC_ROOT=self.proc)
        for name,value in values.items():
            self.stack.enter_context(patch.object(cleanup,name,value))
        self.stack.enter_context(patch.object(cleanup.os,'geteuid',return_value=0))
        self.protected = self.stack.enter_context(patch.object(cleanup,'protected_state',
                         return_value={'frozen':{'candidate_unchanged':True},'sha256':{'live.py':'same'}}))
        self.references = self.stack.enter_context(patch.object(cleanup,'active_references',return_value=[]))

    def test_audit_is_read_only_and_binds_all_four_trees(self):
        plan, fingerprint = cleanup.audit()
        self.assertEqual(set(plan['targets']),set(map(str,self.targets)))
        self.assertEqual(fingerprint,cleanup.fingerprint(plan))
        self.assertTrue(all(path.exists() for path in self.targets))
        self.assertFalse(cleanup.MAINTENANCE.exists())

    def test_apply_removes_only_four_roots_preserving_evidence_and_live_backups(self):
        _, fingerprint = cleanup.audit()
        receipt = cleanup.apply(fingerprint)
        self.assertEqual(receipt['state'],'initial_staging_retired')
        self.assertEqual(receipt['removed'],list(map(str,self.targets)))
        self.assertTrue(all(not path.exists() for path in self.targets))
        self.assertEqual((self.live/'production-data').read_bytes(),b'current-data')
        self.assertEqual(self.daily.read_bytes(),b'retained-archive')
        self.assertEqual((self.cutover/'rollback').read_bytes(),b'retained')
        folder = Path(receipt['receipt_directory'])
        self.assertEqual(len(list((folder/'evidence').rglob('*.json'))),8)
        self.assertTrue((folder/'plan.json').exists())
        self.assertFalse(receipt['services_restarted'])
        self.assertFalse(receipt['backups_removed'])

    def test_changed_inventory_blocks_apply_before_any_removal(self):
        _, fingerprint = cleanup.audit()
        (self.targets[2]/'test-data').write_bytes(b'changed')
        with self.assertRaisesRegex(RuntimeError,'plan changed'):
            cleanup.apply(fingerprint)
        self.assertTrue(all(path.exists() for path in self.targets))

    def test_uninventoried_stage_file_blocks_even_if_it_is_small(self):
        (self.targets[0]/'files'/'unique').write_bytes(b'unique')
        with self.assertRaisesRegex(RuntimeError,'payload differs'):
            cleanup.audit()

    def test_stage_anchor_and_replay_marker_are_mandatory(self):
        self.assertEqual(len(cleanup.audit()[0]['targets']),4)
        with patch.object(cleanup,'STAGE_ANCHORS',{**cleanup.STAGE_ANCHORS,self.targets[0]:'0'*64}), \
                self.assertRaisesRegex(RuntimeError,'anchor mismatch'):
            cleanup.audit()
        path = self.targets[2]/'.linux-replay-sandbox.json'
        marker=json.loads(path.read_text()); marker['production']=True; dump(path,marker)
        with self.assertRaisesRegex(RuntimeError,'accepted disposable'):
            cleanup.audit()

    def test_missing_backup_verification_blocks_removal(self):
        self.daily.write_bytes(b'changed-backup')
        with self.assertRaisesRegex(RuntimeError,'backup archive mismatch'):
            cleanup.audit()
        self.assertTrue(all(path.exists() for path in self.targets))

    def test_symlink_in_target_or_to_live_is_never_followed(self):
        (self.targets[2]/'live-link').symlink_to(self.live,target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError,'Symlink inside'):
            cleanup.audit()
        self.assertEqual((self.live/'production-data').read_bytes(),b'current-data')

    def test_active_or_configured_reference_blocks_removal(self):
        self.references.return_value=[{'kind':'process_path','pid':'123'}]
        with self.assertRaisesRegex(RuntimeError,'active/configured'):
            cleanup.audit()
        self.assertTrue(all(path.exists() for path in self.targets))

    def test_newly_active_reference_after_plan_persistence_blocks_removal(self):
        _, fingerprint = cleanup.audit()
        self.references.side_effect=[[],[{'kind':'process_path','pid':'123'}]]
        with self.assertRaisesRegex(RuntimeError,'became active'):
            cleanup.apply(fingerprint)
        self.assertTrue(all(path.exists() for path in self.targets))

    def test_runtime_checks_detect_configured_references_mounts_and_live_symlinks(self):
        config = self.root/'config'
        config.mkdir()
        unit=config/'old-worker.service'
        unit.write_text('WorkingDirectory='+str(self.targets[0]))
        # The active_references mock is disabled only for the independent scanner.
        with patch.object(cleanup,'mounted_paths',return_value=[]):
            scanner = self.real_scanner
            with patch.object(cleanup,'CONFIG_ROOTS',(config,)):
                self.assertTrue(any(row['kind']=='configured_reference' for row in scanner()))
            unit.unlink()
            (self.live/'link').symlink_to(self.targets[2],target_is_directory=True)
            self.assertTrue(any(row['kind']=='live_symlink' for row in scanner()))
        with patch.object(cleanup,'mounted_paths',return_value=[str(self.targets[1])]):
            self.assertTrue(any(row['kind']=='mount' for row in self.real_scanner()))

    def test_scanner_detects_process_descriptors_maps_and_command_references(self):
        proc = self.proc/'123'
        (proc/'fd').mkdir(parents=True)
        (proc/'fd'/'4').symlink_to(self.targets[0]/'files'/'data.bin')
        (proc/'maps').write_text('0000-1000 r--p 0000 01:01 12 '+str(self.targets[2]/'test-data')+'\n')
        (proc/'cmdline').write_bytes(b'python\0'+str(self.targets[3]/'test-data').encode()+b'\0')
        with patch.object(cleanup,'mounted_paths',return_value=[]):
            hits=self.real_scanner()
        self.assertTrue(any(row['kind']=='process_path' and row['pid']=='123' for row in hits))
        self.assertTrue(any(row['kind']=='process_command' and row['pid']=='123' for row in hits))

    real_scanner = staticmethod(cleanup.active_references)


if __name__ == '__main__':
    unittest.main()
