"""Causal, sampling and likelihood invariants for the learning diagnosis."""
import unittest

import numpy as np

from halftime_count_models import class_probabilities
from halftime_learning_diagnostic import (FRACTIONS,causal_league_means,class_labels,
    losses,nested_indices,permuted_goals,probabilities,synthetic_counts)


class LearningDiagnosticTests(unittest.TestCase):
    def test_skellam_forecast_matches_unknown_total_integration(self):
        means=np.asarray([[1.2,1.5],[.01,5.],[2.1,.2],[.01,.01]])
        np.testing.assert_allclose(probabilities(means),class_probabilities(means),atol=1e-11,rtol=0)

    def test_joint_loss_factorization_and_zero_total(self):
        goals=np.asarray([[0,0],[0,3],[3,1],[2,2]])
        l=losses(goals,np.asarray([[1,1],[.2,3],[2,1],[1.5,2.1]]))
        np.testing.assert_allclose(l['total_nll']+l['allocation_nll'],l['joint_nll'],atol=1e-12)
        self.assertEqual(l['allocation_nll'][0],0.)
        np.testing.assert_array_equal(class_labels(goals),[1,2,0,1])

    def example_blocks(self):
        days=[];leagues=[]
        for month in (1,2,3):
            for day in range(1,9):
                for lid in ('39','61','71'):
                    days.extend([f'2024-{month:02d}-{day:02d}']*2);leagues.extend([lid]*2)
        return np.asarray(days),np.asarray(leagues)

    def test_stratified_nested_whole_league_days_and_full_once(self):
        days,leagues=self.example_blocks();previous=set()
        for fraction in FRACTIONS:
            idx=nested_indices(days,leagues,fraction,123)
            self.assertTrue(previous.issubset(set(idx)));previous=set(idx)
            np.testing.assert_array_equal(idx,nested_indices(days,leagues,fraction,123))
            self.assertEqual(len(idx),round(len(days)*fraction))
            for day,lid in zip(days,leagues):
                block=set(np.flatnonzero((days==day)&(leagues==lid)))
                self.assertTrue(block.issubset(previous) or block.isdisjoint(previous))
        np.testing.assert_array_equal(nested_indices(days,leagues,1.,123),np.arange(len(days)))
        np.testing.assert_array_equal(nested_indices(days,leagues,1.,456),np.arange(len(days)))

    def test_shuffling_preserves_paired_goals_and_stratum_counts(self):
        days,leagues=self.example_blocks();goals=np.column_stack((np.arange(len(days)),1000+np.arange(len(days))))
        result=permuted_goals(goals,days,leagues,77)
        self.assertTrue(np.all(result[:,1]-result[:,0]==1000))
        np.testing.assert_array_equal(result,permuted_goals(goals,days,leagues,77))
        for lid in set(leagues):
            for month in ('2024-01','2024-02','2024-03'):
                mask=(leagues==lid)&np.char.startswith(days,month)
                self.assertEqual(sorted(map(tuple,result[mask])),sorted(map(tuple,goals[mask])))

    def test_synthetic_control_scaling_uses_training_only(self):
        rng=np.random.default_rng(7);x=rng.normal(size=(40,48));v=rng.normal(size=(20,48))
        sy,vy,spec,_=synthetic_counts(x,v,99)
        self.assertEqual(spec['center'],float(x[:,16].mean()))
        self.assertEqual(spec['scale'],float(x[:,16].std()))
        changed=v.copy();changed[:,16]+=100
        sy2,_,spec2,_=synthetic_counts(x,changed,99)
        np.testing.assert_array_equal(sy,sy2);self.assertEqual(spec,spec2)
        self.assertEqual((sy.shape,vy.shape),((40,2),(20,2)))

    def test_causal_league_baseline_never_uses_target_or_late_previous_fixture(self):
        rows=[dict(match_id='1',league='39',timestamp='2024-01-01T12:00:00',ht='1-0',ft='2-1'),
              dict(match_id='2',league='39',timestamp='2024-01-02T12:00:00',ht='5-5',ft='9-9')]
        result=causal_league_means(rows,['2'])
        np.testing.assert_allclose(result,[[1,1.5]])
        rows[1]['ht']='0-0';rows[1]['ft']='0-0'
        np.testing.assert_array_equal(result,causal_league_means(rows,['2']))
        rows[0]['timestamp']='2024-01-01T23:00:00'
        np.testing.assert_array_equal(causal_league_means(rows,['2']),[[1,1]])


if __name__=='__main__':unittest.main()
