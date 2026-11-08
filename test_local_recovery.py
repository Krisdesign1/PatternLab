from pathlib import Path
from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from local_recovery import create_recovery_point, source_files, verify_recovery_point
import local_recovery as recovery


class LocalRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'project'
        (self.root / 'data').mkdir(parents=True)
        self.destination = self.root / '.local_backups' / 'initial'

    def test_committed_wal_pages_restored_and_active_database_unchanged(self):
        path = self.root / 'data' / 'journal.sqlite3'
        connection = sqlite3.connect(path)
        self.addCleanup(connection.close)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE forecasts(id INTEGER PRIMARY KEY,p REAL)')
        connection.execute('INSERT INTO forecasts VALUES(1,.55)')
        connection.commit()
        connection.execute('INSERT INTO forecasts VALUES(2,.45)')  # uncommitted, deliberately excluded
        report = create_recovery_point(self.root, self.destination)
        self.assertEqual(report['state'], 'verified')
        self.assertEqual(report['databases_restored'], ['data/journal.sqlite3'])
        with sqlite3.connect(self.destination / 'files/data/journal.sqlite3') as backup:
            self.assertEqual(backup.execute('SELECT * FROM forecasts').fetchall(), [(1,.55)])
        self.assertEqual(connection.execute('SELECT count(*) FROM forecasts').fetchone()[0], 2)
        self.assertFalse(report['atomic_across_databases'])

    def test_secrets_locks_and_links_are_not_copied(self):
        (self.root / '.streamlit').mkdir()
        (self.root / '.streamlit/secrets.toml').write_text('PRIVATE')
        (self.root / '.streamlit/config.toml').write_text('[server]\naddress="127.0.0.1"')
        (self.root / 'data/private_credentials.json').write_text('PRIVATE')
        (self.root / 'data/state.json').write_text('{"done":true}')
        (self.root / 'data/journal.sqlite3-wal').write_text('not a standalone database')
        (self.root / '.env').write_text('PRIVATE')
        (self.root / 'data/link.json').symlink_to(self.root / '.env')
        selected = {p.as_posix() for p in source_files(self.root)}
        self.assertEqual(selected, {'data/state.json','.streamlit/config.toml'})
        create_recovery_point(self.root, self.destination)
        self.assertEqual(verify_recovery_point(self.destination)['files_verified'], 2)

    def test_tampering_and_incomplete_or_overwritten_recovery_points_are_rejected(self):
        (self.root / 'data/state.json').write_text('{}')
        create_recovery_point(self.root, self.destination)
        with self.assertRaises(FileExistsError):
            create_recovery_point(self.root, self.destination)
        (self.destination / 'files/data/state.json').write_text('{"changed":true}')
        with self.assertRaisesRegex(ValueError, 'empreinte'):
            verify_recovery_point(self.destination)
        manifest = self.destination / 'manifest.json'
        value = json.loads(manifest.read_text()); value['state'] = 'incomplete'
        manifest.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'complet'):
            verify_recovery_point(self.destination)

    def test_backup_cannot_be_created_under_active_data(self):
        with self.assertRaises(ValueError):
            create_recovery_point(self.root, self.root / 'data/backup')

    def point_with_database(self, name='journal.sqlite3'):
        path = self.root / 'data' / name
        with closing(sqlite3.connect(path)) as connection:
            connection.execute('CREATE TABLE forecasts(id INTEGER PRIMARY KEY,p REAL)')
            connection.execute('INSERT INTO forecasts VALUES(1,.55)')
            connection.commit()
        create_recovery_point(self.root, self.destination)
        return path

    def rewrite_manifest(self, change):
        path = self.destination / 'manifest.json'
        value = json.loads(path.read_text())
        change(value)
        path.write_text(json.dumps(value))

    def test_uppercase_sqlite_suffix_uses_online_backup_and_retains_committed_wal(self):
        path = self.root / 'data' / 'CAPTURES.SQLITE3'
        with closing(sqlite3.connect(path)) as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('CREATE TABLE forecasts(id INTEGER PRIMARY KEY,p REAL)')
            connection.execute('INSERT INTO forecasts VALUES(1,.55)')
            connection.commit()
            connection.execute('INSERT INTO forecasts VALUES(2,.45)')
            report = create_recovery_point(self.root, self.destination)
            self.assertEqual(report['databases_restored'], ['data/CAPTURES.SQLITE3'])
            with closing(sqlite3.connect(self.destination / 'files/data/CAPTURES.SQLITE3')) as copied:
                self.assertEqual(copied.execute('SELECT * FROM forecasts').fetchall(), [(1,.55)])
            self.assertEqual(connection.execute('SELECT count(*) FROM forecasts').fetchone()[0], 2)

    def test_known_private_names_parent_directories_and_key_bundles_are_excluded(self):
        (self.root/'data/secrets').mkdir()
        for name in ('api_key.json','token.txt','private.pem','service.P12','access-key.toml','key.json'):
            (self.root/'data'/name).write_text('SYNTHETIC PRIVATE')
        (self.root/'data/secrets/state.json').write_text('SYNTHETIC PRIVATE')
        (self.root/'data/monkey.json').write_text('{}')
        (self.root/'data/keyboard.json').write_text('{}')
        (self.root/'api_key_helpers.py').write_text('pass')
        (self.root/'data/.automation_health.SQLITE3').touch()
        self.assertEqual({p.as_posix() for p in source_files(self.root)},
            {'api_key_helpers.py','data/monkey.json','data/keyboard.json','data/.automation_health.SQLITE3'})

    def test_manifest_duplicate_paths_and_wrong_declared_size_are_rejected(self):
        (self.root/'data/state.json').write_text('{}')
        create_recovery_point(self.root, self.destination)
        original=(self.destination/'manifest.json').read_text()
        self.rewrite_manifest(lambda value:value['files'].append(value['files'][0].copy()))
        with self.assertRaisesRegex(ValueError,'répété'):
            verify_recovery_point(self.destination)
        (self.destination/'manifest.json').write_text(original)
        self.rewrite_manifest(lambda value:value['files'][0].update(bytes=99))
        with self.assertRaisesRegex(ValueError,'empreinte'):
            verify_recovery_point(self.destination)

    def test_sqlite_cannot_be_downgraded_to_file_to_skip_database_restoration(self):
        self.point_with_database()
        self.rewrite_manifest(lambda value:(value['files'][0].update(kind='file'),
                                           value['files'][0].pop('database')))
        with self.assertRaisesRegex(ValueError,'manifeste'):
            verify_recovery_point(self.destination)

    def test_invalid_manifest_kinds_hashes_counts_and_paths_are_rejected_before_copy(self):
        self.point_with_database()
        original=(self.destination/'manifest.json').read_text()
        mutations = [dict(kind='unknown'),dict(bytes=True),dict(sha256='not-a-hash'),
                     dict(path='../escape.sqlite3'),dict(path='/tmp/escape.sqlite3'),
                     dict(path='data//journal.sqlite3'),dict(path='data\\journal.sqlite3'),
                     dict(database={'quick_check':'ok','tables':{'forecasts':True},'schema_sha256':'0'*64})]
        for values in mutations:
            with self.subTest(values=values):
                (self.destination/'manifest.json').write_text(original)
                self.rewrite_manifest(lambda value:value['files'][0].update(values))
                with patch.object(recovery.shutil,'copy2',side_effect=AssertionError('No restore for invalid contract')):
                    with self.assertRaises(ValueError):
                        verify_recovery_point(self.destination)

    def test_destination_open_failure_closes_source_and_leaves_incomplete_point(self):
        source=self.root/'data/journal.sqlite3';source.touch()
        fake=Mock()
        with patch.object(recovery,'_open_readonly',return_value=fake), \
             patch.object(recovery.sqlite3,'connect',side_effect=sqlite3.OperationalError('disk full')):
            with self.assertRaises(sqlite3.OperationalError):
                create_recovery_point(self.root,self.destination)
        fake.close.assert_called_once()
        self.assertEqual(json.loads((self.destination/'manifest.json').read_text())['state'],'incomplete')
        self.assertFalse((self.destination/'verification.json').exists())

    def test_file_copy_failure_keeps_incomplete_point_and_active_source(self):
        source=self.root/'data/state.json';source.write_text('{}')
        with patch.object(recovery.shutil,'copy2',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                create_recovery_point(self.root,self.destination)
        self.assertEqual(source.read_text(),'{}')
        self.assertEqual(json.loads((self.destination/'manifest.json').read_text())['state'],'incomplete')
        self.assertFalse((self.destination/'verification.json').exists())

    def test_tampered_restored_copy_is_rejected_before_sqlite_summary(self):
        self.point_with_database()
        original_copy=recovery.shutil.copy2
        def corrupt(source,target,*args,**kwargs):
            result=original_copy(source,target,*args,**kwargs)
            with Path(target).open('ab') as stream:stream.write(b'changed')
            return result
        with patch.object(recovery.shutil,'copy2',side_effect=corrupt):
            with self.assertRaisesRegex(ValueError,'copie restaurée'):
                verify_recovery_point(self.destination)

    def test_valid_legacy_v1_manifest_and_hidden_health_database_still_verify_without_schema_writes(self):
        self.point_with_database('.automation_health.sqlite3')
        self.rewrite_manifest(lambda value:[value.pop(key,None) for key in recovery.SECURITY_SCOPE])
        path=self.destination/'files/data/.automation_health.sqlite3';before=path.read_bytes()
        result=verify_recovery_point(self.destination)
        self.assertEqual(result['databases_restored'],['data/.automation_health.sqlite3'])
        self.assertEqual(path.read_bytes(),before)
        self.assertTrue(result['known_private_paths_excluded'])
        self.assertFalse(result['embedded_secret_content_audited'])
        self.assertNotIn('secrets_included',result)

    def test_symlink_in_backup_cannot_escape_verified_files(self):
        (self.root/'data/state.json').write_text('{}')
        create_recovery_point(self.root,self.destination)
        target=self.destination/'files/data/state.json';target.unlink()
        target.symlink_to(self.root/'data/state.json')
        with self.assertRaisesRegex(ValueError,'Chemin'):
            verify_recovery_point(self.destination)

    def test_single_backslash_and_actual_null_paths_are_rejected_before_io(self):
        self.point_with_database()
        original=(self.destination/'manifest.json').read_text()
        for invalid in ('data'+chr(92)+'journal.sqlite3','data'+chr(0)+'journal.sqlite3'):
            with self.subTest(invalid=repr(invalid)):
                (self.destination/'manifest.json').write_text(original)
                self.rewrite_manifest(lambda value:value['files'][0].update(path=invalid))
                with patch.object(recovery.shutil,'copy2',side_effect=AssertionError('No IO for invalid path')):
                    with self.assertRaisesRegex(ValueError,'Chemin'):
                        verify_recovery_point(self.destination)

    def test_creation_restore_failure_never_publishes_complete_manifest_or_receipt(self):
        path=self.root/'data/journal.sqlite3'
        with closing(sqlite3.connect(path)) as connection:
            connection.execute('CREATE TABLE forecasts(id PRIMARY KEY)')
            connection.execute('INSERT INTO forecasts VALUES(1)')
            connection.commit()
        original_copy=recovery.shutil.copy2
        def fail_restore(source,target,*args,**kwargs):
            if Path(target).name=='restored.sqlite3':
                state=json.loads((self.destination/'manifest.json').read_text())['state']
                self.assertEqual(state,'incomplete')
                raise OSError('restore copy unavailable')
            return original_copy(source,target,*args,**kwargs)
        with patch.object(recovery.shutil,'copy2',side_effect=fail_restore):
            with self.assertRaises(OSError):
                create_recovery_point(self.root,self.destination)
        self.assertEqual(json.loads((self.destination/'manifest.json').read_text())['state'],'incomplete')
        self.assertFalse((self.destination/'verification.json').exists())
        self.assertTrue((self.destination/'files/data/journal.sqlite3').exists())
        with closing(sqlite3.connect(path)) as con:
            self.assertEqual(con.execute('SELECT * FROM forecasts').fetchall(),[(1,)])

    def test_receipt_publication_failure_keeps_inventory_incomplete(self):
        (self.root/'data/state.json').write_text('{}')
        original_write=recovery._write_json_atomic
        def fail_receipt(path,value):
            if path.name=='verification.json':
                path.write_text('partial receipt')
                raise OSError('disk full during verification receipt')
            return original_write(path,value)
        with patch.object(recovery,'_write_json_atomic',side_effect=fail_receipt):
            with self.assertRaises(OSError):
                create_recovery_point(self.root,self.destination)
        self.assertEqual(json.loads((self.destination/'manifest.json').read_text())['state'],'incomplete')
        self.assertFalse((self.destination/'verification.json').exists())
        self.assertEqual((self.destination/'files/data/state.json').read_text(),'{}')

    def test_final_manifest_publication_failure_removes_already_written_receipt(self):
        (self.root/'data/state.json').write_text('{}')
        original_write=recovery._write_json_atomic
        def fail_complete(path,value):
            if path.name=='manifest.json' and value.get('state')=='complete':
                self.assertTrue((self.destination/'verification.json').is_file())
                raise OSError('disk full at final manifest publication')
            return original_write(path,value)
        with patch.object(recovery,'_write_json_atomic',side_effect=fail_complete):
            with self.assertRaises(OSError):
                create_recovery_point(self.root,self.destination)
        self.assertEqual(json.loads((self.destination/'manifest.json').read_text())['state'],'incomplete')
        self.assertFalse((self.destination/'verification.json').exists())


if __name__ == '__main__':
    unittest.main()
