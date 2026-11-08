"""Cold-start and temporal invariants for prior-centred count features."""
from copy import deepcopy
import unittest

import numpy as np

from halftime_count_priors import contrast,prepare_arrays
from halftime_count_experiment import PROFILES,CORE,RICH
from halftime_research_features import RICH_COLUMNS as FULL_RICH


class PriorFeatureTests(unittest.TestCase):
    def fixture(self):
        rows={'1':dict(match_id='1',league='39',timestamp='2024-01-01T12:00:00',home_id='10',away_id='20',ht='2-0',ft='2-2'),
              '2':dict(match_id='2',league='39',timestamp='2024-01-02T12:00:00',home_id='10',away_id='20',ht='0-0',ft='1-0')}
        profile=dict(match_id='2',core={key:0. for key in CORE},rich={key:0. for key in FULL_RICH},history_ids={prefix:[] for prefix in PROFILES})
        return {'2':profile},rows,{}

    def test_missing_history_is_exactly_neutral(self):
        profiles,rows,statistics=self.fixture()
        scores,rich=prepare_arrays(profiles,rows,statistics,['2'])
        np.testing.assert_array_equal(scores,np.zeros((1,16)))
        np.testing.assert_array_equal(rich,np.zeros((1,48)))

    def test_no_target_or_future_goals_or_statistics_enter_prior(self):
        profiles,rows,statistics=self.fixture()
        profiles['2']['core']['home_home_last10_count']=2
        profiles['2']['core']['home_home_ht_for_mean']=4
        before=prepare_arrays(profiles,rows,statistics,['2'])
        self.assertAlmostEqual(before[0][0,4],np.log(1.2))
        rows['2']['ht']='30-0';rows['2']['ft']='30-30'
        rows['3']=dict(rows['2'],match_id='3',timestamp='2026-01-01T00:00:00')
        statistics['2']={'periods':{'first':{'expected_goals':[100,0]}}}
        after=prepare_arrays(profiles,rows,statistics,['2'])
        for a,b in zip(before,after):np.testing.assert_array_equal(a,b)

    def test_late_previous_day_match_does_not_enter_prior(self):
        profiles,rows,statistics=self.fixture()
        profiles['2']['core']['home_home_last10_count']=2
        profiles['2']['core']['home_home_ht_for_mean']=4
        rows['1']['timestamp']='2024-01-01T23:00:00'
        scores,_=prepare_arrays(profiles,rows,statistics,['2'])
        self.assertEqual(scores[0,4],0.)

    def test_shrinkage_reduces_extremes_and_zero_support_does_not_mean_zero_strength(self):
        self.assertEqual(contrast(0,0,1),0.)
        self.assertEqual(contrast(3,4,0),0.)
        self.assertGreater(contrast(0,1,1),np.log(.01))
        self.assertLess(contrast(3,1,1),contrast(3,10,1))
        with self.assertRaises(ValueError):contrast(1,-1,1)


if __name__=='__main__':unittest.main()
