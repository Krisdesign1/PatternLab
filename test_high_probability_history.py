"""Saved choices retain their original marginals and current result revisions."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import high_probability_history as history
import probability_scenario_store as intents
from test_probability_scenario_store import forecast, P, NOW


LATER = datetime(2026, 10, 25, 10, tzinfo=timezone.utc)


def saved(status='pending'):
    entry = forecast(mixed=True)
    entry.update(group_kind='mixed', league='mixed', observed=[], evaluations={},
                 actual=None, issues=[], result_history=[])
    entry['metadata'].update(daily_policy=intents.POLICY, selection=dict(policy=intents.POLICY,
        requested_scenario=dict(sequence=['1st', '2nd'], probability=.3),
        search=dict(outcome='mixed', minimum=0), match_probabilities=[
            dict(match_id=fixture['match_id'], probabilities=deepcopy(p), search_outcome=choice)
            for fixture, p, choice in zip(entry['fixtures'], P, ['1st', '2nd'])]))
    if status == 'settled':
        entry.update(actual=['1st', '2nd'], checked_at='2026-10-22T18:00:00Z', observed=[
            dict(match_id='100', ht='1-0', ft='1-0', value='1st'),
            dict(match_id='101', ht='0-0', ft='1-0', value='2nd')])
    entry['status'] = status
    return entry


class HighProbabilityHistoryTests(unittest.TestCase):
    def test_saved_values_and_each_match_verdict_are_detached_from_inputs(self):
        entry = saved('settled')
        before = deepcopy(entry)
        with patch('high_probability_engine.scan', side_effect=AssertionError('No current scan')), \
                patch('api_football.ApiFootballClient.get', side_effect=AssertionError('No API')):
            report = history.build([entry], now=LATER)
        self.assertEqual((report['forecasts'], report['choices'], report['fixtures']), (1, 1, 2))
        row = report['rows'][0]
        self.assertEqual([m['probabilities'] for m in row['match_rows']], P)
        self.assertEqual([m['searched_probability'] for m in row['match_rows']], [.6, .5])
        self.assertEqual([m['verdict'] for m in row['match_rows']], ['hit', 'hit'])
        self.assertEqual([m['order'] for m in row['match_rows']], [1, 2])
        self.assertEqual(row['searched_scenario']['state'], 'hit')
        row['entry']['metadata']['extra'] = True
        row['match_rows'][0]['probabilities']['1st'] = .99
        self.assertEqual(entry, before)

    def test_multiple_choices_share_one_forecast_without_overwriting_it(self):
        entry = saved()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'intents.sqlite3'
            for index, sequence in enumerate((['1st', '2nd'], ['Égal', '1st'])):
                selection = dict(requested_scenario=dict(sequence=sequence), search={'minimum': 0})
                intents.record(entry, selection, path=path, clock=lambda: NOW+timedelta(minutes=1+index))
            choices = intents.list_intents(path)
        original = deepcopy([entry, choices])
        report = history.build([entry], choices, now=LATER)
        self.assertEqual((report['forecasts'], report['choices'], report['fixtures']), (1, 2, 2))
        self.assertTrue(all(row['id'].startswith('intent:') for row in report['rows']))
        self.assertEqual([m['searched_outcome'] for m in report['rows'][0]['match_rows']], ['Égal', '1st'])
        self.assertEqual([m['searched_probability'] for m in report['rows'][0]['match_rows']], [.1, .2])
        self.assertEqual([entry, choices], original)

    def test_partial_group_has_only_observed_match_evaluated(self):
        entry = saved('settled')
        entry.update(status='pending', actual=None, observed=entry['observed'][:1])
        row = history.build([entry], now=LATER)['rows'][0]
        self.assertEqual(row['status'], 'pending')
        self.assertEqual(row['searched_scenario']['state'], 'pending')
        self.assertEqual([m['verdict'] for m in row['match_rows']], ['hit', 'pending'])
        self.assertEqual([m['actual'] for m in row['match_rows']], ['1st', None])

    def test_void_unresolved_and_invalid_scores_never_become_losses(self):
        for status in ('void', 'unresolved'):
            with self.subTest(status=status):
                row = history.build([saved(status)], now=LATER)['rows'][0]
                self.assertEqual([m['verdict'] for m in row['match_rows']], [status]*2)
        entry = saved('settled')
        entry['observed'][0]['value'] = '2nd'
        row = history.build([entry], now=LATER)['rows'][0]
        self.assertEqual(row['match_rows'][0]['verdict'], 'unresolved')
        self.assertIsNone(row['match_rows'][0]['actual'])
        self.assertIsNone(row['match_rows'][0]['ht'])

    def test_latest_correction_updates_result_without_changing_forecast(self):
        entry = saved('settled')
        first = history.build([entry], now=LATER)['rows'][0]
        entry['observed'][0].update(ht='0-0', ft='1-0', value='2nd')
        entry['actual'][0] = '2nd'
        entry['checked_at'] = '2026-10-24T18:00:00Z'
        corrected = history.build([entry], now=LATER)['rows'][0]
        self.assertEqual(first['match_rows'][0]['verdict'], 'hit')
        self.assertEqual(corrected['match_rows'][0]['verdict'], 'miss')
        self.assertEqual(first['match_rows'][0]['probabilities'], corrected['match_rows'][0]['probabilities'])
        self.assertEqual(corrected['searched_scenario']['state'], 'miss')

    def test_missing_display_uses_frozen_distribution_and_missing_distribution_stays_unknown(self):
        entry = saved()
        entry['metadata']['selection']['match_probabilities'] = []
        row = history.build([entry], now=LATER)['rows'][0]
        self.assertEqual([m['probabilities'] for m in row['match_rows']], P)
        self.assertEqual([m['searched_outcome'] for m in row['match_rows']], ['1st', '2nd'])
        entry['methods']['clubs'].pop('distribution')
        row = history.build([entry], now=LATER)['rows'][0]
        self.assertTrue(all(m['probabilities'] is None for m in row['match_rows']))
        self.assertTrue(all(m['searched_probability'] is None for m in row['match_rows']))

    def test_conflicting_display_is_not_presented_as_a_frozen_percentage(self):
        entry = saved()
        entry['metadata']['selection']['match_probabilities'][0]['probabilities'] = P[1]
        row = history.build([entry], now=LATER)['rows'][0]
        self.assertIsNone(row['match_rows'][0]['probabilities'])
        self.assertIsNone(row['match_rows'][0]['searched_probability'])

    def test_other_origins_and_not_yet_saved_predictions_are_not_invented(self):
        other = saved()
        other['metadata']['daily_policy'] = 'daily-v1'
        other['metadata']['selection']['policy'] = 'daily-v1'
        self.assertEqual(history.build([other], now=LATER)['rows'], [])
        self.assertEqual(history.build([], now=LATER)['rows'], [])
        report = history.build([saved()], now=NOW-timedelta(minutes=1))
        self.assertEqual(report['rows'], [])
        self.assertEqual(report['excluded'], 1)

    def test_kinshasa_day_and_pre_match_time_guards_apply_to_history(self):
        entry = saved()
        # A 23:30 UTC fixture belongs to the following Kinshasa day.
        for index, fixture in enumerate(entry['fixtures']):
            fixture['timestamp'] = f'2026-10-22T23:{30+index}:00Z'
        entry['methods']['clubs']['fixtures'] = deepcopy(entry['fixtures'])
        before_local_day = datetime(2026, 10, 22, 22, 59, tzinfo=timezone.utc)
        early = history.build([entry], now=before_local_day)['rows'][0]
        self.assertEqual(early['day'].isoformat(), '2026-10-23')
        self.assertTrue(all(match['verdict'] == 'pending' for match in early['match_rows']))
        row = history.build([entry], now=before_local_day+timedelta(minutes=2))['rows'][0]
        self.assertEqual(row['day'].isoformat(), '2026-10-23')
        entry['created_at'] = '2026-10-23T01:00:00Z'
        self.assertEqual(history.build([entry], now=LATER)['rows'], [])
        with self.assertRaises(ValueError):
            history.build([], now=LATER.replace(tzinfo=None))

    def test_future_groups_retain_frozen_probabilities_without_accepting_future_results(self):
        entry = saved()
        for fixture in entry['fixtures']:
            fixture['timestamp'] = fixture['timestamp'].replace('2026-10-22', '2026-10-26')
        entry['methods']['clubs']['fixtures'] = deepcopy(entry['fixtures'])
        before = deepcopy(entry)
        report = history.build([entry], now=LATER)
        self.assertEqual((report['choices'], report['excluded']), (1, 0))
        self.assertEqual([match['probabilities'] for match in report['rows'][0]['match_rows']], P)
        self.assertEqual(report['rows'][0]['status'], 'pending')
        self.assertEqual(entry, before)
        entry['observed'] = [dict(match_id='100',ht='1-0',ft='1-0',value='1st')]
        report = history.build([entry], now=LATER)
        self.assertEqual((report['rows'],report['excluded']),([],1))


if __name__ == '__main__':
    unittest.main()
