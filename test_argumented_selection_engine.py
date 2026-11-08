"""Synthetic evidence for the fixed personal shortlist; no model or data I/O."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

import argumented_selection_engine as engine


NOW = datetime(2026, 10, 22, 9, tzinfo=timezone.utc)
VALUES = engine.VALUES


def distribution(choice='2nd', probability=.6):
    other = (1-probability)/2
    return {value: probability if value == choice else other for value in VALUES}


def scan_row(mid=100001, choice='2nd', *, probability=.6, stamp='2026-10-22T15:00:00',
             league='61', home_id='611', away_id='612'):
    return dict(
        fixture=dict(match_id=str(mid), league=league, competition_code=league,
                     country='France', competition='Synthetic league', season=2026,
                     home='Home', away='Away', home_id=home_id, away_id=away_id,
                     timestamp=stamp, provider='api-football', status='TIMED',
                     raw_status='NS', is_upcoming=True, is_historical=False, selectable=True),
        probabilities=distribution(choice, probability), baseline=distribution(choice, .5),
        home_profile=dict(matches=40, scored=[20, 30], conceded=[10, 12]),
        away_profile=dict(matches=35, scored=[15, 25], conceded=[14, 21]),
        personalized=True, training=100, history_days=30,
        before='2026-10-22', history_fingerprint='a'*64,
        expected_goals=[1.1, 1.4], choice=choice, max_probability=probability, rank=8)


def scan(rows=None, *, day='2026-10-22', before='2026-10-22'):
    return dict(source='API-Football', version='existing-scan-version', day=day,
                before=before, rows=[scan_row()] if rows is None else rows,
                history_fingerprint='b'*64, excluded=[], diagnostics={})


def historical(mid, choice='2nd', *, role='home', stamp='2026-10-01T12:00:00',
               league='61', team_id=None):
    ht, ft = {'1st': ('2-0', '2-0'), 'Égal': ('1-0', '1-1'),
              '2nd': ('0-0', '2-0')}[choice]
    home_id = (team_id or '611') if role == 'home' else str(900000+mid)
    away_id = (team_id or '612') if role == 'away' else str(800000+mid)
    return dict(match_id=str(mid), league=league, competition_code=league,
                country='France', competition='Synthetic league', season=2026,
                home='Home' if role == 'home' else 'Opponent',
                away='Away' if role == 'away' else 'Opponent',
                home_id=home_id, away_id=away_id, timestamp=stamp,
                day=stamp[:10], ht=ht, ft=ft, value=choice,
                provider='api-football', source='https://www.api-football.com/')


def history(choice='2nd', *, home_values=None, away_values=None, count=10):
    output = []
    for offset, role, values in ((0, 'home', home_values), (100, 'away', away_values)):
        values = [choice]*count if values is None else values
        for index, value in enumerate(values):
            stamp = (datetime(2026, 10, 1, 12)+timedelta(days=index)).isoformat()
            output.append(historical(offset+index+1, value, role=role, stamp=stamp))
    return output


class ArgumentedSelectionEngineTests(unittest.TestCase):
    def result(self, report=None, rows=None, now=NOW):
        return engine.build(scan() if report is None else report,
                            history() if rows is None else rows, now)

    def reason(self, result):
        self.assertFalse(result['rows'])
        return result['excluded'][-1]['reason']

    def test_all_three_true_model_favourites_can_qualify_without_forcing_second(self):
        for choice in VALUES:
            with self.subTest(choice=choice):
                original = scan([scan_row(choice=choice)])
                result = self.result(original, history(choice))
                row = result['rows'][0]
                self.assertEqual(row['choice'], choice)
                self.assertEqual(row['selection_probability'], original['rows'][0]['probabilities'][choice])
                self.assertEqual(row['baseline_probability'], .5)
                self.assertAlmostEqual(row['delta_points'], 10)
                self.assertEqual(row['recent_home']['matching'], 10)
                self.assertEqual(row['recent_away']['matching'], 10)
                self.assertEqual(row['selection_version'], engine.VERSION)
                self.assertEqual(row['criteria'], engine.CRITERIA)
                self.assertTrue(all(isinstance(reason, str) and reason for reason in row['reasons']))

    def test_keeps_every_qualifier_sorted_by_probability_kickoff_and_numeric_id(self):
        rows = [scan_row(mid, probability=.55) for mid in range(200, 361)]
        rows += [scan_row(10, probability=.75, stamp='2026-10-22T16:00:00'),
                 scan_row(9, probability=.75, stamp='2026-10-22T14:00:00'),
                 scan_row(2, probability=.75, stamp='2026-10-22T14:00:00')]
        result = self.result(scan(list(reversed(rows))))
        self.assertEqual(len(result['rows']), len(rows))
        self.assertEqual([row['fixture']['match_id'] for row in result['rows'][:3]], ['2', '9', '10'])
        self.assertEqual(result['diagnostics']['selected_count'], 164)

    def test_uses_all_baseline_and_model_probabilities_without_positive_delta_threshold(self):
        row = scan_row()
        row['baseline'] = distribution('2nd', .8)
        result = self.result(scan([row]))
        self.assertEqual(len(result['rows']), 1)
        self.assertAlmostEqual(result['rows'][0]['delta_points'], -20)

    def test_preserves_scan_fields_and_does_not_mutate_inputs_or_share_proof_objects(self):
        report, rows = scan(), history()
        original_report, original_rows = deepcopy(report), deepcopy(rows)
        result = self.result(report, rows)
        selected = result['rows'][0]
        for key, value in original_report['rows'][0].items():
            self.assertEqual(selected[key], value, key)
        self.assertEqual(report, original_report)
        self.assertEqual(rows, original_rows)
        selected['home_profile']['matches'] = 0
        selected['recent_home']['rows'][0]['ht'] = '9-9'
        selected['criteria']['minimum_full_profile'] = 0
        result['criteria']['minimum_full_profile'] = 0
        self.assertEqual(report, original_report)
        self.assertEqual(rows, original_rows)
        self.assertEqual(engine.CRITERIA['minimum_full_profile'], 10)

    def test_model_baseline_disagreement_is_excluded_even_if_another_category_confirms(self):
        row = scan_row(choice='1st')
        row['baseline'] = distribution('2nd')
        self.assertEqual(self.reason(self.result(scan([row]), history('2nd'))), 'model_baseline_disagreement')

    def test_uses_true_argmax_instead_of_a_preexisting_search_or_choice_field(self):
        row = scan_row(choice='1st')
        row.update(choice='2nd', search_outcome='2nd', max_probability=.99, search_probability=.99)
        selected = self.result(scan([row]), history('1st'))['rows'][0]
        self.assertEqual(selected['choice'], '1st')
        self.assertEqual(selected['selection_probability'], .6)
        self.assertEqual(selected['search_outcome'], '2nd')

    def test_argmax_tie_order_is_fixed_and_identical_for_model_and_baseline(self):
        row = scan_row(choice='1st')
        row['probabilities'] = {'1st': .45, 'Égal': .1, '2nd': .45}
        row['baseline'] = {'1st': .5, 'Égal': 0, '2nd': .5}
        selected = self.result(scan([row]), history('1st'))['rows'][0]
        self.assertEqual(selected['choice'], '1st')

    def test_minimum_ten_in_both_full_profiles(self):
        for role in ('home', 'away'):
            with self.subTest(role=role):
                row = scan_row()
                row[f'{role}_profile']['matches'] = 9
                self.assertEqual(self.reason(self.result(scan([row]))), 'insufficient_full_profile')
        row = scan_row()
        row['home_profile']['matches'] = row['away_profile']['matches'] = 10
        self.assertEqual(len(self.result(scan([row]))['rows']), 1)

    def test_requires_exactly_ten_valid_available_precedents_in_each_role(self):
        for role in ('home', 'away'):
            with self.subTest(role=role):
                rows = history()
                rows.pop(0 if role == 'home' else -1)
                result = self.result(rows=rows)
                self.assertEqual(self.reason(result), f'insufficient_recent_{role}')

    def test_five_of_ten_and_joint_modal_category_is_accepted(self):
        five = ['1st']*5 + ['2nd']*5
        result = self.result(scan([scan_row(choice='1st')]),
                             history('1st', home_values=five, away_values=five))
        row = result['rows'][0]
        self.assertEqual(row['recent_home']['counts'], {'1st': 5, 'Égal': 0, '2nd': 5})
        self.assertEqual(row['recent_home']['matching'], 5)

    def test_four_of_ten_is_rejected_even_when_it_is_modal(self):
        four = ['2nd']*4+['1st']*3+['Égal']*3
        for role in ('home', 'away'):
            with self.subTest(role=role):
                kwargs = {f'{role}_values': four}
                self.assertEqual(self.reason(self.result(rows=history(**kwargs))), f'recent_{role}_not_confirmed')

    def test_a_nonmodal_selected_category_is_excluded(self):
        rows = history(home_values=['2nd']*4+['1st']*6)
        self.assertEqual(self.reason(self.result(rows=rows)), 'recent_home_not_confirmed')

    def test_last_available_ten_ignore_older_pattern_and_can_span_years(self):
        rows = history()
        rows[0].update(timestamp='2015-10-01T12:00:00', day='2015-10-01', season=2015)
        rows.append(historical(999, '1st', stamp='2010-01-01T12:00:00'))
        selected = self.result(rows=list(reversed(rows)))['rows'][0]
        self.assertEqual(selected['recent_home']['matches'], 10)
        self.assertEqual(selected['recent_home']['first_at'], '2015-10-01T12:00:00')
        self.assertNotIn('999', selected['recent_home']['ids'])
        self.assertIn('plusieurs années', selected['reasons'][-1])

    def test_newest_ten_replace_old_confirmation_instead_of_cherry_picking(self):
        rows = history()
        rows += [historical(200+i, '1st', stamp=f'2026-10-{11+i:02d}T12:00:00') for i in range(6)]
        self.assertEqual(self.reason(self.result(rows=rows)), 'recent_home_not_confirmed')

    def test_same_league_and_exact_team_id_and_venue_are_required_names_do_not_join(self):
        for alteration in ('league', 'home_id', 'venue'):
            with self.subTest(alteration=alteration):
                rows = history()
                if alteration == 'league':
                    rows[0].update(league='39', competition_code='39')
                elif alteration == 'home_id':
                    rows[0]['home_id'] = '9999'  # identical name cannot rescue this row
                else:
                    rows[0]['home_id'], rows[0]['away_id'] = rows[0]['away_id'], rows[0]['home_id']
                self.assertEqual(self.reason(self.result(rows=rows)), 'insufficient_recent_home')
        rows = history()
        for row in rows:
            row['home'], row['away'] = 'Renamed home', 'Renamed away'
        self.assertEqual(len(self.result(rows=rows)['rows']), 1)

    def test_exact_duplicates_count_once_and_conflicting_history_ids_are_entirely_removed(self):
        rows = history()
        exact = self.result(rows=rows+[deepcopy(rows[0])])
        self.assertEqual(exact['diagnostics']['history_count'], 20)
        self.assertEqual(exact['diagnostics']['history_duplicate_count'], 1)
        conflict = deepcopy(rows[0])
        conflict.update(ht='2-0', ft='2-0', value='1st')
        first, second = self.result(rows=rows+[conflict]), self.result(rows=list(reversed(rows+[conflict])))
        self.assertEqual(self.reason(first), 'insufficient_recent_home')
        self.assertEqual(first['rows'], second['rows'])
        self.assertEqual(first['diagnostics']['invalid_history'], second['diagnostics']['invalid_history'])
        self.assertEqual(first['diagnostics']['invalid_history'][0]['reason'], 'conflicting_history_duplicate')

    def test_exact_fixture_duplicates_deduplicate_and_conflicting_ids_are_excluded(self):
        row = scan_row()
        result = self.result(scan([row, deepcopy(row)]))
        self.assertEqual(len(result['rows']), 1)
        self.assertEqual(result['diagnostics']['fixture_duplicate_count'], 1)
        conflict = deepcopy(row)
        conflict['probabilities'] = distribution('2nd', .7)
        result = self.result(scan([row, conflict]))
        self.assertEqual(self.reason(result), 'conflicting_fixture_duplicate')

    def test_missing_bad_or_contradictory_scores_and_labels_never_count(self):
        changes = [dict(ht=None), dict(ft=None), dict(ht='-1-0'), dict(ft='2.0-0'),
                   dict(ht='3-0', ft='2-0'), dict(value='1st'), dict(value=None),
                   dict(ht=True), dict(ft={'home': 2, 'away': 0}),
                   dict(ht='(2-0'), dict(ft='2-0)'), dict(ht='9'*5000+'-0')]
        for change in changes:
            with self.subTest(change=change):
                rows = history()
                rows[0].update(change)
                result = self.result(rows=rows)
                self.assertEqual(self.reason(result), 'insufficient_recent_home')
                self.assertEqual(result['diagnostics']['invalid_history_count'], 1)

    def test_outcome_uses_total_goals_in_each_half_not_home_win_or_ft_total(self):
        rows = history('1st')
        for row in rows:
            row.update(ht='0-2', ft='0-3', value='1st')
        result = self.result(scan([scan_row(choice='1st')]), rows)
        self.assertEqual(len(result['rows']), 1)
        self.assertEqual(result['rows'][0]['recent_home']['counts']['1st'], 10)

    def test_score_validation_runs_for_whole_history_before_any_count(self):
        rows = history()
        unrelated = historical(999, team_id='777')
        unrelated['value'] = '1st'
        rows.append(unrelated)
        result = self.result(rows=rows)
        self.assertEqual(len(result['rows']), 1)
        self.assertEqual(result['diagnostics']['invalid_history_count'], 1)
        self.assertEqual(result['diagnostics']['history_count'], 20)

    def test_foreign_omitted_or_contradictory_sources_never_supply_scores(self):
        for change in (dict(provider='forebet'), dict(provider=None), dict(source='forebet')):
            with self.subTest(change=change):
                rows = history()
                rows[0].update(change)
                result = self.result(rows=rows)
                self.assertEqual(self.reason(result), 'insufficient_recent_home')
                self.assertEqual(result['diagnostics']['invalid_history_count'], 1)
        rows = history()
        foreign_same_id = deepcopy(rows[0])
        foreign_same_id.update(provider='forebet', ht='9-0', ft='9-0', value='1st')
        self.assertEqual(len(self.result(rows=rows+[foreign_same_id])['rows']), 1)

    def test_invalid_ids_and_dates_do_not_fall_back_to_names_or_slices(self):
        changes = [dict(home_id=None), dict(match_id=True), dict(league='bad'),
                   dict(home_id='-2'), dict(home_id='٢'),
                   dict(timestamp='2026-10-01'), dict(day='2026-10-02'),
                   dict(competition_code='99')]
        for change in changes:
            with self.subTest(change=change):
                rows = history()
                rows[0].update(change)
                result = self.result(rows=rows)
                self.assertEqual(self.reason(result), 'insufficient_recent_home')
                self.assertEqual(result['diagnostics']['invalid_history_count'], 1)

    def test_strict_before_report_cutoff_and_current_utc_day_excludes_future_results(self):
        rows = history()
        injected = [historical(700+i, '1st', stamp=stamp) for i, stamp in enumerate(
            ('2026-10-22T00:00:00', '2026-10-22T08:00:00',
             '2026-10-22T20:00:00', '2026-10-23T12:00:00'))]
        original = self.result(rows=rows)
        future = self.result(rows=rows+injected)
        self.assertEqual(future['rows'], original['rows'])
        self.assertEqual(future['diagnostics']['outside_history_cutoff_count'], 4)
        report = scan(before='2026-10-10')
        report['rows'][0]['before'] = '2026-10-10'
        self.assertEqual(self.reason(self.result(report, rows)), 'insufficient_recent_home')

    def test_later_conflicting_id_never_suppresses_an_available_past_match(self):
        rows = history()
        original = self.result(rows=rows)
        for stamp in ('2026-10-22T00:00:00', '2026-10-23T12:00:00'):
            with self.subTest(stamp=stamp):
                later = deepcopy(rows[0])
                later.update(timestamp=stamp, day=stamp[:10], ht=None, ft=None, value=None)
                after = self.result(rows=rows+[later])
                self.assertEqual(after['rows'], original['rows'])
                self.assertEqual(after['diagnostics']['invalid_history'], [])
                self.assertEqual(after['diagnostics']['outside_history_cutoff_count'], 1)

    def test_invalid_records_are_audited_without_aborting_valid_rows(self):
        rows = history()+[None, {}, historical(999)]
        rows[-1]['timestamp'] = '9999-12-31T23:00:00-02:00'
        result = self.result(rows=rows)
        self.assertEqual(len(result['rows']), 1)
        self.assertEqual(result['diagnostics']['invalid_history_count'], 3)
        report = scan([None, {}, scan_row()])
        self.assertEqual(len(self.result(report)['rows']), 1)

    def test_cutoff_respects_fixture_utc_day_at_kinshasa_midnight(self):
        current = datetime.fromisoformat('2026-10-21T23:01:00+00:00')
        row = scan_row(stamp='2026-10-21T23:05:00')
        row['before'] = '2026-10-21'
        report = scan([row], day='2026-10-22', before='2026-10-21')
        rows = history()+[historical(999, '1st', stamp='2026-10-21T01:00:00')]
        selected = self.result(report, rows, current)['rows'][0]
        self.assertEqual(selected['recent_home']['last_at'], '2026-10-10T12:00:00')
        self.assertIn('2026-10-21 UTC', selected['reasons'][-1])

    def test_timezone_offsets_are_compared_as_utc_and_normalized_in_proof(self):
        rows = history()
        rows[0].update(timestamp='2026-10-01T14:00:00+02:00', day='2026-10-01')
        result = self.result(rows=rows)
        self.assertEqual(result['rows'][0]['recent_home']['first_at'], '2026-10-01T12:00:00')
        rows[0].update(timestamp='2026-10-22T00:30:00+01:00', day='2026-10-21')
        self.assertEqual(len(self.result(rows=rows)['rows']), 1)

    def test_future_selected_day_still_uses_current_utc_history_boundary(self):
        row = scan_row(stamp='2026-10-23T15:00:00')
        report = scan([row], day='2026-10-23')
        rows = history()+[historical(700, '1st', stamp='2026-10-22T01:00:00')]
        result = self.result(report, rows)
        self.assertEqual(len(result['rows']), 1)
        self.assertEqual(result['diagnostics']['outside_history_cutoff_count'], 1)
        self.assertEqual(result['before'], '2026-10-22')

    def test_unsafe_model_cutoff_and_inconsistent_row_cutoff_are_rejected(self):
        row = scan_row()
        row['before'] = '2026-10-23'
        self.assertEqual(self.reason(self.result(scan([row], before='2026-10-23'))), 'unsafe_scan_cutoff')
        row = scan_row()
        row['before'] = '2026-10-21'
        self.assertEqual(self.reason(self.result(scan([row]))), 'inconsistent_scan_cutoff')

    def test_no_non_ns_expired_unconfirmed_or_wrong_day_fixtures(self):
        changes = [(dict(raw_status='FT'), 'not_upcoming'),
                   (dict(raw_status='TBD'), 'not_upcoming'),
                   (dict(status='FINISHED'), 'not_upcoming'),
                   (dict(is_upcoming=False), 'not_upcoming'),
                   (dict(is_historical=True), 'not_upcoming'),
                   (dict(selectable=False), 'not_upcoming'),
                   (dict(timestamp=NOW.replace(tzinfo=None).isoformat()), 'kickoff_elapsed'),
                   (dict(timestamp='2026-10-22T08:00:00'), 'kickoff_elapsed'),
                   (dict(timestamp='2026-10-22T23:00:00'), 'wrong_day'),
                   (dict(home='TBD'), 'unconfirmed_fixture'),
                   (dict(home_id='612'), 'invalid_identity'),
                   (dict(provider=None), 'foreign_provider')]
        for change, reason in changes:
            with self.subTest(change=change):
                row = scan_row()
                row['fixture'].update(change)
                self.assertEqual(self.reason(self.result(scan([row]))), reason)

    def test_target_fixture_must_be_score_free_and_results_are_not_used(self):
        for key in ('score', 'ht', 'ft', 'value', 'goals', 'outcome'):
            with self.subTest(key=key):
                row = scan_row()
                row['fixture'][key] = object()  # presence is sufficient; value never inspected
                self.assertEqual(self.reason(self.result(scan([row]))), 'fixture_contains_result')

    def test_invalid_probability_distributions_are_never_repaired_or_rounded(self):
        invalid = [None, {}, {'1st': .5, '2nd': .5},
                   {'1st': .2, 'Égal': .2, '2nd': .2},
                   {'1st': .2, 'Égal': -.1, '2nd': .9},
                   {'1st': True, 'Égal': 0, '2nd': 0},
                   {'1st': float('nan'), 'Égal': .2, '2nd': .8},
                   {'1st': float('inf'), 'Égal': .2, '2nd': .8},
                   {'1st': '0.2', 'Égal': .2, '2nd': .6}]
        for key in ('probabilities', 'baseline'):
            for value in invalid:
                with self.subTest(key=key, value=value):
                    row = scan_row()
                    row[key] = value
                    self.assertEqual(self.reason(self.result(scan([row]))), 'invalid_probabilities')

    def test_original_scan_exclusions_remain_visible(self):
        report = scan()
        original = dict(match_id='555', reason='insufficient_league_history', detail='12/30 résultats.')
        report['excluded'].append(original)
        result = self.result(report)
        self.assertEqual(result['excluded'], [original])
        self.assertEqual(result['diagnostics']['scan_excluded_count'], 1)

    def test_report_and_clock_validation(self):
        cases = [None, {}, dict(scan(), source='Forebet'), dict(scan(), rows=None),
                 dict(scan(), day='2026-99-01'), dict(scan(), before='20261022'),
                 dict(scan(), day=NOW.date()), dict(scan(), excluded={}),
                 dict(scan(), excluded=[None])]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.result(value if value is not None else {})
        with self.assertRaises(ValueError):
            engine.build(scan(), history(), NOW.replace(tzinfo=None))
        with self.assertRaises(ValueError):
            engine.build(scan(), None, NOW)

    def test_frozen_row_proof_validation_rejects_tampering(self):
        selected = self.result()['rows'][0]
        expected = engine.validate_row(selected, day='2026-10-22', before='2026-10-22', now=NOW)
        self.assertEqual(expected, selected)
        changes = [('choice', '1st'), ('selection_probability', .99),
                   ('baseline_probability', .01), ('delta_points', 99),
                   ('selection_version', 'unknown'), ('criteria', {}),
                   ('reasons', ['invented'])]
        for key, value in changes:
            with self.subTest(key=key), self.assertRaises(ValueError):
                changed = deepcopy(selected)
                changed[key] = value
                engine.validate_row(changed, day='2026-10-22', before='2026-10-22', now=NOW)
        changed = deepcopy(selected)
        changed['recent_home']['matching'] = 9
        with self.assertRaises(ValueError):
            engine.validate_row(changed, day='2026-10-22', before='2026-10-22', now=NOW)


if __name__ == '__main__':
    unittest.main()
