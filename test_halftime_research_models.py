"""Model mechanics and evaluation gates on synthetic data only."""
import contextlib
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from halftime_research_models import fit,predict,metrics,compare,evaluate_complete_snapshot
from halftime_research_experiment import TREE_PARAMETERS,readiness,evaluate


class ResearchModelTests(unittest.TestCase):
    def test_baseline_is_applied_once_and_class_order_preserved(self):
        rng=np.random.default_rng(7);x=rng.normal(size=(90,4));y=np.arange(90)%3
        offset=np.tile([.2,.3,.5],(90,1))
        model=fit(x,y,offset,{**TREE_PARAMETERS,'iterations':3})
        actual=predict(model,x,offset)
        from scipy.special import softmax
        from catboost import Pool
        raw=model.predict(Pool(x.astype(np.float32)),prediction_type='RawFormulaVal')
        expected=softmax(np.asarray(raw)+np.log(offset).astype(np.float32),axis=1)
        np.testing.assert_allclose(actual,expected,rtol=0,atol=1e-12)
        self.assertEqual(list(model.classes_),['1st','Égal','2nd'])
    def test_confidence_reports_exact_selected_coverage_and_reference(self):
        p=np.asarray([[.8,.1,.1],[.2,.3,.5],[.4,.3,.3]])
        y=np.asarray([0,2,1]);reference=np.tile([.2,.3,.5],(3,1))
        result=metrics(y,p,reference)
        self.assertEqual(result['confidence_thresholds']['0.7']['matches'],1)
        self.assertEqual(result['confidence_thresholds']['0.7']['accuracy'],1)
        self.assertEqual(result['confidence_thresholds']['0.7']['reference_accuracy'],0)
        self.assertEqual(sum(b['count'] for b in result['calibration']['1st']),3)
        self.assertAlmostEqual(result['accuracy'],2/3)
    def test_incomplete_collection_refuses_even_with_many_examples(self):
        examples=[dict(day=str(year)+'-06-01',actual=label,support=dict(home_all=10,away_all=10))
                  for year in (2024,2025,2026) for _ in range(110) for label in ('1st','Égal','2nd')]
        coverage={str(year):dict(targets=330,observed=330) for year in (2024,2025,2026)}
        self.assertTrue(readiness(coverage,examples)['ready'])
        coverage['2025']['observed']=329
        self.assertFalse(readiness(coverage,examples)['ready'])
    def test_insufficient_rich_history_refuses_instead_of_filtering_targets(self):
        examples=[dict(day=str(year)+'-06-01',actual=label,support=dict(home_all=0,away_all=10))
                  for year in (2024,2025,2026) for _ in range(110) for label in ('1st','Égal','2nd')]
        coverage={str(year):dict(targets=330,observed=330) for year in (2024,2025,2026)}
        result=readiness(coverage,examples)
        self.assertFalse(result['ready']);self.assertEqual(result['examples']['validation'],330)
    def test_evaluation_gate_prevents_any_fit(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp);(path/'readiness.json').write_text(json.dumps(dict(ready=False)))
            with patch('halftime_research_models.fit',side_effect=AssertionError('must not fit')):
                with self.assertRaises(ValueError):evaluate_complete_snapshot(path,{})
    def test_no_interval_with_too_few_closed_weeks(self):
        y=np.asarray([0,1,2]);p=np.tile([.3,.3,.4],(3,1))
        result=compare(y,p,p,np.asarray(['2026-01-03','2026-01-10','2026-01-17']))
        self.assertIsNone(result['interval']);self.assertEqual(result['gain_positive_is_better'],0.)
    def test_synthetic_end_to_end_writes_selection_then_results(self):
        rng=np.random.default_rng(17);n=330;ys=np.arange(n)%3
        years=(2024,2025,2026)
        y=np.tile(ys,3);core=rng.normal(size=(3*n,4));rich=np.eye(3)[y]
        days=np.concatenate([np.asarray([f'{year}-06-{(i%25)+1:02d}' for i in range(n)]) for year in years])
        # Sorting within each chronological partition avoids artificial time inversions.
        order=np.argsort(days,kind='stable');days=days[order];y=y[order];core=core[order];rich=rich[order]
        p=np.tile([.3,.3,.4],(3*n,1))
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp);(path/'readiness.json').write_text(json.dumps(dict(ready=True)))
            np.savez_compressed(path/'dataset.npz',ids=np.asarray([str(i+1) for i in range(3*n)]),days=days,
                y=y,core=core,rich=rich,clubs=p,frequency=p,support=np.full((3*n,2),10),leagues=np.full(3*n,'39'))
            plan=dict(learner=dict(parameters={**TREE_PARAMETERS,'iterations':8}))
            result=evaluate_complete_snapshot(path,plan)
            self.assertTrue((path/'evaluation/validation_selection.json').exists())
            self.assertEqual(result['metrics']['rich_fixed']['matches'],330)
            self.assertFalse(result['production_changed'])
            self.assertLess(result['metrics']['rich_fixed']['logloss'],result['metrics']['scores_fixed']['logloss'])
            self.assertEqual(result['policies']['rich'],'head')
            from catboost import CatBoostClassifier
            loaded=CatBoostClassifier();loaded.load_model(str(path/'evaluation/rich_final.cbm'))
            saved=np.load(path/'evaluation/predictions.npz',allow_pickle=False)
            np.testing.assert_allclose(saved['rich_fixed'],predict(loaded,np.column_stack((core,rich))[days>='2026-01-01'],p[days>='2026-01-01']),atol=0,rtol=0)


if __name__=='__main__':unittest.main()
