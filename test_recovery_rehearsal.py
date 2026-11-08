from contextlib import closing, redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import causal_confirmation_store as causal
import fixture_store
import local_recovery as recovery
import prediction_journal as journal
import recovery_rehearsal as rehearsal
from test_api_football import fixture


class RecoveryRehearsalTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.directory=Path(temp.name);self.root=self.directory/'project';self.data=self.root/'data/api_football'
        self.data.mkdir(parents=True);self.point=self.directory/'point'
        p=patch.object(api,'load_key',side_effect=AssertionError('No credentials'));self.key=p.start();self.addCleanup(p.stop)
        p=patch.object(api.ApiFootballClient,'get',side_effect=AssertionError('No API'));self.network=p.start();self.addCleanup(p.stop)
        p=patch('cloud_storage.configured',return_value=False);p.start();self.addCleanup(p.stop)

    def seed(self):
        api.save_fixtures([fixture()],self.data/'fixtures.json')
        fixture_store.migrate_json(self.data/'fixtures.json')
        with closing(journal._connect(self.data/'predictions.sqlite3',create=True)) as con:con.commit()
        with closing(causal._connect(self.data,create=True)):pass

    def snapshot(self):
        # SQLite can create empty WAL/shared-memory sidecars on a read-only
        # open. Compare authoritative files, not these transient lock artifacts.
        return {p.relative_to(self.root).as_posix():recovery.fingerprint(p) for p in self.root.rglob('*')
                if p.is_file() and not p.name.endswith(('-wal','-shm'))}

    def test_real_app_readers_use_isolated_copies_and_keep_live_and_backup_intact(self):
        self.seed();before=self.snapshot();recovery.create_recovery_point(self.root,self.point)
        original={p:recovery.fingerprint(p) for p in (self.point/'files').rglob('*') if p.is_file()}
        report=rehearsal.rehearse(self.point)
        self.assertEqual(report['state'],'passed')
        rows={r['component']:r for r in report['domain_checks']}
        self.assertEqual(rows['fixtures']['items'],1)
        self.assertEqual(rows['fixtures']['sample_items'],1)
        self.assertEqual(rows['forecast_journal']['items'],0)
        self.assertEqual(rows['causal_confirmation']['protocols'],0)
        self.assertFalse(report['production_restored']);self.assertFalse(report['atomic_across_databases'])
        self.assertEqual(before,self.snapshot())
        self.assertTrue(all(recovery.fingerprint(p)==value for p,value in original.items()))
        self.assertFalse(list(self.point.glob('application-restore-*')))
        self.key.assert_not_called();self.network.assert_not_called()

    def test_missing_required_store_is_incomplete_not_a_successful_empty_application(self):
        (self.root/'README.md').write_text('example')
        recovery.create_recovery_point(self.root,self.point)
        result=rehearsal.rehearse(self.point)
        self.assertEqual(result['state'],'incomplete')
        self.assertEqual(sum(r['state']=='missing' and r['required'] for r in result['domain_checks']),3)

    def test_integrity_ok_but_bad_forecast_digest_fails_domain_read_without_details(self):
        self.seed()
        with closing(sqlite3.connect(self.data/'predictions.sqlite3')) as con:
            con.execute('INSERT INTO predictions VALUES (?,?,?,?)',('example','2026-11-02T00:00:00Z','{}','bad-secret-text'));con.commit()
        recovery.create_recovery_point(self.root,self.point)
        result=rehearsal.rehearse(self.point)
        self.assertEqual(result['state'],'failed')
        failed=next(r for r in result['domain_checks'] if r['component']=='forecast_journal')
        self.assertEqual(failed['state'],'failed')
        self.assertNotIn('bad-secret-text',json.dumps(result))
        self.assertNotIn('items',failed)

    def test_tampered_file_stops_before_application_read(self):
        self.seed();recovery.create_recovery_point(self.root,self.point)
        (self.point/'files/data/api_football/predictions.sqlite3').write_bytes(b'broken')
        with patch.object(rehearsal,'_domain',side_effect=AssertionError('No domain read')) as read:
            with self.assertRaises(ValueError):rehearsal.rehearse(self.point)
        read.assert_not_called()

    def test_changed_manifest_is_not_reused_after_verification(self):
        self.seed();recovery.create_recovery_point(self.root,self.point)
        original=recovery.verify_recovery_point
        def changed(folder):
            value=original(folder);path=folder/'manifest.json';path.write_text(path.read_text()+'\n');return value
        with patch.object(recovery,'verify_recovery_point',side_effect=changed):
            with self.assertRaisesRegex(rehearsal.RehearsalError,'manifeste a changé'):
                rehearsal.rehearse(self.point)

    def test_known_private_paths_in_legacy_point_are_not_restored_by_drill(self):
        (self.root/'README.md').write_text('example');recovery.create_recovery_point(self.root,self.point)
        private=self.point/'files/data/api_key.json';private.parent.mkdir(parents=True,exist_ok=True);private.write_text('SYNTHETIC')
        path=self.point/'manifest.json';value=json.loads(path.read_text())
        value['files'].append(dict(path='data/api_key.json',kind='file',bytes=private.stat().st_size,sha256=recovery.fingerprint(private)))
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(rehearsal.RehearsalError,'chemins privés'):
            rehearsal.rehearse(self.point)
        self.assertFalse(list(self.point.glob('application-restore-*')))


class RecoveryRehearsalCliTests(unittest.TestCase):
    def test_existing_output_prevents_loading_and_is_never_overwritten(self):
        from scripts.rehearse_local_recovery import main
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'report.json';target.write_text('original')
            with patch('recovery_rehearsal.rehearse') as run,redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                main(['--point',folder,'--output',str(target)])
            run.assert_not_called();self.assertEqual(target.read_text(),'original')

    def test_failed_domain_report_is_written_with_nonzero_exit_status(self):
        from scripts.rehearse_local_recovery import main
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'report.json'
            with patch('recovery_rehearsal.rehearse',return_value=dict(state='failed',domain_checks=[])),redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--point',folder,'--output',str(target)]),1)
            self.assertEqual(json.loads(target.read_text())['state'],'failed')

    def test_sqlite_error_is_sanitized_and_no_success_report_is_written(self):
        from scripts.rehearse_local_recovery import main
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'report.json';errors=io.StringIO()
            with patch('recovery_rehearsal.rehearse',side_effect=sqlite3.DatabaseError('private-dsn')), \
                 redirect_stderr(errors),self.assertRaises(SystemExit) as raised:
                main(['--point',folder,'--output',str(target)])
            self.assertEqual(raised.exception.code,2)
            self.assertNotIn('private-dsn',errors.getvalue());self.assertFalse(target.exists())


if __name__=='__main__':unittest.main()
