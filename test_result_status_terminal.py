"""A changed schedule must not hide a provider's administrative state."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

import prediction_journal as journal
from test_result_explanations import fixture, raw


class TerminalStateTests(unittest.TestCase):
    def test_administrative_states_with_changed_dates_exclude_group(self):
        self.check_states(('CANC', 'ABD', 'AWD', 'WO'), 'void', 'annulé, abandonné ou attribué')

    def test_postponement_and_interruptions_keep_explicit_reason_with_changed_dates(self):
        self.check_states(('PST', 'SUSP', 'INT'), 'unresolved', 'reporté ou interrompu')

    def check_states(self, statuses, expected, message):
        fixtures = [fixture(), dict(fixture(), match_id='2')]
        entry = dict(fixtures=fixtures, size=2, methods={}, created_at='2026-10-24T09:00:00Z')
        original = deepcopy(entry)
        for code in statuses:
            with self.subTest(status=code):
                observations = {'1':raw(code), '2':raw(code)}
                observations['2']['fixture']['id'] = 2
                def converted(value):
                    mid = str(value['fixture']['id'])
                    return dict(next(f for f in fixtures if f['match_id'] == mid),
                                timestamp='2026-10-28T12:00:00')
                with patch.object(journal.workspace, 'fixture_from_raw', side_effect=converted):
                    result = journal._settle(entry, observations, datetime(2026,10,27,18,tzinfo=timezone.utc))
                self.assertEqual(result['status'], expected)
                self.assertTrue(all(message in issue for issue in result['issues']))
                self.assertEqual(result['evaluations'], {})
                self.assertIsNone(result['actual'])
                self.assertEqual(entry, original)


if __name__ == '__main__':
    unittest.main()
