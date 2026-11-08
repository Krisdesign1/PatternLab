from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math
import unittest

import causal_confirmation_followup as followup


START = '2026-11-02T00:00:00+00:00'
END = '2026-12-28T00:00:00+00:00'
CUTOFF = '2027-01-04T08:00:00+00:00'


class FollowupTests(unittest.TestCase):
    def protocol(self, *, final=True):
        start = datetime.fromisoformat(START)
        days, attempts, rows = [], [], []
        for offset in range(56):
            date = start+timedelta(days=offset)
            day = date.date().isoformat()
            week = offset//7
            # Different weekly effect sizes, deliberately unequal block counts.
            count = week+1 if offset % 7 == 0 else 0
            days.append(dict(day=day, targets=count, captures=count, state='anchored'))
            attempts.append(dict(id=f'closure-{offset}', kind='window_closed', day=day,
                                 observed_at_utc=(date+timedelta(hours=9)).isoformat()))
            attempts.append(dict(id=f'closure-ack-{offset}', kind='window_closed_ack', day=day,
                                 closure_id=f'closure-{offset}', time_quality='local_closure_commit_ack',
                                 observed_at_utc=(date+timedelta(hours=9, seconds=1)).isoformat()))
            for index in range(count):
                rows.append(dict(forecast_id=f'f-{offset}-{index}',
                    fixture=dict(match_id=f'{offset}-{index}', league='39',
                                 timestamp=(date+timedelta(hours=12)).isoformat()),
                    status='settled', admissible=True, timing_quality='local_commit_ack', actual='2nd',
                    methods=dict(candidate={'1st': .2, 'Égal': .2-week*.01, '2nd': .6+week*.01},
                                 clubs={'1st': .3, 'Égal': .2, '2nd': .5},
                                 frequency={'1st': .3, 'Égal': .3, '2nd': .4})))
        return dict(id='protocol', registered_at='2026-10-31T20:00:00+00:00',
                    implementation_compatible=True,
                    confirmation_start=START, confirmation_end=END,
                    followup=followup.make_plan(START, END), rows=rows, days=days, attempts=attempts,
                    generated_at=CUTOFF if final else '2026-12-29T10:00:00+00:00',
                    endpoint=dict(id='endpoint', prepared_at=CUTOFF, result_cutoff_utc=CUTOFF) if final else None,
                    result_cutoff_utc=CUTOFF if final else None)

    def test_plan_has_eight_complete_weeks_and_fixed_endpoint(self):
        plan = followup.make_plan(START, END)
        self.assertEqual(plan['result_cutoff_utc'], CUTOFF)
        self.assertEqual(plan['min_blocks'], 8)
        self.assertEqual(followup.validate_plan(plan, START, END), plan)
        for start, end in [('2026-11-01T00:00:00Z', '2026-12-27T00:00:00Z'),
                           (START, '2026-12-21T00:00:00Z'),
                           ('2026-11-02T01:00:00Z', END)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                followup.make_plan(start, end)

    def test_criteria_cannot_be_changed_or_bool_impersonate_integer(self):
        for key, value in [('min_blocks', 7), ('weeks', 8.0), ('automatic_promotion', 0),
                           ('result_cutoff_utc', '2027-01-05T08:00:00Z')]:
            plan = followup.make_plan(START, END)
            plan[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                followup.validate_plan(plan, START, END)

    def test_primary_is_count_weighted_paired_loss_not_mean_of_week_means(self):
        report = followup.build(self.protocol())
        expected = sum((week+1)*math.log(.5/(.6+week*.01)) for week in range(8))/36
        simple = sum(math.log(.5/(.6+week*.01)) for week in range(8))/8
        self.assertAlmostEqual(report['primary']['estimate'], expected, places=14)
        self.assertNotAlmostEqual(report['primary']['estimate'], simple, places=4)
        self.assertEqual(report['primary']['count'], 36)
        self.assertEqual(report['coverage']['eligible_weeks'], 8)
        self.assertTrue(report['inference']['available'])
        self.assertFalse(report['automatic_promotion'])
        self.assertFalse(report['predictive_improvement_demonstrated'])

    def test_final_interval_is_deterministic_and_source_hash_is_order_stable(self):
        protocol = self.protocol()
        first = followup.build(protocol)
        protocol['rows'].reverse()
        protocol['days'].reverse()
        protocol['attempts'].reverse()
        second = followup.build(protocol)
        self.assertEqual(first['inference'], second['inference'])
        self.assertEqual(first['source_sha256'], second['source_sha256'])
        self.assertLess(first['inference']['confidence_interval']['upper'], 0)
        self.assertNotIn('favorable', first)

    def test_no_interim_interval_even_when_all_weeks_are_complete(self):
        report = followup.build(self.protocol(final=False))
        self.assertEqual(report['coverage']['eligible_weeks'], 8)
        self.assertFalse(report['inference']['available'])
        self.assertEqual(report['inference']['status'], 'awaiting_fixed_endpoint')

    def test_final_requires_exact_fixed_result_cutoff(self):
        for value in (None, '2027-01-05T08:00:00+00:00'):
            protocol = self.protocol()
            protocol['result_cutoff_utc'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                followup.build(protocol)

    def test_late_registration_or_future_read_is_refused(self):
        for registered, generated in [('2026-11-02T08:00:00Z', CUTOFF),
                                      ('2026-10-31T20:00:00Z', '2026-10-31T19:00:00Z')]:
            protocol = self.protocol()
            protocol.update(registered_at=registered, generated_at=generated)
            with self.subTest(registered=registered, generated=generated), self.assertRaises(ValueError):
                followup.build(protocol)

    def test_pending_or_unresolved_blocks_week_and_never_counts_as_loss(self):
        for status in ('pending', 'unresolved'):
            protocol = self.protocol()
            protocol['rows'][0].update(status=status, actual=None)
            result = followup.build(protocol)
            self.assertFalse(result['weeks'][0]['eligible'])
            self.assertIn('unresolved_results', result['weeks'][0]['reasons'])
            self.assertEqual(result['primary']['count'], 35)
            self.assertEqual(result['counts']['excluded'], 1)
            self.assertEqual(result['inference']['status'], 'insufficient_complete_weeks')

    def test_void_is_known_exclusion_not_pending_or_loss(self):
        protocol = self.protocol()
        # Week eight still retains seven other evaluable observations.
        protocol['rows'][-1].update(status='void', actual=None)
        result = followup.build(protocol)
        self.assertTrue(result['weeks'][-1]['eligible'])
        self.assertEqual(result['primary']['count'], 35)
        self.assertEqual(result['counts']['exclusion_reasons']['void'], 1)
        self.assertTrue(result['inference']['available'])

    def test_missing_census_or_closure_excludes_week(self):
        for kind in ('census', 'closure'):
            protocol = self.protocol()
            if kind == 'census':
                # A zero-match day is unknown, not an attested empty census.
                protocol['days'][1].update(state='missed', targets=None)
            else:
                protocol['attempts'].pop(1)
            result = followup.build(protocol)
            self.assertFalse(result['weeks'][0]['eligible'])
            self.assertFalse(result['inference']['available'])
            self.assertEqual(result['coverage']['eligible_weeks'], 7)

    def test_missed_target_is_visible_but_not_imputed_as_a_loss(self):
        protocol = self.protocol()
        protocol['days'][0]['targets'] += 2
        result = followup.build(protocol)
        self.assertEqual(result['coverage']['missing_captures_known'], 2)
        self.assertAlmostEqual(result['weeks'][0]['capture_coverage'], 1/3)
        self.assertEqual(result['primary']['count'], 36)
        self.assertIn('conditional', ' '.join(result['limitations']))

    def test_open_week_is_never_eligible(self):
        protocol = self.protocol(final=False)
        protocol['generated_at'] = '2026-12-26T12:00:00Z'
        result = followup.build(protocol)
        self.assertFalse(result['weeks'][-1]['eligible'])
        self.assertIn('open_week', result['weeks'][-1]['reasons'])

    def test_closure_ack_after_fixed_endpoint_cannot_change_final_eligibility(self):
        protocol = self.protocol()
        protocol['attempts'][1]['observed_at_utc'] = '2027-01-05T12:00:00Z'
        protocol['generated_at'] = '2027-01-10T12:00:00Z'
        report = followup.build(protocol)
        self.assertFalse(report['weeks'][0]['eligible'])
        self.assertFalse(report['inference']['available'])

    def test_early_closure_is_invalid(self):
        protocol = self.protocol()
        protocol['attempts'][0]['observed_at_utc'] = '2026-11-02T08:29:59Z'
        with self.assertRaises(ValueError):
            followup.build(protocol)

    def test_empty_future_has_unknown_coverage_and_no_zero_success(self):
        protocol = self.protocol(final=False)
        protocol.update(rows=[], days=[], attempts=[], generated_at='2026-10-31T21:00:00Z')
        report = followup.build(protocol)
        self.assertEqual(report['coverage']['unknown_census_days'], 56)
        self.assertIsNone(report['primary']['estimate'])
        self.assertEqual(report['metrics'], {})
        self.assertTrue(all(week['capture_coverage'] is None for week in report['weeks']))

    def test_old_protocol_has_no_invented_followup(self):
        report = followup.build(dict(id='old-protocol'))
        self.assertFalse(report['registered'])
        self.assertEqual(report['inference']['status'], 'not_registered')

    def test_duplicate_fixture_or_forecast_is_refused(self):
        for field in ('fixture', 'forecast_id'):
            protocol = self.protocol()
            protocol['rows'][1][field] = deepcopy(protocol['rows'][0][field])
            with self.subTest(field=field), self.assertRaises(ValueError):
                followup.build(protocol)

    def test_counts_cannot_hide_or_invent_forecasts(self):
        protocol = self.protocol()
        protocol['days'][0]['captures'] = 0
        with self.assertRaises(ValueError):
            followup.build(protocol)
        protocol = self.protocol()
        protocol['days'][1].update(targets=1, captures=1)
        with self.assertRaises(ValueError):
            followup.build(protocol)

    def test_certain_equal_block_effects_do_not_create_zero_width_certainty(self):
        protocol = self.protocol()
        for row in protocol['rows']:
            row['methods']['candidate'] = deepcopy(row['methods']['clubs'])
        result = followup.build(protocol)
        self.assertEqual(result['primary']['estimate'], 0)
        self.assertEqual(result['inference']['status'], 'degenerate_blocks')
        self.assertIsNone(result['inference']['confidence_interval'])

    def test_build_is_pure(self):
        protocol = self.protocol()
        before = deepcopy(protocol)
        followup.build(protocol)
        self.assertEqual(protocol, before)

    def test_incompatible_or_unknown_implementation_withholds_final_interval(self):
        for compatible, status in [(False, 'incompatible_implementation'),
                                   (None, 'unverified_implementation')]:
            protocol = self.protocol()
            original = followup.build(protocol)
            protocol['implementation_compatible'] = compatible
            result = followup.build(protocol)
            self.assertEqual(result['inference']['status'], status)
            self.assertFalse(result['inference']['available'])
            self.assertIsNone(result['inference']['confidence_interval'])
            self.assertEqual(result['primary'], original['primary'])
            self.assertNotEqual(result['source_sha256'], original['source_sha256'])
            self.assertIs(result['implementation_compatible'], compatible)

    def test_missing_implementation_metadata_is_not_treated_as_verified(self):
        protocol = self.protocol()
        protocol.pop('implementation_compatible')
        result = followup.build(protocol)
        self.assertFalse(result['inference']['available'])
        self.assertEqual(result['inference']['status'], 'unverified_implementation')

    def test_non_boolean_implementation_metadata_is_invalid(self):
        for value in (0, 1, 'true', {}):
            protocol = self.protocol()
            protocol['implementation_compatible'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                followup.build(protocol)

    def test_missing_or_synthetic_closure_ack_never_qualifies_week(self):
        for mode in ('missing', 'synthetic'):
            protocol = self.protocol()
            if mode == 'missing':
                protocol['attempts'].pop(1)
            else:
                protocol['attempts'][1]['time_quality'] = 'synthetic_unattested'
            result = followup.build(protocol)
            self.assertEqual(result['coverage']['closed_days'], 55)
            self.assertFalse(result['weeks'][0]['eligible'])
            self.assertIn('capture_window_not_closed', result['weeks'][0]['reasons'])
            self.assertFalse(result['inference']['available'])

    def test_closure_ack_cannot_precede_original_or_point_to_another_day(self):
        for field, value in [('observed_at_utc', '2026-11-02T08:59:59Z'),
                             ('day', '2026-11-03')]:
            protocol = self.protocol()
            protocol['attempts'][1][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                followup.build(protocol)

    def test_ambiguous_double_closure_ack_is_invalid(self):
        protocol = self.protocol()
        second = dict(protocol['attempts'][1], id='different-ack-id')
        protocol['attempts'].append(second)
        with self.assertRaises(ValueError):
            followup.build(protocol)

    def test_closure_ack_identifies_its_exact_original_event(self):
        protocol = self.protocol()
        protocol['attempts'][1]['closure_id'] = 'another-closure'
        result = followup.build(protocol)
        self.assertFalse(result['weeks'][0]['eligible'])
        protocol['attempts'][1]['closure_id'] = ''
        with self.assertRaises(ValueError):
            followup.build(protocol)

    def test_source_hash_includes_real_closure_ack_time(self):
        protocol = self.protocol()
        first = followup.build(protocol)
        protocol['attempts'][1]['observed_at_utc'] = '2026-11-02T09:00:02Z'
        second = followup.build(protocol)
        self.assertEqual(first['coverage'], second['coverage'])
        self.assertNotEqual(first['source_sha256'], second['source_sha256'])

    def test_unsealed_final_read_withholds_interval_but_keeps_observed_point(self):
        protocol = self.protocol()
        original = followup.build(protocol)
        protocol['endpoint'] = None
        result = followup.build(protocol)
        self.assertEqual(result['primary'], original['primary'])
        self.assertEqual(result['inference']['status'], 'awaiting_endpoint_freeze')
        self.assertFalse(result['inference']['available'])
        self.assertIsNone(result['endpoint'])
        self.assertNotEqual(result['source_sha256'], original['source_sha256'])

    def test_final_endpoint_must_match_fixed_cutoff_and_actual_read_time(self):
        for field, value in [('id', ''), ('prepared_at', '2027-01-04T07:59:59Z'),
                             ('prepared_at', '2027-01-04T08:00:01Z'),
                             ('result_cutoff_utc', '2027-01-05T08:00:00Z')]:
            protocol = self.protocol()
            protocol['endpoint'][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                followup.build(protocol)

    def test_endpoint_metadata_enters_output_and_source_hash(self):
        protocol = self.protocol()
        first = followup.build(protocol)
        protocol['endpoint']['id'] = 'another-endpoint'
        second = followup.build(protocol)
        self.assertNotEqual(first['source_sha256'], second['source_sha256'])
        self.assertEqual(second['endpoint'], protocol['endpoint'])

    def test_no_pre_cutoff_interval_can_be_unlocked_with_a_forged_endpoint(self):
        protocol = self.protocol(final=False)
        protocol['endpoint'] = dict(id='endpoint', prepared_at='2026-12-29T10:00:00Z',
                                    result_cutoff_utc=CUTOFF)
        with self.assertRaises(ValueError):
            followup.build(protocol)


if __name__ == '__main__':
    unittest.main()
