"""Meaningful pre-match identity, missingness and temporal-boundary checks."""
from copy import deepcopy
import unittest

from prematch_context_quality import audit_injuries, audit_lineups


def fixture():
    def lineup(team, start):
        return dict(team=dict(id=team), coach=dict(id=team + 100),
                    startXI=[dict(player=dict(id=start + i)) for i in range(11)],
                    substitutes=[dict(player=dict(id=start + 20))])
    return dict(fixture=dict(id=99, date='2026-11-07T18:00:00Z', status=dict(short='NS')),
                teams=dict(home=dict(id=1), away=dict(id=2)),
                lineups=[lineup(1, 1000), lineup(2, 2000)])


TIMES = dict(observed_at='2026-11-07T17:20:00Z', decision_at='2026-11-07T17:30:00Z')


class ContextQualityTests(unittest.TestCase):
    def test_foreign_provider_never_qualifies_context(self):
        raw=fixture(); raw['provider']='football-data'
        self.assertFalse(audit_lineups(raw, **TIMES)['usable_before_decision'])
        raw['injuries']=[dict(fixture=dict(id=99),team=dict(id=1),
            player=dict(id=123,type='Missing Fixture',reason='Injury'))]
        self.assertFalse(audit_injuries(raw, **TIMES)['usable_before_decision'])

    def test_malformed_quality_annotation_does_not_turn_into_permission(self):
        for annotation in ({'quarantined':'true'}, 'invalid', {'quarantined':1}):
            raw=fixture(); raw['_patternlab_import']=annotation
            self.assertFalse(audit_lineups(raw, **TIMES)['usable_before_decision'])
            self.assertIn('fixture_quality_annotation_malformed',audit_lineups(raw, **TIMES)['issues'])

    def test_valid_starting_elevens_and_no_input_mutation(self):
        raw = fixture(); before = deepcopy(raw)
        result = audit_lineups(raw, **TIMES)
        self.assertTrue(result['complete_starting_elevens'])
        self.assertTrue(result['usable_before_decision'])
        self.assertEqual(result['coach_ids'], ['101', '102'])
        self.assertEqual(raw, before)
        self.assertFalse(result['provider_completeness_proven'])

    def test_missingness_is_preserved(self):
        for state, value in [('absent', 'omit'), ('null', None), ('empty', []), ('malformed', {})]:
            with self.subTest(state=state):
                raw = fixture()
                if value == 'omit': raw.pop('lineups')
                else: raw['lineups'] = value
                result = audit_lineups(raw, **TIMES)
                self.assertEqual(result['availability'], state)
                self.assertFalse(result['complete_starting_elevens'])
                self.assertFalse(result['usable_before_decision'])

    def test_exact_two_fixture_identities_required(self):
        for change in ('one', 'third', 'duplicate', 'wrong', 'bool', 'no_fixture_id'):
            with self.subTest(change=change):
                raw = fixture()
                if change == 'one': raw['lineups'].pop()
                if change == 'third': raw['lineups'].append(deepcopy(raw['lineups'][0]))
                if change == 'duplicate': raw['lineups'][1]['team']['id'] = 1
                if change == 'wrong': raw['lineups'][1]['team']['id'] = 3
                if change == 'bool': raw['teams']['home']['id'] = True
                if change == 'no_fixture_id': raw['fixture'].pop('id')
                self.assertFalse(audit_lineups(raw, **TIMES)['complete_starting_elevens'])

    def test_eleven_unique_identified_players_each_side(self):
        for change in ('ten', 'duplicate', 'missing_id', 'shared'):
            with self.subTest(change=change):
                raw = fixture()
                xi = raw['lineups'][0]['startXI']
                if change == 'ten': xi.pop()
                if change == 'duplicate': xi[-1] = deepcopy(xi[0])
                if change == 'missing_id': xi[-1]['player']['id'] = None
                if change == 'shared': raw['lineups'][1]['startXI'][0] = deepcopy(xi[0])
                self.assertFalse(audit_lineups(raw, **TIMES)['complete_starting_elevens'])

    def test_bench_defects_are_separate_from_starters(self):
        raw = fixture(); lineup = raw['lineups'][0]
        lineup['substitutes'] = [deepcopy(lineup['startXI'][0])] * 2
        result = audit_lineups(raw, **TIMES)
        self.assertTrue(result['complete_starting_elevens'])
        self.assertIn('bench_duplicate_player', result['bench_issues'])
        self.assertIn('bench_overlaps_starting_xi', result['bench_issues'])
        self.assertTrue(result['usable_before_decision'])

    def test_missing_coaches_remain_unknown(self):
        raw = fixture(); raw['lineups'][0].pop('coach'); raw['lineups'][1]['coach'] = dict(id=None)
        result = audit_lineups(raw, **TIMES)
        self.assertEqual(result['coach_count'], 0)
        self.assertIn('coach_unknown', result['unknown'])
        self.assertTrue(result['complete_starting_elevens'])
        self.assertFalse(result['coaches_usable_before_decision'])

    def test_shared_coach_is_not_a_qualified_two_team_coach_observation(self):
        raw = fixture(); raw['lineups'][1]['coach'] = deepcopy(raw['lineups'][0]['coach'])
        result = audit_lineups(raw, **TIMES)
        self.assertIn('coach_shared_between_teams', result['coach_issues'])
        self.assertFalse(result['coaches_usable_before_decision'])
        self.assertTrue(result['complete_starting_elevens'])

    def test_quarantine_and_invalid_declared_league_veto_both_domains(self):
        for extra in [dict(_patternlab_import=dict(quarantined=True)), dict(league=dict(id=False))]:
            raw = dict(fixture(), **extra)
            raw['injuries'] = [dict(fixture=dict(id=99), team=dict(id=1),
                player=dict(id=123, type='Missing Fixture', reason='Knee Injury'))]
            self.assertFalse(audit_lineups(raw, **TIMES)['usable_before_decision'])
            self.assertFalse(audit_injuries(raw, **TIMES)['presence_usable_before_decision'])

    def test_no_default_receipt_and_strict_time_boundaries(self):
        self.assertFalse(audit_lineups(fixture())['usable_before_decision'])
        invalid = [dict(TIMES, observed_at=TIMES['decision_at']),
                   dict(TIMES, observed_at='2026-11-07T17:20:00'),
                   dict(TIMES, decision_at='2026-11-07T18:00:00Z'),
                   dict(TIMES, observed_at='2026-11-07T18:00:00Z')]
        for times in invalid:
            with self.subTest(times=times):
                self.assertFalse(audit_lineups(fixture(), **times)['usable_before_decision'])
        self.assertTrue(audit_lineups(fixture(), observed_at='2026-11-07T18:20:00+01:00',
                                     decision_at=TIMES['decision_at'])['usable_before_decision'])

    def test_naive_kickoff_is_not_silently_assumed_utc(self):
        raw = fixture(); raw['fixture']['date'] = '2026-11-07T18:00:00'
        self.assertFalse(audit_lineups(raw, **TIMES)['usable_before_decision'])

    def test_status_and_played_data_are_separate_from_xi_completeness(self):
        for status in ['FT', 'SUSP', 'PST', None]:
            raw = fixture(); raw['fixture']['status'] = dict(short=status)
            result = audit_lineups(raw, **TIMES)
            self.assertTrue(result['complete_starting_elevens'])
            self.assertFalse(result['usable_before_decision'])
            self.assertTrue(result['prematch_issues'])
        for extra, expected in [
                (dict(events=[dict(type='Goal')]), 'target_events_already_present'),
                (dict(events=[dict(type='Card')]), 'target_events_already_present'),
                (dict(events=[dict(type='subst')]), 'target_events_already_present'),
                (dict(goals=dict(home=0, away=None)), 'target_goals_already_present'),
                (dict(score=dict(halftime=dict(home=0, away=0))), 'target_scores_already_present'),
                (dict(score=dict(fulltime=dict(home=1, away=0))), 'target_scores_already_present')]:
            with self.subTest(extra=extra):
                raw = dict(fixture(), **extra)
                raw['injuries'] = [dict(fixture=dict(id=99), team=dict(id=1),
                    player=dict(id=123, type='Missing Fixture', reason='Knee Injury'))]
                result = audit_lineups(raw, **TIMES)
                self.assertTrue(result['complete_starting_elevens'])
                self.assertIn(expected, result['prematch_issues'])
                self.assertFalse(result['usable_before_decision'])
                self.assertFalse(audit_injuries(raw, **TIMES)['usable_before_decision'])
        raw = dict(fixture(), events=[], goals=dict(home=None, away=None),
                   score=dict(halftime=dict(home=None, away=None), fulltime=dict(home=None, away=None)))
        self.assertTrue(audit_lineups(raw, **TIMES)['usable_before_decision'])

    def test_injuries_absent_null_empty_never_certify_no_absence(self):
        for raw, state in [(fixture(), 'absent'), (dict(fixture(), injuries=None), 'null'),
                           (dict(fixture(), injuries=[]), 'empty'), ([], 'empty'),
                           ({'response': []}, 'empty')]:
            with self.subTest(state=state):
                result = audit_injuries(raw, **TIMES)
                self.assertEqual(result['availability'], state)
                self.assertFalse(result['no_injuries_proven'])
                self.assertFalse(result['usable_before_decision'])

    def test_present_injury_rows_require_team_player_fixture_and_timing(self):
        raw = fixture(); raw['injuries'] = [dict(fixture=dict(id=99), team=dict(id=1),
            player=dict(id=123, type='Missing Fixture', reason='Knee Injury'))]
        before = deepcopy(raw)
        result = audit_injuries(raw, **TIMES)
        self.assertTrue(result['usable_before_decision'])
        self.assertFalse(result['provider_completeness_proven'])
        self.assertEqual(raw, before)
        for key, value in [('fixture', dict(id=98)), ('team', dict(id=3)), ('player', dict(id=None))]:
            changed = deepcopy(raw); changed['injuries'][0][key] = value
            self.assertFalse(audit_injuries(changed, **TIMES)['usable_before_decision'])
        self.assertFalse(audit_injuries(raw['injuries'], **TIMES)['usable_before_decision'])

    def test_undescribed_injury_is_only_a_present_player_row(self):
        raw = fixture(); raw['injuries'] = [dict(fixture=dict(id=99), team=dict(id=1), player=dict(id=123))]
        result = audit_injuries(raw, **TIMES)
        self.assertTrue(result['presence_usable_before_decision'])
        self.assertFalse(result['usable_before_decision'])
        self.assertIn('injury_type_unknown', result['unknown'])
        self.assertIn('injury_reason_unknown', result['unknown'])

    def test_malformed_rows_do_not_crash_or_become_empty(self):
        for value in [42, {}, [None], [dict(player='bad')]]:
            with self.subTest(value=value):
                raw = dict(fixture(), injuries=value)
                self.assertFalse(audit_injuries(raw, **TIMES)['usable_before_decision'])
        raw = fixture(); raw['lineups'] = [None, 17]
        self.assertFalse(audit_lineups(raw, **TIMES)['complete_starting_elevens'])


if __name__ == '__main__':
    unittest.main()
