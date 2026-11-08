"""Synthetic contract checks; no fixture database or provider call is needed."""
from copy import deepcopy
import json
import unittest

from context_event_quality import audit_events


def event(minute, *, team=10, kind='Goal', detail='Normal Goal', extra=None, player=100, assist=None):
    return dict(time=dict(elapsed=minute, extra=extra), team=dict(id=team),
                player=dict(id=player), assist=dict(id=assist), type=kind, detail=detail, comments=None)


def fixture(events=(), *, ht=(0, 0), ft=(0, 0), status='FT'):
    return dict(fixture=dict(id=123, status=dict(short=status)), teams=dict(home=dict(id=10), away=dict(id=20)),
                score=dict(halftime=dict(zip(('home', 'away'), ht)), fulltime=dict(zip(('home', 'away'), ft))),
                events=list(events))


class EventQualityTests(unittest.TestCase):
    def test_availability_distinguishes_missing_null_empty_present_and_malformed(self):
        raw = fixture();del raw['events']
        missing = audit_events(raw)
        self.assertEqual(missing['availability'], 'absent')
        self.assertIsNone(missing['event_count'])
        self.assertIsNone(missing['counted_goal_totals_90'])
        self.assertFalse(missing['goal_compatible'])
        for value, expected in ((None, 'null'), ({}, 'malformed'), ('', 'malformed'), ([], 'empty')):
            raw['events'] = value
            result = audit_events(raw)
            self.assertEqual(result['availability'], expected)
            self.assertFalse(result['event_process_eligible'])
        result = audit_events(fixture([event(10)], ht=(1, 0), ft=(1, 0)))
        self.assertEqual(result['availability'], 'present')
        self.assertTrue(result['goal_compatible'])
        self.assertTrue(result['event_process_eligible'])

    def test_empty_zero_score_compatible_does_not_certify_other_events(self):
        result = audit_events(fixture())
        self.assertTrue(result['goal_compatible'])
        self.assertFalse(result['event_process_eligible'])
        self.assertFalse(result['collection_completeness_proven'])
        self.assertFalse(result['historical_availability_proven'])
        self.assertIn('EMPTY_EVENTS_NOT_COMPLETENESS_PROOF', result['issue_codes'])
        self.assertFalse(audit_events(fixture(ft=(1, 0)))['goal_compatible'])

    def test_time_phase_45_plus_precedes_46_and_90_plus_stays_h2(self):
        rows = [event(45, extra=8), event(46, team=20), event(90, extra=10)]
        result = audit_events(fixture(rows, ht=(1, 0), ft=(2, 1)))
        self.assertTrue(result['event_process_eligible'])
        self.assertEqual(result['counted_goal_totals_h1'], dict(home=1, away=0))
        self.assertEqual(result['chronology_order'], [0, 1, 2])
        self.assertEqual([e['phase'] for e in result['annotations']], ['H1', 'H2', 'H2'])

    def test_provider_order_is_audited_never_silently_sorted_or_deduplicated(self):
        rows = [event(46), event(45, extra=8)]
        result = audit_events(fixture(rows, ht=(1, 0), ft=(2, 0)))
        self.assertTrue(result['goal_compatible'])
        self.assertFalse(result['event_process_eligible'])
        self.assertIn('EVENTS_OUT_OF_ORDER', result['issue_codes'])
        self.assertEqual(result['chronology_order'], [1, 0])
        result = audit_events(fixture([event(10), event(10)], ht=(2, 0), ft=(2, 0)))
        self.assertEqual(result['duplicate_count'], 1)
        self.assertEqual(result['event_count'], 2)
        self.assertFalse(result['goal_compatible'])
        self.assertEqual(result['counted_goal_totals_90'], dict(home=2, away=0))

    def test_own_goal_keeps_credited_team_and_missed_penalty_is_not_goal(self):
        rows = [event(17, detail='Own Goal'), event(27, detail='Missed Penalty', team=20)]
        result = audit_events(fixture(rows, ht=(1, 0), ft=(1, 0)))
        self.assertTrue(result['goal_compatible'])
        self.assertEqual(result['own_goal_count'], 1)
        self.assertEqual(result['counted_goal_totals_90'], dict(home=1, away=0))
        self.assertFalse(result['annotations'][1]['counted_goal'])
        self.assertIn('OWN_GOAL_TEAM_AS_CREDITED', result['issue_codes'])
        # Opposite score is an inconsistency, never an instruction to invert.
        self.assertFalse(audit_events(fixture(rows, ht=(0, 1), ft=(0, 1)))['goal_compatible'])

    def test_unknown_goal_details_and_var_ambiguity_block_process(self):
        raw = fixture([event(12, detail='Unrecognized')])
        result = audit_events(raw)
        self.assertFalse(result['goal_compatible'])
        self.assertIn('UNKNOWN_GOAL_DETAIL', result['issue_codes'])
        for detail in ('Goal cancelled', 'Goal Disallowed - offside', 'Card upgrade', None):
            result = audit_events(fixture([event(12, kind='Var', detail=detail)]))
            self.assertTrue(result['goal_compatible'])
            self.assertFalse(result['event_process_eligible'])
            self.assertIn('VAR_DECISION_AMBIGUOUS', result['issue_codes'])
        rows = [event(12), event(13, kind='Var', detail='Goal confirmed')]
        result = audit_events(fixture(rows, ht=(1, 0), ft=(1, 0)))
        self.assertTrue(result['goal_compatible'])
        self.assertEqual(result['counted_goal_totals_90'], dict(home=1, away=0))

    def test_team_mismatch_and_unknown_player_are_distinct(self):
        result = audit_events(fixture([event(10, team=99)], ht=(1, 0), ft=(1, 0)))
        self.assertIn('EVENT_TEAM_NOT_IN_FIXTURE', result['issue_codes'])
        self.assertFalse(result['goal_compatible'])
        result = audit_events(fixture([event(10, player=None)], ht=(1, 0), ft=(1, 0)))
        self.assertIn('PLAYER_ID_UNKNOWN', result['issue_codes'])
        self.assertNotIn('EVENT_TEAM_NOT_IN_FIXTURE', result['issue_codes'])
        self.assertEqual(result['missing_player_id_count'], 1)
        self.assertTrue(result['goal_compatible'])
        self.assertFalse(result['event_process_eligible'])

    def test_nonregulation_status_and_shootouts_are_never_ft_events(self):
        for status in ('AET', 'PEN', 'NS', 'ABD'):
            result = audit_events(fixture([event(30)], ht=(1, 0), ft=(1, 0), status=status))
            self.assertFalse(result['goal_compatible'])
            self.assertFalse(result['event_process_eligible'])
            self.assertIn('STATUS_NOT_NORMAL_FT', result['issue_codes'])
        e = event(90, detail='Penalty');e['comments'] = 'Penalty Shootout'
        result = audit_events(fixture([e]))
        self.assertIn('SHOOTOUT_EVENT', result['issue_codes'])
        self.assertFalse(result['annotations'][0]['counted_goal'])
        raw = fixture();raw['score']['extratime'] = dict(home=0, away=0)
        self.assertFalse(audit_events(raw)['goal_compatible'])

    def test_bad_times_are_not_coerced_and_bare_91_is_not_added_time(self):
        for minute, extra in ((None, None), ('45', None), (True, None), (-1, None), (91, None), (120, None), (45, -1), (45, True), (46, 2)):
            result = audit_events(fixture([event(minute, extra=extra)], ht=(1, 0), ft=(1, 0)))
            self.assertFalse(result['goal_compatible'])
            self.assertFalse(result['event_process_eligible'])
            self.assertIsNone(result['annotations'][0]['phase'])

    def test_score_and_import_quality_veto(self):
        raw = fixture([event(10)], ht=(1, 0), ft=(1, 0))
        raw['_patternlab_import'] = dict(quarantined=True)
        result = audit_events(raw)
        self.assertFalse(result['goal_compatible'])
        self.assertIn('IMPORT_QUARANTINED', result['issue_codes'])
        raw = fixture();raw['score']['halftime']['home'] = None
        result = audit_events(raw)
        self.assertFalse(result['goal_compatible'])
        self.assertIsNone(result['score_halftime'])
        self.assertFalse(audit_events(fixture(ht=(1, 0), ft=(0, 0)))['goal_compatible'])
        raw = fixture();raw['teams']['away']['id'] = 10
        self.assertFalse(audit_events(raw)['goal_compatible'])
        raw = fixture();raw['provider'] = 'football-data'
        self.assertIn('PROVIDER_NOT_API_FOOTBALL', audit_events(raw)['issue_codes'])
        raw = fixture();raw['_patternlab_import'] = dict(quarantined='true')
        self.assertFalse(audit_events(raw)['goal_compatible'])
        for value in ('2-1', [2, 1], {}):
            raw = fixture();raw['score']['extratime'] = value
            self.assertIn('NON_REGULATION_SCORE_MALFORMED', audit_events(raw)['issue_codes'])
        for value in (None, {}, {'id': True}, {'id': 'not-id'}):
            raw = fixture();raw['league'] = value
            self.assertIn('LEAGUE_ID_INVALID', audit_events(raw)['issue_codes'])
        for value in (None, {}, {'home': None, 'away': 0}, {'home': True, 'away': 0}):
            raw = fixture();raw['goals'] = value
            self.assertIn('FINAL_GOALS_FIELD_INVALID', audit_events(raw)['issue_codes'])
        raw = fixture();raw['goals'] = dict(home=1, away=0)
        self.assertIn('FINAL_GOALS_FIELD_MISMATCH', audit_events(raw)['issue_codes'])
        raw['goals'] = dict(home=0, away=0);raw['league'] = dict(id=39)
        self.assertTrue(audit_events(raw)['goal_compatible'])

    def test_substitutions_cards_and_unknown_types_keep_semantic_limits(self):
        rows = [event(20, kind='Card', detail='Yellow Card'),
                event(46, kind='subst', detail='Substitution 1', assist=101)]
        result = audit_events(fixture(rows))
        self.assertTrue(result['event_process_eligible'])
        self.assertEqual(result['type_counts'], dict(Card=1, subst=1))
        self.assertIn('SUBSTITUTION_DIRECTION_UNVERIFIED', result['issue_codes'])
        self.assertIn('CARD_RECIPIENT_MEMBERSHIP_UNVERIFIED', result['issue_codes'])
        rows[-1]['assist']['id'] = None
        self.assertFalse(audit_events(fixture(rows))['event_process_eligible'])
        result = audit_events(fixture([event(1, kind='Unknown')]))
        self.assertIn('UNKNOWN_EVENT_TYPE', result['issue_codes'])
        self.assertFalse(result['event_process_eligible'])

    def test_malformed_input_json_output_and_input_immutability(self):
        for raw in (None, [], 'fixture', 3):
            result = audit_events(raw)
            self.assertFalse(result['goal_compatible'])
            json.dumps(result, allow_nan=False)
        raw = fixture([event(1), None, {'time': {'elapsed': float('nan')}}])
        before = deepcopy(raw)
        result = audit_events(raw)
        self.assertIn('EVENT_NOT_OBJECT', result['issue_codes'])
        self.assertIn('EVENT_NOT_JSON', result['issue_codes'])
        self.assertEqual(raw['events'][:2], before['events'][:2])
        json.dumps(result, allow_nan=False)
        for kind, detail in (('Goal', []), ('Card', {}), ('Var', []), ([], 'Normal Goal')):
            result = audit_events(fixture([event(1, kind=kind, detail=detail)]))
            self.assertFalse(result['event_process_eligible'])
            json.dumps(result, allow_nan=False)


if __name__ == '__main__':
    unittest.main()
