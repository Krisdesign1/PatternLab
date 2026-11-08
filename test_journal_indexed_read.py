"""Date-scoped journal reads preserve verification and current result revisions."""
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import journal_performance
import prediction_journal as journal


class JournalIndexedReadTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'journal.sqlite3'

    def insert(self, identifier, timestamps, *, kind=None, size=None, methods=None):
        payload = dict(source='API-Football', league='39', size=len(timestamps) if size is None else size,
            fixtures=[dict(match_id=str(index + 1), timestamp=stamp)
                      for index, stamp in enumerate(timestamps)], methods=methods or {})
        if kind:
            payload['group_kind'] = kind
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        con = journal._connect(self.path, create=True)
        try:
            con.execute('INSERT INTO predictions VALUES(?,?,?,?)',
                (identifier, '2026-10-17T10:00:00+00:00', encoded, sha256(encoded.encode()).hexdigest()))
            con.commit()
        finally:
            con.close()

    def revision(self, identity, revision, *, state='pending'):
        with sqlite3.connect(self.path) as con:
            con.execute('INSERT INTO results VALUES(?,?,?,?)', (identity, revision,
                f'2026-10-20T12:{revision:02d}:00+00:00', json.dumps(dict(status=state,
                    actual=None, evaluations={}, observed=[], issues=[]))))

    def test_empty_reads_never_create_storage(self):
        self.assertEqual(journal.overview(self.path), {'total': 0, 'days': {}})
        self.assertEqual(journal.list_entries(self.path, day=date(2026, 10, 20)), [])
        self.assertIsNone(journal.get_entry('not-saved', self.path))
        self.assertFalse(self.path.exists())

    def test_scoped_reads_match_every_frozen_local_kickoff_with_offsets(self):
        values = {
            'one': ['2026-10-19T23:00:00Z', '2026-10-20T22:59:59.999999+00:00'],
            'offset': ['2026-10-20T00:00:00+01:00', '2026-10-20T20:00:00+02:00'],
            'naive': ['2026-10-20T12:00:00', '2026-10-20T13:00:00'],
            'future': ['2026-10-20T23:00:00Z'] * 2,
            'past': ['2026-10-19T22:59:59.999999Z'] * 2,
            'mixed-days': ['2026-10-20T12:00:00Z', '2026-10-20T23:30:00Z'],
            'invalid': ['bad', '2026-10-20T12:00:00Z'],
            'empty': [],
        }
        for identity, timestamps in values.items():
            self.insert(identity, timestamps)
        self.insert('mixed-leagues', values['naive'], kind='mixed')
        before = self.path.read_bytes()
        all_entries = journal.list_entries(self.path)
        selected = journal.list_entries(self.path, day=date(2026, 10, 20))
        self.assertEqual(selected, journal_performance.entries_on_day(all_entries, date(2026, 10, 20)))
        self.assertEqual({entry['id'] for entry in selected}, {'one', 'offset', 'naive', 'mixed-leagues'})
        ordinary = journal.list_entries(self.path, day=date(2026, 10, 20), include_mixed=False)
        self.assertEqual({entry['id'] for entry in ordinary}, {'one', 'offset', 'naive'})
        self.assertEqual(journal.overview(self.path), dict(total=9,
            days={'2026-10-19': 1, '2026-10-20': 4, '2026-10-21': 1}))
        self.assertEqual(journal.overview(self.path, include_mixed=False), dict(total=8,
            days={'2026-10-19': 1, '2026-10-20': 3, '2026-10-21': 1}))
        self.assertEqual(self.path.read_bytes(), before)

    def test_overview_does_not_decode_or_validate_methods_or_result_history(self):
        self.insert('today', ['2026-10-20T12:00:00Z'] * 2,
                    methods={'clubs': {'large': 'x' * 100000}})
        self.revision('today', 1)
        with patch.object(journal.json, 'loads', side_effect=AssertionError('No Python payload decoding')), \
             patch.object(journal, '_entry_from_row', side_effect=AssertionError('No method validation')):
            self.assertEqual(journal.overview(self.path), dict(total=1, days={'2026-10-20': 1}))

    def test_only_requested_day_is_decoded_but_its_integrity_is_still_checked(self):
        self.insert('today', ['2026-10-20T12:00:00Z'] * 2)
        self.insert('past', ['2026-10-19T12:00:00Z'] * 2)
        with patch.object(journal, '_entry_from_row', wraps=journal._entry_from_row) as read:
            self.assertEqual([entry['id'] for entry in journal.list_entries(self.path, day=date(2026, 10, 20))], ['today'])
            self.assertEqual(read.call_count, 1)
        with sqlite3.connect(self.path) as con:
            con.execute('DROP TRIGGER predictions_no_update')
            con.execute("UPDATE predictions SET digest='tampered' WHERE id='today'")
        with self.assertRaises(journal.JournalError):
            journal.list_entries(self.path, day=date(2026, 10, 20))
        with self.assertRaises(journal.JournalError):
            journal.get_entry('today', self.path)
        self.assertEqual([entry['id'] for entry in journal.list_entries(self.path, day=date(2026, 10, 19))], ['past'])

    def test_result_revisions_are_batched_and_a_new_writer_is_visible_next_read(self):
        for index in range(75):
            identity = f'p{index:03d}'
            self.insert(identity, ['2026-10-20T12:00:00Z'] * 2)
            self.revision(identity, 1)
        statements = []
        original = journal._connect
        def connect(*args, **kwargs):
            con = original(*args, **kwargs)
            con.set_trace_callback(statements.append)
            return con
        with patch.object(journal, '_connect', side_effect=connect):
            rows = journal.list_entries(self.path, day=date(2026, 10, 20))
        self.assertEqual(len(rows), 75)
        self.assertEqual(len([q for q in statements if q.startswith('SELECT * FROM results')]), 1)
        self.assertEqual(rows[0]['revision'], 1)
        self.revision('p000', 2, state='settled')
        rows = journal.list_entries(self.path, day=date(2026, 10, 20))
        self.assertEqual(rows[0]['status'], 'settled')
        self.assertEqual(rows[0]['revision'], 2)
        self.assertEqual([row['revision'] for row in rows[0]['result_history']], [1, 2])
        self.assertEqual(journal.get_entry('p000', self.path), rows[0])
        self.insert('new', ['2026-10-20T13:00:00Z'] * 2)
        self.assertEqual(journal.overview(self.path)['total'], 76)
        self.assertEqual(len(journal.list_entries(self.path, day=date(2026, 10, 20))), 76)

    def test_invalid_scope_and_partial_groups_are_not_silently_accepted(self):
        for invalid in ['2026-10-20', datetime(2026, 10, 20, tzinfo=timezone.utc), 1, True]:
            with self.assertRaises(journal.JournalError):
                journal.list_entries(self.path, day=invalid)
        for invalid in [None, 1, 'no']:
            with self.assertRaises(journal.JournalError):
                journal.list_entries(self.path, include_mixed=invalid)
            with self.assertRaises(journal.JournalError):
                journal.overview(self.path, include_mixed=invalid)
        with self.assertRaises(journal.JournalError):
            journal.get_entry('', self.path)
        self.insert('partial', ['2026-10-20T12:00:00Z'], size=2)
        self.assertEqual(journal.list_entries(self.path, day=date(2026, 10, 20)), [])
        self.assertEqual(journal.overview(self.path), dict(total=1, days={}))


if __name__ == '__main__':
    unittest.main()
