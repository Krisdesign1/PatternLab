"""Bounded database reads reject incomplete payloads before restoration."""
from contextlib import nullcontext
from hashlib import sha256
import unittest
from unittest.mock import Mock, patch

from cloud_postgres import BackendError, PostgresBackend


class PostgresReadBatchTests(unittest.TestCase):
    def setUp(self):
        self.backend = PostgresBackend('postgresql://test:unused@remote/test?sslmode=require', 'test-only')
        self.first = sha256(b'first').hexdigest()
        self.second = sha256(b'second').hexdigest()

    def connection(self, metadata, contents):
        connection = Mock()
        connection.execute.side_effect = [Mock(fetchall=lambda: metadata), Mock(fetchall=lambda: contents)]
        return connection

    def test_deduplicates_requests_on_one_connection_and_scopes_workspace(self):
        con = self.connection([(self.first, 50, 5), (self.second, 60, 6)],
                              [(self.second, b'second', 60), (self.first, b'first', 50)])
        with patch.object(self.backend, '_connection', return_value=nullcontext(con)) as connect:
            result = self.backend.read_blobs([self.first, self.second, self.first])
        self.assertEqual(result, {self.first: (b'first', 50), self.second: (b'second', 60)})
        self.assertEqual(connect.call_count, 1)
        self.assertEqual(con.execute.call_count, 2)
        for call in con.execute.call_args_list:
            self.assertEqual(call.args[1], ('test-only', sorted([self.first, self.second])))

    def test_empty_invalid_and_excessive_requests_never_connect(self):
        with patch.object(self.backend, '_connection') as connect:
            self.assertEqual(self.backend.read_blobs([]), {})
            for digests in ('not-a-list', ['invalid'], [None],
                            [sha256(str(i).encode()).hexdigest() for i in range(33)]):
                with self.subTest(digests=digests), self.assertRaises(BackendError):
                    self.backend.read_blobs(digests)
            connect.assert_not_called()

    def test_missing_objects_are_rejected_before_reading_payloads(self):
        con = self.connection([(self.first, 50, 5)], [])
        with patch.object(self.backend, '_connection', return_value=nullcontext(con)):
            with self.assertRaises(BackendError):
                self.backend.read_blobs([self.first, self.second])
        self.assertEqual(con.execute.call_count, 1)

    def test_oversized_batch_is_rejected_before_reading_payloads(self):
        con = self.connection([(self.first, 1, 4 * 1024 * 1024), (self.second, 1, 1)], [])
        with patch.object(self.backend, '_connection', return_value=nullcontext(con)):
            with self.assertRaises(BackendError):
                self.backend.read_blobs([self.first, self.second])
        self.assertEqual(con.execute.call_count, 1)

    def test_single_large_object_is_allowed_but_workspace_limit_is_enforced(self):
        content = b'x' * (4 * 1024 * 1024 + 1)
        con = self.connection([(self.first, len(content), len(content))],
                              [(self.first, content, len(content))])
        with patch.object(self.backend, '_connection', return_value=nullcontext(con)):
            self.assertEqual(self.backend.read_blobs([self.first])[self.first], (content, len(content)))
        con = self.connection([(self.first, 1, 96 * 1024 * 1024 + 1)], [])
        with patch.object(self.backend, '_connection', return_value=nullcontext(con)):
            with self.assertRaises(BackendError):
                self.backend.read_blobs([self.first])
        self.assertEqual(con.execute.call_count, 1)

    def test_payload_changes_and_incomplete_results_fail_closed(self):
        for contents in ([], [(self.first, b'bad', 50)], [(self.first, b'first', 51)],
                         [(self.second, b'first', 50)]):
            with self.subTest(contents=contents):
                con = self.connection([(self.first, 50, 5)], contents)
                with patch.object(self.backend, '_connection', return_value=nullcontext(con)):
                    with self.assertRaises(BackendError):
                        self.backend.read_blobs([self.first])


if __name__ == '__main__':
    unittest.main()
