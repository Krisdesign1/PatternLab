import copy
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import database
from ml_model import (
    dataset_fingerprint, evaluate_model, export_experiment,
    fit_model, predict_next, train_experiment,
)


def make_rows(sequences):
    return [{'id': i + 1, 'sequence': list(seq)} for i, seq in enumerate(sequences)]


class TestLearning(unittest.TestCase):
    def test_fit_never_crosses_coupon_boundary(self):
        model = fit_model([['1st'] * 4, ['2nd'] * 4])
        self.assertEqual(model['counts']['1st']['2nd'], 0)
        self.assertEqual(model['transitions'], 6)
        self.assertEqual(model['counts']['1st']['1st'], 3)

    def test_probabilities_and_unseen_state_fallback(self):
        model = fit_model([['1st'] * 4])
        p = predict_next(model, ['1st'])
        self.assertAlmostEqual(p['probabilities']['1st'], 4 / 6)
        self.assertAlmostEqual(sum(p['probabilities'].values()), 1)
        self.assertFalse(p['used_global_fallback'])
        fallback = predict_next(model, ['2nd'])
        self.assertTrue(fallback['used_global_fallback'])
        self.assertEqual(fallback['context_support'], 0)
        self.assertEqual(fallback['probabilities'], p['probabilities'])

    def test_holdout_changes_cannot_affect_evaluation_model(self):
        rows = make_rows([['1st'] * 4] * 4 + [['Égal'] * 4])
        first = train_experiment(rows)
        rows[-1]['sequence'] = ['2nd'] * 4
        second = train_experiment(rows)
        self.assertEqual(first['evaluation_model'], second['evaluation_model'])
        self.assertNotEqual(first['model'], second['model'])
        self.assertEqual(first['train_ids'], [1, 2, 3, 4])
        self.assertEqual(first['test_ids'], [5])
        self.assertFalse(set(first['train_ids']) & set(first['test_ids']))
        self.assertEqual(first['evaluation_model']['targets']['Égal'], 0)
        self.assertEqual(first['model']['targets']['Égal'], 3)

    def test_evaluation_does_not_update_model(self):
        model = fit_model([['1st'] * 4])
        before = copy.deepcopy(model)
        score = evaluate_model(model, [['2nd'] * 4])
        self.assertEqual(model, before)
        self.assertEqual(score['predictions'], 3)
        self.assertEqual(score['model']['accuracy'], 0)
        self.assertAlmostEqual(score['model']['log_loss'], math.log(6))

    def test_learned_signal_beats_baseline_on_known_pattern(self):
        model = fit_model([['1st', '2nd', '1st', '2nd']] * 4)
        result = evaluate_model(model, [['1st', '2nd', '1st', '2nd']])
        self.assertEqual(result['model']['accuracy'], 1)
        self.assertAlmostEqual(result['baseline']['accuracy'], 2 / 3)
        self.assertLess(result['model']['log_loss'], result['baseline']['log_loss'])
        for name in ['model', 'baseline']:
            self.assertEqual(sum(sum(row.values()) for row in result[name]['confusion'].values()), 3)

    def test_duplicate_patterns_and_reproducibility(self):
        rows = make_rows([['1st'] * 4] * 12)
        first = train_experiment(rows)
        second = train_experiment(rows)
        self.assertEqual(first['evaluation'], second['evaluation'])
        self.assertEqual(len(first['train_ids']), 9)
        self.assertEqual(len(first['test_ids']), 3)
        self.assertEqual(first['test_patterns_already_in_train'], 3)
        self.assertEqual(json.loads(export_experiment(first))['model'], first['model'])

    def test_invalid_or_insufficient_data_rejected(self):
        for sequences in [[], [['1st']], [['1st', 'invalid', '2nd', 'Égal']]]:
            with self.assertRaises(ValueError):
                fit_model(sequences)
        with self.assertRaises(ValueError):
            train_experiment(make_rows([['1st'] * 4] * 4))

    def test_mutations_invalidate_fingerprint(self):
        rows = make_rows([['1st'] * 4, ['2nd'] * 4])
        original = dataset_fingerprint(rows)
        self.assertNotEqual(original, dataset_fingerprint(rows[::-1]))
        self.assertNotEqual(original, dataset_fingerprint(rows[:1]))
        rows[0]['sequence'][0] = 'Égal'
        self.assertNotEqual(original, dataset_fingerprint(rows))


class TestLearningUI(unittest.TestCase):
    def test_seed_does_not_create_a_database_or_manual_examples(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(database,'DB_PATH',Path(tmp)/'coupons.db'):
            database.seed_if_empty()
            self.assertFalse(database.DB_PATH.exists())
            self.assertEqual(database.SEED_COUPONS, [])


if __name__ == '__main__':
    unittest.main()
