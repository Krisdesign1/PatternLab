"""Provider budgets and durable mutation boundaries must stay wired together."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import automation_worker as worker
import cloud_storage as storage
import test_cloud_storage as fixtures


class BudgetPersistenceTests(unittest.TestCase):
    setUp = fixtures.CloudStorageTests.setUp
    write = fixtures.CloudStorageTests.write
    seed = fixtures.CloudStorageTests.seed
    def test_request_reservation_is_remote_before_provider_call(self):
        self.seed()
        now = datetime(2026,10,23,10,tzinfo=timezone.utc)
        state = {}
        report = dict(requests=0)
        backend = self.backend
        class Client:
            last_quota = {}
            def fixtures(client_self, **params):
                entry = backend.manifest['files']['api_football/automation_state.json']
                encoded, _ = backend.blobs[entry['sha256']]
                import json, zlib
                saved = json.loads(zlib.decompress(encoded))
                self.assertEqual(saved['requests_today'],1)
                return dict(response=[])
        gate = worker._Gate(self.root/'api_football', state, worker.DEFAULT_CONFIG, Client(), lambda:now, report)
        with storage.transaction():
            self.assertEqual(gate.call(date='2026-10-23'), [])

    def test_failed_checkpoint_prevents_provider_spending(self):
        self.seed()
        self.backend.fail_publish = True
        now = datetime(2026,10,23,10,tzinfo=timezone.utc)
        from unittest.mock import Mock
        client = Mock()
        gate = worker._Gate(self.root/'api_football', {}, worker.DEFAULT_CONFIG, client, lambda:now, dict(requests=0))
        with self.assertRaises(storage.CloudStorageError):
            with storage.transaction():
                gate.call(date='2026-10-23')
        client.fixtures.assert_not_called()


if __name__ == '__main__':
    unittest.main()
