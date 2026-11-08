from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import local_recovery as recovery
from scripts import prepare_linux_bundle as bundle


class LinuxBundleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / 'project'
        (self.root / 'data').mkdir(parents=True)
        (self.root / 'audit/archive').mkdir(parents=True)
        (self.root / 'deployment/linux').mkdir(parents=True)
        (self.root / 'deployment/linux/run_worker.py').write_text('print("fixture only")\n')
        self.destination = self.root / '.local_backups/aws-stage-fixture'

    def stage(self):
        return bundle.prepare_linux_bundle(self.root, self.destination)

    def rewrite(self, transform):
        index = self.destination / bundle.MANIFEST_NAME
        value = json.loads(index.read_text())
        transform(value)
        index.write_text(json.dumps(value))

    def test_current_untracked_source_data_audit_and_kit_are_copied_without_mac_environment(self):
        (self.root / 'new_untracked.py').write_text('VALUE = 42\n')
        (self.root / 'data/state.json').write_text('{"fixture": true}')
        (self.root / 'audit/archive/saved_source.py').write_text('ARCHIVED = True\n')
        (self.root / 'audit/receipt.json').write_text('{"state": "fixture"}')
        (self.root / 'deployment/linux/secrets.toml.example').write_text('API_FOOTBALL_KEY = ""\n')
        (self.root / 'deployment/linux/.python-version').write_text('3.12.10\n')
        archived_log = self.root / 'audit/PATTERNLAB2_LOT11_SOURCES_2026-11-02/tests.log'
        archived_log.parent.mkdir()
        archived_log.write_text('Ran 1 test in 0.001s\n\nOK\n')
        (self.root / '.venv').mkdir()
        (self.root / '.venv/mac-native.so').write_bytes(b'not portable')
        (self.root / '.streamlit').mkdir()
        (self.root / '.streamlit/config.toml').write_text('[runner]\nfastReruns=false\n')
        (self.root / '.streamlit/secrets.toml').write_text('SYNTHETIC PRIVATE')
        report = self.stage()
        self.assertEqual(report['state'], 'verified_local_staging')
        for name in ('new_untracked.py', 'data/state.json', 'audit/archive/saved_source.py',
                     'audit/receipt.json', 'deployment/linux/run_worker.py',
                     'audit/PATTERNLAB2_LOT11_SOURCES_2026-11-02/tests.log',
                     'deployment/linux/secrets.toml.example', 'deployment/linux/.python-version',
                     '.streamlit/config.toml'):
            self.assertEqual((self.destination / 'files' / name).read_bytes(), (self.root / name).read_bytes())
        self.assertFalse((self.destination / 'files/.venv').exists())
        self.assertFalse((self.destination / 'files/.streamlit/secrets.toml').exists())
        self.assertFalse(report['embedded_secret_content_audited'])
        self.assertFalse(report['mac_environment_copied'])
        self.assertFalse(report['upload_performed'])
        self.assertEqual(bundle.verify_linux_bundle(self.destination), report)

    def test_online_backup_retains_committed_wal_and_never_commits_source_transaction(self):
        database = self.root / 'data/fixtures.sqlite3'
        connection = sqlite3.connect(database)
        self.addCleanup(connection.close)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE fixtures(id INTEGER PRIMARY KEY)')
        connection.execute('INSERT INTO fixtures VALUES(1)')
        connection.commit()
        connection.execute('INSERT INTO fixtures VALUES(2)')
        source_before = database.read_bytes()
        report = self.stage()
        self.assertEqual(database.read_bytes(), source_before)
        self.assertTrue(connection.in_transaction)
        self.assertEqual(connection.execute('SELECT id FROM fixtures').fetchall(), [(1,), (2,)])
        with closing(sqlite3.connect(self.destination / 'files/data/fixtures.sqlite3')) as restored:
            self.assertEqual(restored.execute('SELECT id FROM fixtures').fetchall(), [(1,)])
        self.assertEqual(report['databases_restored'], ['data/fixtures.sqlite3'])
        self.assertFalse(report['atomic_across_databases'])

    def test_private_names_links_caches_logs_and_unknown_suffixes_are_excluded(self):
        private = ('audit/api_key.json', 'audit/secrets/receipt.json', 'audit/.hidden.json',
            'audit/archive/tests.log', 'audit/logs/receipt.json', 'audit/cache/receipt.json',
            'audit/__pycache__/copy.py', 'audit/file.bin', 'deployment/linux/secrets.toml',
            'deployment/linux/private_credentials.json', 'deployment/linux/token.example')
        for name in private:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('SYNTHETIC PRIVATE')
        (self.root / 'audit/public.json').write_text('{}')
        (self.root / 'audit/link.json').symlink_to(self.root / 'audit/api_key.json')
        (self.root / 'audit/linked_directory').symlink_to(self.root / 'audit/secrets', target_is_directory=True)
        selected = {path.as_posix() for path in bundle.supplemental_sources(self.root)}
        self.assertEqual(selected, {'audit/public.json', 'deployment/linux/run_worker.py'})
        self.stage()
        for name in (*private, 'audit/link.json', 'audit/linked_directory/receipt.json'):
            self.assertFalse((self.destination / 'files' / name).exists())

    def test_tampering_and_replaced_base_inventory_are_rejected(self):
        self.stage()
        copied = self.destination / 'files/deployment/linux/run_worker.py'
        copied.write_text('changed')
        with self.assertRaises(ValueError):
            bundle.verify_linux_bundle(self.destination)
        copied.write_bytes((self.root / 'deployment/linux/run_worker.py').read_bytes())
        with (self.destination / 'manifest.json').open('a') as handle:
            handle.write('\n')
        with self.assertRaisesRegex(ValueError, 'reprise a changé'):
            bundle.verify_linux_bundle(self.destination)

    def test_manifest_traversal_duplicate_private_paths_and_symlink_parent_are_rejected(self):
        self.stage()
        index = self.destination / bundle.MANIFEST_NAME
        original = index.read_text()
        for path in ('../escape.json', '/tmp/escape.json', 'audit//receipt.json',
                     'audit/secret.json', 'audit\\receipt.json', 'audit\x00receipt.json'):
            with self.subTest(path=repr(path)):
                index.write_text(original)
                self.rewrite(lambda value: value['files'][0].update(path=path))
                with self.assertRaises(ValueError):
                    bundle.verify_linux_bundle(self.destination)
        index.write_text(original)
        self.rewrite(lambda value: value['files'].append(value['files'][0].copy()))
        with self.assertRaises(ValueError):
            bundle.verify_linux_bundle(self.destination)
        index.write_text(original)
        directory = self.destination / 'files/deployment/linux'
        directory.rename(directory.with_name('saved-kit'))
        directory.symlink_to(self.root / 'deployment/linux', target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'lien'):
            bundle.verify_linux_bundle(self.destination)

    def test_existing_destination_and_active_tree_destinations_are_preserved(self):
        (self.root / 'data/state.json').write_text('original')
        for destination in (self.root, self.root / 'data/new', self.root / 'audit/new',
                            self.root / 'deployment/linux/new'):
            with self.subTest(destination=str(destination)):
                with self.assertRaises((ValueError, FileExistsError)):
                    bundle.prepare_linux_bundle(self.root, destination)
        self.stage()
        with self.assertRaises(FileExistsError):
            self.stage()
        self.assertEqual((self.root / 'data/state.json').read_text(), 'original')

    def test_failed_extra_copy_leaves_overall_stage_incomplete(self):
        source = self.root / 'audit/public.json'
        source.write_text('{}')
        original_copy = bundle.shutil.copy2
        def fail_public_copy(origin, target, *args, **kwargs):
            if Path(origin) == source:
                raise OSError('fixture disk full')
            return original_copy(origin, target, *args, **kwargs)
        with patch.object(bundle.shutil, 'copy2', side_effect=fail_public_copy):
            with self.assertRaises(OSError):
                self.stage()
        manifest = json.loads((self.destination / bundle.MANIFEST_NAME).read_text())
        self.assertEqual(manifest['state'], 'incomplete')
        with self.assertRaises(ValueError):
            bundle.verify_linux_bundle(self.destination)
        self.assertEqual(source.read_text(), '{}')

    def test_destination_parent_link_cannot_redirect_the_snapshot(self):
        outside = self.root.parent / 'outside'
        outside.mkdir()
        (self.root / '.local_backups').symlink_to(outside, target_is_directory=True)
        with patch.object(recovery, 'create_recovery_point') as create:
            with self.assertRaisesRegex(ValueError, 'lien'):
                self.stage()
        create.assert_not_called()
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
