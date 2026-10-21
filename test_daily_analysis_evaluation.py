import copy
from contextlib import ExitStack
from datetime import date
import math
import unittest
from unittest.mock import patch

import api_football as api
import daily_analysis_engine as engine
import daily_analysis_evaluation as replay
import match_predictor as clubs
import prospective_models as models
from test_daily_analysis_engine import history, future, NOW


def past_slate(count=8):
    rows=future(count,(39,61))
    for i,row in enumerate(rows):
        row['fixture']['status']['short']='FT'
        row['score']['halftime']={'home':i%2,'away':0}
        row['score']['fulltime']={'home':1+i%2,'away':1}
    return rows


class DailyReplayTests(unittest.TestCase):
    def test_incremental_probabilities_match_the_production_clubs_estimator(self):
        raw=history(80,39)+history(80,61)
        rows=[api.normalize_fixture(row) for row in raw]
        index=replay.HistoryIndex(rows);index.advance('2026-10-21')
        for target in future(4,(39,61)):
            fixture=replay.workspace.fixture_from_raw(target)
            actual=index.estimate(fixture)
            expected=clubs.estimate(rows,fixture['league'],fixture['home'],fixture['away'],'2026-10-21')
            self.assertEqual(actual['baseline'],expected['baseline'])
            for value in clubs.VALUES:self.assertAlmostEqual(actual['probabilities'][value],expected['probabilities'][value],places=14)
            self.assertEqual(actual['home_profile'],expected['home_profile'])
            self.assertEqual(actual['away_profile'],expected['away_profile'])
        with self.assertRaises(ValueError):index.advance('2026-10-20')

    def test_constructed_group_sets_match_frozen_production_policy_all_sizes(self):
        raw=history(80,39)+history(40,61)+future(23,(39,61))
        plan=engine.plan(raw,now=NOW)
        rows=[api.normalize_fixture(row) for row in raw if api._status_short(row)=='FT']
        index=replay.HistoryIndex(rows);index.advance(plan['before'])
        fixtures=sorted([replay.workspace.fixture_from_raw(row) for row in raw if api._status_short(row)=='NS'],key=replay._order)
        supports={row['match_id']:engine._support(row,index.leagues) for row in fixtures}
        actual=replay.construct(fixtures,supports)
        key=lambda group:tuple(row['match_id'] for row in group)
        self.assertEqual({key(group) for group in actual},{key(group['fixtures']) for group in plan['candidates']})
        self.assertEqual({len(group) for group in actual},set(range(2,21)))

    def test_same_member_sets_preserve_nll_and_neither_outcomes_nor_input_order_plan_groups(self):
        raw=history(120,39)+history(60,61)+past_slate(9)
        original=copy.deepcopy(raw)
        result=replay.evaluate(raw,today=date(2026,10,22),start='2026-10-21')
        self.assertEqual(raw,original)
        self.assertTrue(result['paired_records'])
        for record in result['paired_records']:
            self.assertTrue(record['member_sets_equal'])
            self.assertAlmostEqual(record['support_loss_sum'],record['reference_loss_sum'],places=12)
        reverse=replay.evaluate(reversed(raw),today=date(2026,10,22),start='2026-10-21')
        self.assertEqual(result,reverse)
        changed=copy.deepcopy(raw)
        for row in changed[-9:]:
            row['score']['halftime']={'home':2,'away':2};row['score']['fulltime']={'home':2,'away':2}
        other=replay.evaluate(changed,today=date(2026,10,22),start='2026-10-21')
        self.assertEqual(result['inventory'],other['inventory'])
        self.assertEqual([row['groups'] for row in result['production_by_size']],
                         [row['groups'] for row in other['production_by_size']])

    def test_missing_outcome_excludes_entire_common_day_size_without_recomposing(self):
        raw=history(120,39)+history(120,61)+past_slate(4)
        full=replay.evaluate(raw,today=date(2026,10,22),start='2026-10-21')
        raw[-4]['score']['halftime']={'home':None,'away':None}
        incomplete=replay.evaluate(raw,today=date(2026,10,22),start='2026-10-21')
        self.assertFalse(incomplete['paired_records'])
        self.assertGreater(incomplete['controlled_attrition']['day_sizes_missing_outcomes'],0)
        self.assertEqual([row['groups'] for row in full['production_by_size']],
                         [row['groups'] for row in incomplete['production_by_size']])
        self.assertTrue(any(row['missing_outcomes'] for row in incomplete['production_by_size']))

    def test_method_availability_obeys_multimatch_history_threshold(self):
        raw=history(30,39)+past_slate(4)
        # Replace all target identities with the one league that has history.
        for row in raw[-4:]:
            row['league']['id']=39;row['teams']=copy.deepcopy(raw[0]['teams'])
        report=replay.evaluate(raw,today=date(2026,10,22),start='2026-10-21')
        self.assertFalse(report['paired_records'])
        self.assertTrue(report['production_by_size'])
        self.assertTrue(all(row['evaluated']==0 and row['unavailable']==row['groups'] for row in report['production_by_size']))

    def test_replay_clubs_matches_mixed_snapshot_probabilities_without_artifact_reads(self):
        raw=history(80,39)+history(80,61)
        rows=[api.normalize_fixture(row) for row in raw]
        fixtures=sorted([replay.workspace.fixture_from_raw(row) for row in future(4,(39,61))],key=replay._order)
        index=replay.HistoryIndex(rows);index.advance('2026-10-21')
        with (patch.object(models,'_catboost',side_effect=ValueError('No artifact')),
              patch.object(models,'_context_neighbors',side_effect=ValueError('No artifact'))):
            payload=models.build_mixed_methods(rows,fixtures)['methods']['clubs']
        expected=math.prod(index.estimate(row)['probabilities']['2nd'] for row in fixtures)
        self.assertAlmostEqual(models.probability_for_sequence(payload,['2nd']*4),expected,places=14)

    def test_causal_cutoff_excludes_whole_target_utc_day_and_current_or_future_calendar(self):
        raw=history(120,39)+history(120,61)+past_slate(4)
        index=replay.HistoryIndex([api.normalize_fixture(row) for row in raw])
        index.advance('2026-10-21')
        self.assertEqual(sum(row['matches'] for row in index.leagues.values()),240)
        same_day=replay.evaluate(raw,today=date(2026,10,21),start='2026-10-20')
        self.assertEqual(same_day['inventory']['calendar_matches'],0)
        self.assertFalse(same_day['paired_records'])
        for start,end in [('2026-10-21','2026-10-21'),('2026-10-20','2026-10-23')]:
            with self.assertRaises(ValueError):replay.evaluate(raw,today=date(2026,10,22),start=start,end=end)

    def test_no_api_io_journal_or_trained_model_is_used(self):
        raw=history(120,39)+history(120,61)+past_slate(4)
        with ExitStack() as stack:
            for target,name in [(api,'load_key'),(api,'load_fixtures'),(api,'load_matches'),
                                (api.ApiFootballClient,'get'),(models,'build_mixed_methods')]:
                stack.enter_context(patch.object(target,name,side_effect=AssertionError(name)))
            stack.enter_context(patch('builtins.open',side_effect=AssertionError('No files')))
            report=replay.evaluate(raw,today=date(2026,10,22),start='2026-10-21')
        self.assertTrue(report['paired_records'])


if __name__=='__main__':unittest.main()
