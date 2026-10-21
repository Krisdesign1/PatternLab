"""A dated protocol cannot be silently replaced after outcomes are inspected."""
from datetime import datetime, timedelta, timezone
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import journal_protocol as protocol


NOW = datetime(2026, 10, 18, 22, 55, tzinfo=timezone.utc)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root/'isolated'/'protocol.json'

    def test_reading_absent_protocol_never_creates_any_storage(self):
        self.assertIsNone(protocol.load_protocol(self.path))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_registration_is_idempotent_and_preserves_first_timestamp_and_bytes(self):
        first = protocol.register_protocol(self.path, now=NOW)
        before = self.path.read_bytes()
        second = protocol.register_protocol(self.path, now=NOW+timedelta(days=10))
        self.assertEqual(second, first)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(protocol.load_protocol(self.path), first)
        self.assertEqual(first['registered_at'], NOW.isoformat())
        self.assertEqual(first['sizes'], list(range(2,21)))

    def test_tampered_or_incompatible_criteria_are_rejected_even_with_new_digest(self):
        first = protocol.register_protocol(self.path, now=NOW)
        for key,value in (('registered_at','2026-10-18T22:55:00'),('min_blocks',2),
                          ('baseline','frequency'),('bootstrap_samples',8000),('sizes',[True,4]),
                          ('primary','top3'),('version','new-version')):
            with self.subTest(key=key):
                changed = dict(copy.deepcopy(first), **{key:value})
                changed['fingerprint'] = protocol._fingerprint(changed)
                with self.assertRaises(protocol.ProtocolError):protocol.validate_protocol(changed)
        first['registered_at'] = (NOW+timedelta(hours=1)).isoformat()
        with self.assertRaises(protocol.ProtocolError):protocol.validate_protocol(first)

    def test_invalid_existing_protocol_is_not_replaced(self):
        self.path.parent.mkdir()
        self.path.write_text('{broken')
        with self.assertRaises(protocol.ProtocolError):protocol.register_protocol(self.path, now=NOW)
        self.assertEqual(self.path.read_text(), '{broken')

    def test_atomic_failure_leaves_no_partial_protocol_and_can_retry(self):
        with patch.object(protocol.os,'replace',side_effect=OSError('Simulated storage failure')):
            with self.assertRaises(OSError):protocol.register_protocol(self.path, now=NOW)
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])
        self.assertEqual(protocol.register_protocol(self.path, now=NOW)['registered_at'],NOW.isoformat())

    def test_concurrent_registration_is_refused_and_real_date_cannot_be_injected(self):
        self.path.parent.mkdir()
        with protocol._registration_lock(self.path):
            with self.assertRaises(protocol.ProtocolError):protocol.register_protocol(self.path, now=NOW)
        with self.assertRaises(protocol.ProtocolError):protocol.register_protocol(now=NOW)
        with self.assertRaises(protocol.ProtocolError):protocol.register_protocol(self.path,now=NOW.replace(tzinfo=None))


if __name__ == '__main__':
    unittest.main()
