import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('system_installer',Path(__file__).parents[1]/'install_system_proposals.py')
installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)

class ArchiveIntegrityTests(unittest.TestCase):
    def test_same_archive_passes_then_rejects_timestamp_digest_or_payload_changes(self):
        for column in ('created_at','digest','payload'):
            with self.subTest(column=column),tempfile.TemporaryDirectory() as name:
                root=Path(name);installer.ROOT=root
                database=root/'data/api_football/predictions.sqlite3';database.parent.mkdir(parents=True)
                with sqlite3.connect(database) as con:
                    con.execute('CREATE TABLE predictions(id TEXT,created_at TEXT,digest TEXT,payload TEXT)')
                    con.execute('INSERT INTO predictions VALUES(?,?,?,?)',('one','before','digest','{}'))
                backup=root/'backup';backup.mkdir()
                previous=installer.archive_snapshot(backup)
                installer.require_legacy_unchanged(previous)
                with sqlite3.connect(database) as con:
                    con.execute(f'UPDATE predictions SET {column}=?',('changed',))
                with self.assertRaises(RuntimeError):installer.require_legacy_unchanged(previous)

    def test_worker_append_does_not_change_existing_forecasts(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);installer.ROOT=root
            database=root/'data/api_football/predictions.sqlite3';database.parent.mkdir(parents=True)
            with sqlite3.connect(database) as con:
                con.execute('CREATE TABLE predictions(id TEXT,created_at TEXT,digest TEXT,payload TEXT)')
                con.execute('INSERT INTO predictions VALUES(?,?,?,?)',('one','before','digest','{}'))
            backup=root/'backup';backup.mkdir();previous=installer.archive_snapshot(backup)
            with sqlite3.connect(database) as con:
                con.execute('INSERT INTO predictions VALUES(?,?,?,?)',('two','later','next','{}'))
            installer.require_legacy_unchanged(previous)

if __name__=='__main__':unittest.main()
