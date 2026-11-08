"""Ranking history uses per-match snapshots, never a reconstructed old model."""
from copy import deepcopy
import unittest
from unittest.mock import patch

import high_probability_archive_view as view
from test_high_probability_history import saved, LATER
from test_probability_scenario_store import P


def archived(index=0, *, status='pending', created='2026-10-22T10:00:00Z', identifier=None):
    entry = saved()
    fixture = deepcopy(entry['fixtures'][index])
    return dict(id=identifier or f'archive-{index}', source='API-Football', day='2026-10-22',
        fixture=fixture, probabilities=deepcopy(P[index]), created_at=created,
        before='2026-10-22', model_version='clubs-test', status=status,
        actual='1st' if status == 'settled' else None,
        ht='1-0' if status == 'settled' else None, ft='1-0' if status == 'settled' else None,
        issues=[], revision=1, checked_at='2026-10-22T18:00:00Z', result_history=[])


class RankingArchiveViewTests(unittest.TestCase):
    def test_individual_archive_exists_without_any_group(self):
        inputs=[archived(0,status='settled'),archived(1)]
        before=deepcopy(inputs)
        with patch('high_probability_engine.scan',side_effect=AssertionError('No recalculation')):
            result=view.build(inputs,now=LATER)
        self.assertEqual(len(result['rows']),2)
        self.assertEqual(result['archived_matches'],2)
        self.assertEqual(result['legacy_matches'],0)
        self.assertEqual({row['match_id']:row['probabilities'] for row in result['rows']},
                         {'100':P[0],'101':P[1]})
        result['rows'][0]['probabilities']['1st']=.9
        self.assertEqual(inputs,before)

    def test_latest_snapshot_is_chosen_before_probability_filtering(self):
        first=archived()
        later=archived(created='2026-10-22T11:00:00Z',identifier='new')
        later['probabilities']=deepcopy(P[1])
        result=view.build([first,later],now=LATER)
        self.assertEqual(len(result['rows']),1)
        self.assertEqual(result['rows'][0]['id'],'new')
        self.assertEqual(result['rows'][0]['probabilities']['1st'],.2)
        self.assertEqual(len(result['rows'][0]['snapshots']),2)
        self.assertEqual([row for row in result['rows'] if row['probabilities']['1st']>=.5],[])

    def test_previous_group_percentages_have_explicit_legacy_provenance(self):
        result=view.build([], [saved('settled')], now=LATER)
        self.assertEqual(result['legacy_matches'],2)
        self.assertTrue(all(row['source_kind']=='saved_group' for row in result['rows']))
        self.assertEqual({row['match_id']:row['probabilities'] for row in result['rows']},
                         {'100':P[0],'101':P[1]})
        self.assertTrue(all(row['status']=='settled' for row in result['rows']))

    def test_archive_and_old_group_for_same_match_do_not_duplicate_history(self):
        result=view.build([archived(0,created='2026-10-22T11:00:00Z')], [saved()], now=LATER)
        self.assertEqual(len(result['rows']),2)
        first=next(row for row in result['rows'] if row['match_id']=='100')
        self.assertEqual(first['source_kind'],'ranking_archive')
        self.assertEqual(len(first['snapshots']),2)

    def test_archive_result_is_not_hidden_by_a_later_unsettled_group(self):
        entry=saved()
        entry['created_at']='2026-10-22T11:30:00Z'
        result=view.build([archived(0,status='settled')],[entry],now=LATER)
        first=next(row for row in result['rows'] if row['match_id']=='100')
        self.assertEqual(first['source_kind'],'ranking_archive')
        self.assertEqual(first['status'],'settled')

    def test_late_forecast_and_unobserved_past_are_never_fabricated(self):
        value=archived(created='2026-10-22T16:00:00Z')
        result=view.build([value],now=LATER)
        self.assertEqual(result['rows'],[])
        self.assertEqual(result['excluded'],1)
        self.assertEqual(view.build([],[],now=LATER)['rows'],[])

    def test_invalid_legacy_forecast_cannot_hide_valid_ranking_archive(self):
        with patch.object(view,'_legacy',side_effect=ValueError('Ancien format invalide')):
            report=view.build([archived()],now=LATER)
        self.assertEqual(report['archived_matches'],1)
        self.assertTrue(report['legacy_unavailable'])

    def test_future_local_day_is_visible_only_with_an_already_saved_pre_match_capture(self):
        value=archived()
        value['fixture']['timestamp']='2026-10-25T23:30:00Z'
        report=view.build([value],now=LATER)
        self.assertEqual(report['rows'][0]['day'],'2026-10-26')
        self.assertEqual(report['rows'][0]['status'],'pending')
        self.assertEqual(report['rows'][0]['probabilities'],P[0])
        for changes in ({'created_at':'2026-10-25T11:00:00Z'},
                        {'status':'settled','actual':'1st','ht':'1-0','ft':'1-0'}):
            invalid=dict(deepcopy(value),**changes)
            report=view.build([invalid],now=LATER)
            self.assertEqual((report['rows'],report['excluded']),([],1))


if __name__=='__main__':
    unittest.main()
