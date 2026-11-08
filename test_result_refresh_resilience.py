"""Result collection tolerates omitted IDs and independent journal failures."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import automation_worker as worker
import candidate_tracking
import high_probability_archive as archive
import prediction_journal as journal
from test_high_probability_engine import NOW, fixture
from test_high_probability_archive import report as archived_report


class Gate:
    """An offline provider and durable state checkpoint in a temporary root."""
    def __init__(self, root, raw, *, state=None, omitted=(), pause_after=None):
        self.root, self.raw = root, deepcopy(raw)
        self.state = {} if state is None else state
        self.omitted, self.pause_after = set(omitted), pause_after
        self.calls = []

    def call(self, **params):
        self.calls.append(params)
        if self.pause_after is not None and len(self.calls) > self.pause_after:
            raise worker._Pause('Provider/host pause')
        wanted = set(params['ids'].split('-'))-self.omitted
        return [deepcopy(row) for row in self.raw if str(row['fixture']['id']) in wanted]

    def save(self):
        (self.root/'state.json').write_text(json.dumps(self.state), encoding='utf-8')


class ResultRefreshResilienceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.current = NOW+timedelta(hours=10)
        self.primary, self.candidates = [], []
        sources = patch.object(journal, 'list_entries', side_effect=self.entries)
        sources.start(); self.addCleanup(sources.stop)
        offline = patch.object(worker.cloud_storage, 'configured', return_value=False)
        offline.start(); self.addCleanup(offline.stop)
        network = patch.object(api.ApiFootballClient, 'get', side_effect=AssertionError('No real API'))
        self.network = network.start(); self.addCleanup(network.stop)

    def tearDown(self):
        self.network.assert_not_called()

    def entries(self, path):
        return deepcopy(self.primary if Path(path).name == 'predictions.sqlite3' else self.candidates)

    def tracked(self, count=45, status='pending'):
        raw = [fixture(100000+index) for index in range(count)]
        self.primary = [dict(status=status, fixtures=[dict(match_id=str(row['fixture']['id']),
            timestamp=row['fixture']['date']) for row in raw])]
        api.save_fixtures(raw, self.root/'fixtures.json')
        return raw

    def finals(self, count=45):
        return [fixture(100000+index, status='FT', ht=(1, 0), ft=(1, 0)) for index in range(count)]

    def test_partial_lot_saves_valid_results_and_processes_all_following_lots(self):
        self.tracked()
        gate = Gate(self.root, self.finals(), omitted={'100000'})
        report = {}
        worker._refresh_results(self.root, gate, lambda: self.current, report)
        self.assertEqual([len(call['ids'].split('-')) for call in gate.calls], [20, 20, 5])
        stored = {str(row['fixture']['id']): row for row in api.load_fixtures(self.root/'fixtures.json')}
        self.assertEqual(api._status_short(stored['100000']), 'NS')
        self.assertTrue(all(api._status_short(row) == 'FT' for mid, row in stored.items() if mid != '100000'))
        self.assertEqual(report['result_refresh'], dict(received=44, missing=1, batches=3,
            due=45, remaining=0, status='partial'))
        self.assertEqual(set(gate.state['result_checks']), set(stored))
        self.assertEqual(gate.state['result_missing']['100000']['reason'], 'not_returned_by_api')
        self.assertTrue(report['issues'])
        self.assertEqual(json.loads((self.root/'state.json').read_text()), gate.state)
        self.assertEqual(self.primary[0]['status'], 'pending')

    def test_partial_response_settles_only_returned_archived_match_and_preserves_predictions(self):
        saved = archived_report()
        archive.capture(saved, self.root, clock=lambda: NOW)
        api.save_fixtures([fixture(100001), fixture(100002)], self.root/'fixtures.json')
        original = {row['fixture']['match_id']: deepcopy(row['probabilities'])
                    for row in archive.list_entries(self.root)}
        gate = Gate(self.root, [fixture(100002, status='FT', ht=(1, 0), ft=(1, 0))])
        worker._refresh_results(self.root, gate, lambda: self.current, {})
        with patch.object(worker, '_candidate_observation'):
            report = {}
            worker._reconcile(self.root, report, self.current)
        rows = {row['fixture']['match_id']: row for row in archive.list_entries(self.root)}
        self.assertEqual(rows['100002']['status'], 'settled')
        self.assertEqual(rows['100002']['actual'], '1st')
        self.assertEqual(rows['100001']['status'], 'pending')
        self.assertIsNone(rows['100001']['actual'])
        self.assertIsNone(rows['100001']['ht'])
        self.assertEqual(report['probability_archive_results']['settled'], 1)
        self.assertEqual({mid: row['probabilities'] for mid, row in rows.items()}, original)

    def test_missing_ids_are_retried_later_and_removed_only_after_a_real_reply(self):
        self.tracked(2)
        gate = Gate(self.root, self.finals(2), omitted={'100000'})
        worker._refresh_results(self.root, gate, lambda: self.current, {})
        saved = json.loads((self.root/'state.json').read_text())
        first = deepcopy(saved['result_missing']['100000'])
        retry = Gate(self.root, self.finals(2), state=saved, omitted={'100000'})
        worker._refresh_results(self.root, retry, lambda: self.current+timedelta(minutes=1), {})
        self.assertFalse(retry.calls)
        worker._refresh_results(self.root, retry, lambda: self.current+timedelta(minutes=16), {})
        self.assertEqual(retry.state['result_missing']['100000']['attempts'], 2)
        self.assertEqual(retry.state['result_missing']['100000']['first_attempt_at'], first['first_attempt_at'])
        retry.omitted.clear()
        worker._refresh_results(self.root, retry, lambda: self.current+timedelta(minutes=32), {})
        self.assertNotIn('100000', retry.state['result_missing'])
        self.assertTrue(all(api._status_short(row) == 'FT' for row in api.load_fixtures(self.root/'fixtures.json')))

    def test_empty_responses_are_checkpointed_without_fabricating_or_saving_scores(self):
        original = self.tracked(25)
        gate = Gate(self.root, [])
        with patch.object(api, 'save_fixtures', side_effect=AssertionError('No fabricated fixtures')):
            report = {}
            worker._refresh_results(self.root, gate, lambda: self.current, report)
        self.assertEqual(len(gate.calls), 2)
        self.assertEqual(len(gate.state['result_missing']), 25)
        self.assertEqual(report['result_refresh']['received'], 0)
        self.assertEqual(report['result_refresh']['missing'], 25)
        self.assertEqual(api.load_fixtures(self.root/'fixtures.json'), original)

    def test_provider_pause_preserves_partial_checkpoint_and_fair_resume(self):
        self.tracked()
        gate = Gate(self.root, self.finals(), omitted={'100000'}, pause_after=1)
        with self.assertRaises(worker._Pause):
            worker._refresh_results(self.root, gate, lambda: self.current, {})
        state = json.loads((self.root/'state.json').read_text())
        self.assertEqual(len(state['result_checks']), 20)
        self.assertIn('100000', state['result_missing'])
        resumed = Gate(self.root, self.finals(), state=state, omitted={'100000'})
        worker._refresh_results(self.root, resumed, lambda: self.current+timedelta(minutes=16), {})
        first_ids = resumed.calls[0]['ids'].split('-')
        self.assertNotIn('100000', first_ids)
        self.assertEqual(set(first_ids), {str(100020+index) for index in range(20)})
        self.assertEqual(len(resumed.state['result_checks']), 45)

    def test_duplicate_unexpected_or_malformed_ids_reject_the_entire_lot(self):
        self.tracked(2)
        replies = [[fixture(100000, status='FT')]*2,
                   [fixture(100000, status='FT'), fixture(999999, status='FT')],
                   [fixture(100000, status='FT'), {'fixture': {}}], None]
        for raw in replies:
            gate = Gate(self.root, [])
            with self.subTest(raw=raw), patch.object(gate, 'call', return_value=raw), \
                    patch.object(api, 'save_fixtures', side_effect=AssertionError('Invalid lot must not save')):
                with self.assertRaises(worker.AutomationError):
                    worker._refresh_results(self.root, gate, lambda: self.current, {})
            self.assertEqual(gate.state['result_checks'], {})
            self.assertEqual(gate.state['result_missing'], {})

    def test_void_group_still_refreshes_its_nonadministrative_fixture(self):
        raw = self.tracked(2, status='void')
        api.save_fixtures([fixture(100000, status='CANC')], self.root/'fixtures.json')
        original = deepcopy(self.primary)
        gate = Gate(self.root, [fixture(100000, status='CANC'), self.finals(2)[1]])
        worker._refresh_results(self.root, gate, lambda: self.current, {})
        self.assertEqual([call['ids'] for call in gate.calls], ['100001'])
        self.assertEqual(self.primary, original)
        stored = {str(row['fixture']['id']): row for row in api.load_fixtures(self.root/'fixtures.json')}
        self.assertEqual(api._status_short(stored['100000']), 'CANC')
        self.assertEqual(api._status_short(stored['100001']), 'FT')

    def test_unavailable_primary_or_candidate_source_does_not_hide_other_tracked_fixtures(self):
        self.tracked(2)
        self.candidates = [dict(status='pending', fixtures=[self.primary[0]['fixtures'][1]])]
        for failing in ('predictions.sqlite3', candidate_tracking.database_path(self.root).name):
            gate = Gate(self.root, self.finals(2))
            def load(path):
                if Path(path).name == failing:
                    raise journal.JournalError('Source unavailable')
                return self.entries(path)
            with self.subTest(failing=failing), patch.object(journal, 'list_entries', side_effect=load):
                report = {}
                worker._refresh_results(self.root, gate, lambda: self.current, report)
            self.assertTrue(gate.calls)
            self.assertEqual(report['status'], 'error')
            self.assertIn('unavailable', report['result_refresh_sources'].values())
            self.assertTrue(report['issues'])

    def test_reconciliation_failures_are_isolated_and_never_report_success_for_failed_store(self):
        counts = dict(entries=1, changed=1, settled=1, pending=0, unresolved=0)
        for failing, output in (('journal', 'journal_results'), ('archive', 'probability_archive_results'),
                                ('candidate', 'candidate_results')):
            with self.subTest(failing=failing), \
                    patch.object(api, 'load_fixtures', return_value=[]), \
                    patch.object(journal, 'reconcile', side_effect=journal.JournalError('Bad journal')
                        if failing == 'journal' else None, return_value=counts) as main, \
                    patch.object(archive, 'reconcile', side_effect=archive.ArchiveError('Bad archive')
                        if failing == 'archive' else None, return_value=counts) as archived, \
                    patch.object(candidate_tracking, 'reconcile', side_effect=ValueError('Bad candidate')
                        if failing == 'candidate' else None, return_value=counts) as candidates, \
                    patch.object(worker, '_candidate_observation') as observation:
                report = dict(status='completed', settled=0, pending=0, unresolved=0)
                worker._reconcile(self.root, report, self.current)
            for action in (main, archived, candidates, observation):
                action.assert_called_once()
            self.assertEqual(report[output], dict(status='unavailable'))
            self.assertEqual(report['status'], 'error')
            self.assertTrue(report['issues'])
            if failing == 'journal':
                self.assertNotIn('settled', report)
                self.assertNotIn('pending', report)

    def test_pause_is_never_swallowed_by_source_or_reconciliation_isolation(self):
        with patch.object(journal, 'list_entries', side_effect=worker._Pause('Stop')):
            with self.assertRaises(worker._Pause):
                worker._refresh_results(self.root, Gate(self.root, []), lambda: self.current, {})
        with patch.object(api, 'load_fixtures', return_value=[]), \
                patch.object(journal, 'reconcile', side_effect=worker._Pause('Stop')), \
                patch.object(archive, 'reconcile') as archived:
            with self.assertRaises(worker._Pause):
                worker._reconcile(self.root, {}, self.current)
            archived.assert_not_called()


if __name__ == '__main__':
    unittest.main()
