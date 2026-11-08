from copy import deepcopy
import unittest
from unittest.mock import patch

import causal_confirmation as core
import causal_confirmation_store as store
from causal_confirmation_followup import build
import test_causal_confirmation as helpers


START = '2026-11-02T00:00:00+00:00'
END = '2026-12-28T00:00:00+00:00'


class FollowupEnrollmentTests(unittest.TestCase):
    setUp = helpers.ConfirmationTests.setUp
    target = helpers.ConfirmationTests.target
    clock = staticmethod(helpers.ConfirmationTests.clock)
    save = helpers.ConfirmationTests.save
    capture = helpers.ConfirmationTests.capture

    def enroll(self, stamp='2026-10-31T19:00:00Z'):
        return core.register_followup(self.source, start=START, end=END,
                                      root=self.root, clock=self.clock(stamp))

    def prepare_followup(self):
        protocol = self.enroll()
        raw = deepcopy(self.raw)
        for value in raw:
            value['fixture']['date'] = '2026-11-02T16:00:00+00:00'
        self.save(raw, '2026-11-01T12:00:00+00:00')
        core.capture_due(self.root, clock=self.clock('2026-11-02T08:01:00Z'), refresh=lambda **kw: raw)
        return protocol, raw

    def test_new_enrollment_preserves_original_protocol_and_candidate_window(self):
        original = deepcopy(self.candidate)
        before = deepcopy(store.protocols(self.root))
        with patch.object(core.candidates, 'fit_candidate', side_effect=AssertionError('No fit')):
            followup = self.enroll()
            again = self.enroll('2026-11-01T12:00:00Z')
        self.assertEqual(followup, again)
        self.assertEqual(self.candidate, original)
        self.assertEqual(store.protocols(self.root)[0], before[0])
        self.assertEqual(len(store.protocols(self.root)), 2)
        self.assertNotEqual(followup['id'], self.protocol['id'])
        self.assertEqual(followup['candidate_original_confirmation'], original['report']['plan']['windows']['confirmation'])
        self.assertEqual(followup['followup']['result_cutoff_utc'], '2027-01-04T08:00:00+00:00')
        self.network.assert_not_called()

    def test_new_enrollment_refuses_past_decision_or_original_overlapping_window(self):
        for stamp in ('2026-11-02T08:00:00Z', '2026-12-29T00:00:00Z'):
            with self.assertRaises(ValueError):
                self.enroll(stamp)
        with self.assertRaises(ValueError):
            core.register_followup(self.source, start='2026-10-26T00:00:00Z', end='2026-12-21T00:00:00Z',
                                   root=self.root, clock=self.clock('2026-10-30T19:00:00Z'))

    def test_changed_weekly_policy_rejected_even_if_resealed(self):
        value = self.enroll()
        value['followup']['min_blocks'] = 7
        value = core._seal({k:v for k,v in value.items() if k != 'id'})
        with self.assertRaises(ValueError):
            core._validate_protocol(value)

    def test_runtime_change_blocks_new_capture_without_loading_or_provider_call(self):
        self.enroll()
        with patch.object(core, '_runtime', return_value={'python':'changed', 'packages':{}}), \
                patch.object(core, '_pin', side_effect=AssertionError('No changed runtime')):
            value = core.capture_due(self.root, clock=self.clock('2026-11-02T08:01:00Z'),
                                     refresh=lambda **kw: (_ for _ in ()).throw(AssertionError('No provider')))
        self.assertTrue(value['issues'])

    def test_saved_whitespace_label_uses_original_snapshot_canonicalization(self):
        raw = deepcopy(self.raw)
        raw[0]['teams']['away']['name'] = ' '+raw[0]['teams']['away']['name']+' '
        self.save(raw, '2026-10-30T13:00:00+00:00')
        self.capture(refresh=lambda **kw:raw)
        anchor = store.anchor(self.protocol['id'], '2026-10-31', root=self.root)
        before = deepcopy(anchor)
        result = core._entries(self.root, self.protocol['id'])
        self.assertEqual(len(result), 2)
        self.assertEqual(anchor, before)
        self.assertFalse(result[0]['fixture']['away'].startswith(' '))
        changed = deepcopy(anchor)
        changed['targets'][0]['fixture']['away_id'] = '999'
        with patch.object(store, 'anchor', return_value=changed), self.assertRaises(ValueError):
            core._entries(self.root, self.protocol['id'])

    def test_future_rescheduled_fixture_is_explicitly_excluded_not_recaptured(self):
        protocol, raw = self.prepare_followup()
        changed = deepcopy(raw[0]); changed['fixture']['date'] = '2026-11-03T16:00:00+00:00'
        self.save([changed], '2026-11-02T18:00:00+00:00')
        core.capture_due(self.root, clock=self.clock('2026-11-03T08:01:00Z'), refresh=lambda **kw:[changed])
        anchor = store.anchor(protocol['id'], '2026-11-03', root=self.root)
        self.assertFalse(anchor['targets'])
        self.assertIn(dict(match_id=str(changed['fixture']['id']), reason='fixture_already_selected_on_earlier_day'), anchor['exclusions'])
        self.assertEqual(len(store.heads(root=self.root, protocol_id=protocol['id'])), 2)

    def test_daily_phase_between_windows_and_empty_census_are_not_open_capture(self):
        protocol, raw = self.prepare_followup()
        for stamp, expected in [('2026-11-03T07:00:00Z','between_daily_windows'),
                                ('2026-11-03T08:01:00Z','capture_window'),
                                ('2026-11-03T09:00:00Z','window_missed')]:
            with patch.object(core, '_now', return_value=self.clock(stamp)()):
                value = next(p for p in core.report(self.root)['protocols'] if p['id'] == protocol['id'])
            self.assertEqual(value['phase'], expected)

    def test_repeated_reconcile_does_not_rescan_closed_anchor_histories(self):
        protocol = self.enroll()
        current = self.clock('2026-11-10T09:00:00Z')()
        core.reconcile(self.root, now=current)
        with patch.object(core, '_closed_window', side_effect=AssertionError('Already closed')):
            core.reconcile(self.root, now=current)
        closed = [event for event in store.attempts(protocol['id'], root=self.root) if event['kind'] == 'window_missed']
        self.assertEqual(len(closed), 9)

    def test_final_result_revision_cutoff_does_not_drift_on_later_read(self):
        protocol, raw = self.prepare_followup()
        finished = deepcopy(raw[0]); finished['fixture']['status']['short'] = 'FT'
        finished['score']['halftime'] = {'home':1, 'away':0}
        finished['score']['fulltime'] = {'home':2, 'away':2}
        self.save([finished], '2026-11-02T19:00:00+00:00')
        core.reconcile(self.root, now=self.clock('2026-11-02T20:00:00Z')())
        finished['score']['halftime'] = {'home':2, 'away':0}
        finished['score']['fulltime'] = {'home':2, 'away':0}
        self.save([finished], '2027-01-05T10:00:00+00:00')
        core.reconcile(self.root, now=self.clock('2027-01-05T11:00:00Z')())
        for stamp in ('2027-01-05T12:00:00Z','2027-01-07T12:00:00Z'):
            with patch.object(core, '_now', return_value=self.clock(stamp)()):
                value = next(p for p in core.report(self.root)['protocols'] if p['id'] == protocol['id'])
            self.assertEqual(value['result_cutoff_utc'], '2027-01-04T08:00:00+00:00')
            row = next(r for r in value['rows'] if r['fixture']['match_id'] == str(raw[0]['fixture']['id']))
            self.assertEqual(row['actual'], '2nd')
        live = core._entries(self.root, protocol['id'])
        self.assertEqual(next(r for r in live if r['fixture']['match_id'] == str(raw[0]['fixture']['id']))['actual'], '1st')

    def test_planned_followup_report_is_read_only_and_unknown_census_is_not_zero_targets(self):
        protocol = self.enroll()
        before = store.protocols(self.root)
        with patch.object(core, '_now', return_value=self.clock('2026-11-01T12:00:00Z')()), \
                patch.object(core.candidates, 'fit_candidate', side_effect=AssertionError('No fit')):
            value = next(p for p in core.report(self.root)['protocols'] if p['id'] == protocol['id'])
            weekly = build(value)
        self.assertEqual(weekly['coverage']['unknown_census_days'], 56)
        self.assertEqual(weekly['coverage']['targets_known'], 0)
        self.assertFalse(weekly['inference']['available'])
        self.assertEqual(before, store.protocols(self.root))
        self.network.assert_not_called()

    def test_unsealed_final_read_is_read_only_and_waits_for_worker_to_freeze(self):
        protocol, raw = self.prepare_followup()
        with patch.object(core, '_now', return_value=self.clock('2027-01-05T12:00:00Z')()):
            value = next(p for p in core.report(self.root)['protocols'] if p['id'] == protocol['id'])
        self.assertIsNone(value['endpoint'])
        self.assertIsNone(store.endpoint(protocol['id'], root=self.root))

    def test_result_saved_after_cutoff_with_earlier_scan_time_stays_out(self):
        protocol, raw = self.prepare_followup()
        forecast = store.heads(root=self.root, protocol_id=protocol['id'])[0]['payload']
        result = core._seal(dict(status='settled', ht='0-0', ft='0-1', actual='2nd', issues=[],
            checked_at='2027-01-04T07:59:59+00:00', source_ref=dict(match_id=forecast['fixture']['match_id'],
                observed_at_utc='2026-11-02T19:00:00+00:00')))
        times = iter([self.clock('2027-01-04T07:59:59Z')(),self.clock('2027-01-04T08:00:01Z')()])
        store.put_result(forecast['id'], result, root=self.root, clock=lambda:next(times))
        cutoff = self.clock('2027-01-04T08:00:00Z')()
        rows = core._entries(self.root, protocol['id'], result_cutoff=cutoff)
        row = next(row for row in rows if row['forecast_id'] == forecast['id'])
        self.assertIsNone(row['actual'])

    def test_sealed_final_selection_ignores_receipts_published_after_its_creation(self):
        protocol, raw = self.prepare_followup()
        forecast = store.heads(root=self.root, protocol_id=protocol['id'])[0]['payload']
        result = core._seal(dict(status='settled', ht='0-0', ft='0-1', actual='2nd', issues=[],
            checked_at='2027-01-04T07:59:00+00:00', source_ref=dict(match_id=forecast['fixture']['match_id'],
                observed_at_utc='2026-11-02T19:00:00+00:00')))
        held = []
        with patch.object(store, '_write_result_ack', side_effect=lambda con,value:held.append(value)):
            store.put_result(forecast['id'], result, root=self.root, clock=self.clock('2027-01-04T07:59:01Z'))
        current = self.clock('2027-01-04T08:01:00Z')()
        core._finalize_endpoints(self.root, current, clock=lambda:current)
        endpoint = store.endpoint(protocol['id'], root=self.root)
        self.assertIsNone(endpoint['selection'][forecast['id']]['revision'])
        con = store._connect(self.root, create=True)
        try:
            store._write_result_ack(con, held[0])
        finally:
            con.close()
        self.assertEqual(core._finalize_endpoints(self.root, current, clock=lambda:current), 0)
        with patch.object(core, '_now', return_value=current):
            value = next(p for p in core.report(self.root)['protocols'] if p['id'] == protocol['id'])
        row = next(row for row in value['rows'] if row['forecast_id'] == forecast['id'])
        self.assertIsNone(row['actual'])
        self.assertEqual(value['endpoint']['id'], endpoint['id'])

    def test_final_selection_cannot_choose_a_revision_saved_after_its_cutoff(self):
        protocol, raw = self.prepare_followup()
        forecast = store.heads(root=self.root, protocol_id=protocol['id'])[0]['payload']
        result = core._seal(dict(status='settled', ht='0-0', ft='0-1', actual='2nd', issues=[],
            checked_at='2027-01-05T10:00:00+00:00', source_ref=dict(match_id=forecast['fixture']['match_id'],
                observed_at_utc='2026-11-02T19:00:00+00:00')))
        store.put_result(forecast['id'], result, root=self.root, clock=self.clock('2027-01-05T10:00:01Z'))
        current = self.clock('2027-01-05T11:00:00Z')()
        core._finalize_endpoints(self.root, current, clock=lambda:current)
        endpoint = store.endpoint(protocol['id'], root=self.root)
        endpoint['selection'][forecast['id']] = dict(revision=1, result_id=result['id'])
        with self.assertRaises(ValueError):
            core._entries(self.root, protocol['id'], endpoint=endpoint)


if __name__ == '__main__':
    unittest.main()
