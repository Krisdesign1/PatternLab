"""Feature isolation for the new count-target research experiment."""
from copy import deepcopy
import unittest

import numpy as np

from halftime_count_experiment import CORE,RICH,PROFILES,clean_rich,features


class CountFeatureTests(unittest.TestCase):
    def fixture(self):
        rows={'target':dict(home_id='10',away_id='20'),
              'previous':dict(home_id='30',away_id='10'),
              '1208600':dict(home_id='10',away_id='30')}
        periods={p:{'expected_goals':[1.,2.]} for p in ('first','second')}
        statistics={'previous':dict(periods=deepcopy(periods)),'1208600':dict(periods=deepcopy(periods))}
        profile=dict(match_id='target',_home_id='10',_away_id='20',
            core={key:1. for key in CORE},rich={key:1. for key in RICH},
            history_ids={p:['previous','1208600'] if p.startswith('home_') else [] for p in PROFILES})
        return profile,rows,statistics

    def test_quarantined_xg_does_not_remove_other_features_or_history(self):
        profile,rows,statistics=self.fixture();original=deepcopy(profile)
        result=clean_rich(profile,rows,statistics)
        self.assertEqual(result['home_all_first_xg_for_mean'],2.)
        self.assertEqual(result['home_all_first_xg_against_mean'],1.)
        self.assertEqual(result['home_all_first_xg_for_count'],1)
        self.assertEqual(result['home_all_first_on_goal_for_mean'],1.)
        self.assertEqual(profile,original)

    def test_current_target_statistics_never_enter_features(self):
        profile,rows,statistics=self.fixture()
        before=features(profile,rows,statistics)
        statistics['target']={'periods':{'first':{'expected_goals':[1000.,0.]}}}
        self.assertEqual(features(profile,rows,statistics),before)
        self.assertEqual((len(before[0]),len(before[1])),(24,48))
        self.assertTrue(np.isfinite(np.concatenate(before)).all())

    def test_unknown_team_in_history_is_rejected(self):
        profile,rows,statistics=self.fixture();rows['previous']['away_id']='999'
        with self.assertRaises(ValueError):clean_rich(profile,rows,statistics)

    def test_missing_xg_has_zero_count_and_rest_is_bounded(self):
        profile,rows,statistics=self.fixture();statistics.clear()
        profile['core']['home_all_days_rest']=-1
        profile['core']['away_all_days_rest']=100000
        c,e=features(profile,rows,statistics)
        self.assertEqual(c[CORE.index('home_all_days_rest')],0)
        self.assertEqual(c[CORE.index('away_all_days_rest')],np.log1p(60))
        self.assertEqual(e[RICH.index('home_all_first_xg_for_mean')],0)
        self.assertEqual(e[RICH.index('home_all_first_xg_for_count')],0)


if __name__=='__main__':unittest.main()
