"""Pure historical reconstruction: no database, credentials, API or app calls."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest

import fixture_import_policy as policy
from fixture_temporal import reconstruct


def raw(mid=123, *, short='FT', halftime=(1,0), fulltime=(2,1)):
    return dict(fixture=dict(id=mid,date='2026-01-01T12:00:00Z',status=dict(short=short)),
                league=dict(id=61),teams=dict(home=dict(id=1),away=dict(id=2)),
                score=dict(halftime=dict(zip(('home','away'),halftime)),
                           fulltime=dict(zip(('home','away'),fulltime))))


def event(incoming, stamp, *, previous=None, selected=None, last=None, decision='received'):
    return policy.observation(previous,incoming,incoming if selected is None else selected,
                              decision,None,stamp,last)


def reseal(record, **values):
    result = dict(deepcopy(record),**values)
    result.pop('id',None)
    result['id'] = policy.digest(result)
    return result


class FixtureTemporalTests(unittest.TestCase):
    def setUp(self):
        self.a = raw()
        self.b = raw(halftime=(0,0))
        self.r1 = event(self.a,'2026-10-25T10:00:00Z')
        self.r2 = event(self.b,'2026-10-26T10:00:00Z',previous=self.a,last=self.r1)

    def result(self, records, at='2026-10-27T00:00:00Z'):
        return reconstruct(records,at=at)

    def test_exact_cutoff_is_exclusive_and_old_event_time_does_not_create_availability(self):
        result = self.result([self.r1],at='2026-10-25T10:00:00Z')
        self.assertFalse(result['selected'])
        self.assertEqual(result['excluded'][0]['reason'],'not_observed_before_cutoff')
        self.assertEqual(self.result([self.r1],at='2026-10-25T10:00:00.000001Z')['selected'][0]['raw'],self.a)

    def test_future_correction_does_not_change_or_invalidate_old_selection(self):
        initial = self.result([self.r1],at='2026-10-26T00:00:00Z')
        for future in (self.r2,dict(self.r2,id='broken',selected_raw_sha256='unknown')):
            self.assertEqual(self.result([self.r1,future],at='2026-10-26T00:00:00Z'),initial)
        wrapper = dict(record=self.r2,match_id='999',observed_at_utc='bad index',digest='bad')
        self.assertEqual(self.result([self.r1,wrapper],at='2026-10-26T00:00:00Z'),initial)

    def test_latest_known_correction_and_input_order_independence(self):
        original = deepcopy([self.r1,self.r2])
        expected = self.result(original)
        self.assertEqual(expected['selected'][0]['raw'],self.b)
        self.assertEqual(expected['selected'][0]['observation_id'],self.r2['id'])
        self.assertEqual(expected,self.result(original[::-1]))
        self.assertEqual(original,[self.r1,self.r2])

    def test_live_regression_uses_retained_version_not_received_payload(self):
        incoming = raw(short='NS')
        retained = event(incoming,'2026-10-26T10:00:00Z',previous=self.a,selected=self.a,
                         last=self.r1,decision='retained_final')
        self.assertEqual(self.result([self.r1,retained])['selected'][0]['raw'],self.a)

    def test_legacy_baseline_becomes_available_only_at_first_tracked_import(self):
        incoming = raw(short='NS')
        first = event(incoming,'2026-10-25T10:00:00Z',previous=self.a,selected=self.a,
                      decision='retained_final')
        self.assertEqual(first['baseline_raw'],self.a)
        self.assertFalse(self.result([first],at='2026-10-25T09:59:59Z')['selected'])
        self.assertEqual(self.result([first])['selected'][0]['raw'],self.a)

    def test_equal_baseline_and_actual_zero_scores_are_preserved(self):
        zero = raw(halftime=(0,0),fulltime=(0,0))
        first = event(zero,'2026-10-25T10:00:00Z',previous=zero,selected=zero)
        self.assertTrue(first['baseline_equals_received'])
        self.assertEqual(self.result([first])['selected'][0]['raw'],zero)

    def test_quarantine_is_reapplied_and_not_silently_cleared(self):
        incoming = raw(halftime=(None,None))
        selected = deepcopy(self.a)
        selected[policy.QUALITY_KEY] = dict(version=policy.VERSION,quarantined=True,
            reason='incomplete_final',previous_raw_sha256=policy.digest(self.a),
            received_raw_sha256=policy.digest(incoming),received_status='FT')
        next_event = event(incoming,'2026-10-26T10:00:00Z',previous=self.a,selected=selected,
                           last=self.r1,decision='quarantined')
        self.assertEqual(self.result([self.r1,next_event])['selected'][0]['raw'],selected)

    def test_review_can_clear_quality_only_after_its_actual_local_observation(self):
        incoming = raw(halftime=(None,None))
        selected = deepcopy(self.a)
        selected[policy.QUALITY_KEY] = dict(version=policy.VERSION,quarantined=True,
            reason='incomplete_final',previous_raw_sha256=policy.digest(self.a),
            received_raw_sha256=policy.digest(incoming),received_status='FT')
        quarantined = event(incoming,'2026-10-26T10:00:00Z',previous=self.a,selected=selected,
                            last=self.r1,decision='quarantined')
        reviewed = event(incoming,'2026-10-26T11:00:00Z',previous=selected,selected=self.a,
                         last=quarantined,decision='reviewed')
        records=[self.r1,quarantined,reviewed]
        self.assertEqual(self.result(records,at='2026-10-26T11:00:00Z')['selected'][0]['raw'],selected)
        self.assertEqual(self.result(records)['selected'][0]['raw'],self.a)

    def test_impossible_selected_hash_or_bad_payload_is_excluded_not_repaired(self):
        unknown = reseal(self.r1,selected_raw_sha256='f'*64)
        tampered = dict(self.r1,received_raw=self.b)
        for broken in (unknown,tampered):
            result = self.result([broken])
            self.assertFalse(result['selected'])
            self.assertEqual(result['excluded'][0]['match_id'],'123')

    def test_missing_predecessor_fork_and_retrograde_clock_are_rejected(self):
        early = reseal(self.r2,observed_at_utc='2026-10-25T09:00:00Z')
        fork = event(raw(halftime=(1,1)),'2026-10-26T11:00:00Z',previous=self.a,last=self.r1)
        for records,reason in (([self.r2],'incomplete_chain'),
                               ([self.r1,self.r2,fork],'ambiguous_chain'),
                               ([self.r1,early],'retrograde_observation_time')):
            result = self.result(records)
            self.assertFalse(result['selected'])
            self.assertEqual(result['excluded'][0]['reason'],reason)

    def test_backdated_child_cannot_use_parent_that_was_not_known_at_cutoff(self):
        backdated = reseal(self.r2,observed_at_utc='2026-10-25T09:00:00Z')
        result = self.result([self.r1,backdated],at='2026-10-25T09:30:00Z')
        self.assertEqual(result['excluded'][0]['reason'],'incomplete_chain')

    def test_sql_index_columns_are_checked_and_sequences_follow_append_order(self):
        def wrapped(record,sequence,**values):
            return dict(record=record,match_id=record['match_id'],observed_at_utc=record['observed_at_utc'],
                        digest=record['id'],sequence=sequence,**values)
        valid = [wrapped(self.r1,1),wrapped(self.r2,2)]
        self.assertEqual(self.result(valid)['selected'][0]['raw'],self.b)
        for field,value in (('match_id','999'),('observed_at_utc','2026-10-25T09:00:00Z'),
                            ('digest','f'*64),('sequence',False)):
            invalid = deepcopy(valid); invalid[0][field]=value
            self.assertFalse(self.result(invalid)['selected'])
        decreasing = deepcopy(valid);decreasing[1]['sequence']=1
        self.assertEqual(self.result(decreasing)['excluded'][0]['reason'],'retrograde_sequence')

    def test_aware_instants_normalize_to_utc_and_naive_instants_are_rejected(self):
        self.assertEqual(self.result([self.r1],at='2026-10-25T12:00:01+02:00')['selected'][0]['observed_at_utc'],
                         '2026-10-25T10:00:00+00:00')
        self.assertTrue(self.result([self.r1],at=datetime(2026,10,27,tzinfo=timezone.utc))['selected'])
        for at in (None,True,datetime(2026,10,27),'2026-10-27T00:00:00'):
            with self.subTest(at=at),self.assertRaises(ValueError):
                self.result([self.r1],at=at)
        bad = reseal(self.r1,observed_at_utc='2026-10-25T10:00:00')
        self.assertFalse(self.result([bad])['selected'])

    def test_bad_match_does_not_hide_other_valid_fixture_or_create_any_input_mutation(self):
        other = event(raw(456),'2026-10-25T10:00:00Z')
        broken = dict(self.r1,received_raw=self.b)
        inputs = [broken,other]
        before=deepcopy(inputs)
        result=self.result(inputs)
        self.assertEqual([item['match_id'] for item in result['selected']],['456'])
        self.assertEqual([item['match_id'] for item in result['excluded']],['123'])
        self.assertEqual(inputs,before)


if __name__=='__main__':
    unittest.main()
