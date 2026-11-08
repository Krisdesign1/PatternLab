"""New archives attest their implementation without rewriting legacy evidence."""
from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import high_probability_archive as archive
import probability_implementation as implementation
from test_high_probability_archive import report
from test_high_probability_engine import NOW


def fingerprints(digit='a'):
    return {name: digit*64 for name in implementation.SOURCE_FILES}


class ProbabilityImplementationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        for name in implementation.SOURCE_FILES:
            (self.root/name).write_text('# source '+name+'\n')
        patcher = patch.object(implementation, 'ROOT', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_hashes_exact_named_file_bytes_without_modifying_sources(self):
        before = {p.name:p.read_bytes() for p in self.root.iterdir()}
        expected = {name:sha256(content).hexdigest() for name,content in before.items()}
        self.assertEqual(implementation.code_hashes(), expected)
        self.assertEqual({p.name:p.read_bytes() for p in self.root.iterdir()}, before)
        self.assertEqual(implementation.validate_hashes(expected), expected)

    def test_source_changes_are_observed_and_returned_dicts_are_independent(self):
        first = implementation.code_hashes()
        exposed = implementation.code_hashes()
        exposed[implementation.SOURCE_FILES[0]] = '0'*64
        self.assertEqual(implementation.code_hashes(), first)
        name = implementation.SOURCE_FILES[0]
        (self.root/name).write_text('# revised source\n')
        changed = implementation.code_hashes()
        self.assertNotEqual(changed[name], first[name])
        self.assertEqual({k:v for k,v in changed.items() if k != name},
                         {k:v for k,v in first.items() if k != name})

    def test_missing_source_cannot_be_attested(self):
        (self.root/implementation.SOURCE_FILES[0]).unlink()
        with self.assertRaises(OSError):
            implementation.code_hashes()

    def test_validation_requires_complete_current_or_legacy_canonical_sha256_values(self):
        good = fingerprints()
        invalid = [None, [], {}, {**good, 'other.py':'a'*64},
                   {k:v for k,v in good.items() if k != implementation.SOURCE_FILES[0]}]
        for value in ('a'*63, 'A'*64, 'z'*64, True, None, 42):
            invalid.append({**good, implementation.SOURCE_FILES[0]:value})
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                implementation.validate_hashes(value)
        returned = implementation.validate_hashes(good)
        returned[implementation.SOURCE_FILES[0]] = 'b'*64
        self.assertEqual(good, fingerprints())

    def test_legacy_four_source_attestation_is_readable_but_cannot_attest_a_new_capture(self):
        old = {name:'a'*64 for name in implementation.LEGACY_SOURCE_FILES}
        self.assertEqual(implementation.validate_hashes(old),old)
        with self.assertRaises(ValueError):
            implementation.validate_hashes(old,current=True)
        self.assertEqual(implementation.validate_hashes(fingerprints(),current=True),fingerprints())


class HighProbabilityArchiveImplementationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)/'archive'
        self.report = report()

    def capture(self, value=None, *, now=NOW):
        return archive.capture(self.report if value is None else value,
                               root=self.root, clock=lambda:now)

    def legacy(self):
        return archive._snapshot(self.report, self.report['rows'][0], NOW)

    def seed(self, value):
        # Seed an old append-only payload as it existed before attestation.
        con = archive._connect(self.root, create=True)
        try:
            payload = archive._json(value)
            con.execute('INSERT INTO forecasts VALUES(?,?,?,?,?,?)',
                (value['id'], value['fixture']['match_id'], value['day'], value['created_at'],
                 payload, sha256(payload.encode()).hexdigest()))
            con.commit()
        finally:
            con.close()

    def test_new_capture_uses_one_own_map_for_entire_batch_and_ignores_caller_maps(self):
        value = deepcopy(self.report)
        value['model_code_sha256'] = {'malicious':'untrusted'}
        for row in value['rows']:
            row['model_code_sha256'] = fingerprints('f')
        original = deepcopy(value)
        with patch.object(implementation, 'code_hashes', return_value=fingerprints()) as hashes:
            self.assertEqual(self.capture(value)['created'], 2)
        hashes.assert_called_once_with()
        self.assertEqual(value, original)
        saved = archive.list_entries(self.root)
        self.assertEqual(len(saved), 2)
        self.assertTrue(all(row['model_code_sha256'] == fingerprints() for row in saved))

    def test_new_capture_rejects_attestation_missing_shared_domain_without_writing(self):
        old = {name:'a'*64 for name in implementation.LEGACY_SOURCE_FILES}
        with patch.object(implementation,'code_hashes',return_value=old):
            with self.assertRaises(archive.ArchiveError):
                self.capture()
        self.assertFalse(self.root.exists())

    def test_legacy_four_source_capture_is_not_rewritten_on_read(self):
        old = self.legacy()
        old['model_code_sha256'] = {name:'a'*64 for name in implementation.LEGACY_SOURCE_FILES}
        old['id'] = archive._identity(old)
        self.seed(old)
        path = archive.database_path(self.root)
        before = path.read_bytes()
        rows = archive.list_entries(self.root)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['model_code_sha256'],old['model_code_sha256'])
        self.assertEqual(path.read_bytes(),before)

    def test_changed_code_appends_distinct_capture_and_preserves_old_payload(self):
        code = self.root.parent/'implementation'
        code.mkdir()
        for name in implementation.SOURCE_FILES:
            (code/name).write_text('# original '+name+'\n')
        with patch.object(implementation, 'ROOT', code):
            self.capture()
            first = archive.list_entries(self.root)
            (code/implementation.SOURCE_FILES[0]).write_text('# changed\n')
            changed = implementation.code_hashes()
            result = self.capture(now=NOW+timedelta(minutes=1))
        self.assertEqual(result, dict(created=2, existing=0, skipped=0))
        after = archive.list_entries(self.root)
        self.assertEqual(len(after), 4)
        for saved in first:
            self.assertIn(saved, after)
            later = next(row for row in after if row.get('previous_snapshot') == saved['id'])
            self.assertEqual(later['probabilities'], saved['probabilities'])
            self.assertEqual(later['baseline'], saved['baseline'])
            self.assertEqual(later['model_code_sha256'], changed)
            self.assertNotEqual(later['model_code_sha256'], saved['model_code_sha256'])
            self.assertNotEqual(later['id'], saved['id'])

    def test_same_attestation_keeps_unchanged_capture_idempotent(self):
        with patch.object(implementation, 'code_hashes', return_value=fingerprints()):
            self.capture()
            saved = archive.list_entries(self.root)
            self.assertEqual(self.capture(now=NOW+timedelta(minutes=1)),
                             dict(created=0, existing=2, skipped=0))
        self.assertEqual(archive.list_entries(self.root), saved)

    def test_legacy_record_without_map_remains_valid_and_read_is_byte_preserving(self):
        old = self.legacy()
        self.assertNotIn('model_code_sha256', old)
        self.assertEqual(archive._validate(old), old)
        self.seed(old)
        path = archive.database_path(self.root)
        before = path.read_bytes()
        with patch.object(implementation, 'code_hashes', side_effect=AssertionError('Reading must not attest old evidence')):
            rows = archive.list_entries(self.root)
        self.assertEqual(len(rows), 1)
        self.assertTrue(all(rows[0][key] == value for key,value in old.items()))
        self.assertNotIn('model_code_sha256', rows[0])
        self.assertEqual(path.read_bytes(), before)

    def test_new_capture_does_not_backfill_fingerprint_into_existing_legacy_record(self):
        old = self.legacy()
        self.seed(old)
        with patch.object(implementation, 'code_hashes', return_value=fingerprints()):
            self.capture(now=NOW+timedelta(minutes=1))
        rows = archive.list_entries(self.root)
        original = next(row for row in rows if row['id'] == old['id'])
        self.assertNotIn('model_code_sha256', original)
        self.assertTrue(all(original[key] == value for key,value in old.items()))
        self.assertTrue(any(row.get('previous_snapshot') == old['id']
                            and row['model_code_sha256'] == fingerprints() for row in rows))

    def test_fingerprint_is_cryptographically_included_in_forecast_identity(self):
        value = self.legacy()
        value['model_code_sha256'] = fingerprints()
        old_identity = value['id']
        self.assertNotEqual(archive._identity(value), old_identity)
        with self.assertRaises(archive.ArchiveError):
            archive._validate(value)
        value['id'] = archive._identity(value)
        self.assertEqual(archive._validate(value), value)
        value['model_code_sha256'][implementation.SOURCE_FILES[0]] = 'b'*64
        with self.assertRaises(archive.ArchiveError):
            archive._validate(value)

    def test_malformed_attestation_is_refused_even_with_matching_identity_and_payload_digest(self):
        for fingerprint in (None, {}, {**fingerprints(), 'unexpected.py':'a'*64},
                            {**fingerprints(), implementation.SOURCE_FILES[0]:'bad'}):
            value = self.legacy()
            value['model_code_sha256'] = fingerprint
            value['id'] = archive._identity(value)
            with self.subTest(fingerprint=fingerprint), self.assertRaises(archive.ArchiveError):
                archive._validate(value)
        self.seed(value)
        with self.assertRaises(archive.ArchiveError):
            archive.list_entries(self.root)

    def test_failed_attestation_refuses_new_capture_before_creating_storage(self):
        for error in (OSError('source unavailable'), ValueError('invalid fingerprint')):
            with patch.object(implementation, 'code_hashes', side_effect=error):
                with self.subTest(error=error), self.assertRaises(archive.ArchiveError):
                    self.capture()
            self.assertFalse(self.root.exists())

    def test_invalid_or_empty_capture_does_not_hash_sources_or_create_storage(self):
        for rows in ([], [dict(personalized=False)]):
            value = deepcopy(self.report)
            value['rows'] = rows
            with patch.object(implementation, 'code_hashes', side_effect=AssertionError('No valid snapshot')):
                self.assertEqual(self.capture(value)['created'], 0)
            self.assertFalse(self.root.exists())


if __name__ == '__main__':
    unittest.main()
