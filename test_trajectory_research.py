from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sqlite3
import unittest

import numpy as np

import trajectory_research as research
import trajectory_research_features as features
from test_trajectory_research_features import fixture


class TrajectoryResearchTests(unittest.TestCase):
    def test_fixed_partitions_and_past_support(self):
        plan = research.new_plan(now=datetime(2026, 10, 25, tzinfo=timezone.utc))
        self.assertEqual(research.partition('2024-12-31', plan), 'train')
        self.assertEqual(research.partition('2025-01-01', plan), 'validation')
        self.assertEqual(research.partition('2026-01-01', plan), 'test')
        self.assertIsNone(research.partition('2026-10-25', plan))
        e = dict(context=dict(league_count=30, home_overall_count=10, away_overall_count=10,
                              home_role_count=3, away_role_count=3))
        self.assertTrue(research.eligible(e, plan))
        e['context']['away_role_count'] = 2
        self.assertFalse(research.eligible(e, plan))

    def test_plan_cannot_include_future_or_overwrite(self):
        with self.assertRaises(ValueError):
            research.new_plan('2099-01-01')
        with TemporaryDirectory() as root:
            path = research.prepare(Path(root)/'run')
            original = (path/'plan.json').read_bytes()
            with self.assertRaises(FileExistsError):
                research.prepare(path)
            self.assertEqual((path/'plan.json').read_bytes(), original)

    def test_source_is_read_only_and_excludes_target_day(self):
        with TemporaryDirectory() as root:
            database = Path(root)/'source.sqlite3'
            con = sqlite3.connect(database)
            con.execute('CREATE TABLE fixtures(match_id TEXT, normalized_json TEXT, kickoff_utc TEXT)')
            for i in (1, 2):
                row = fixture(i, i)
                con.execute('INSERT INTO fixtures VALUES(?,?,?)', (str(i), json.dumps(row), row['timestamp']))
            con.commit()
            con.close()
            before = database.read_bytes()
            run = Path(root)/'run'
            run.mkdir()
            rows, fingerprint = research._source_snapshot(database, run, '2024-01-02')
            self.assertEqual([row['match_id'] for row in rows], ['1'])
            self.assertEqual(len(fingerprint), 64)
            self.assertEqual(database.read_bytes(), before)

    def test_probability_metrics_compare_baseline_on_same_selected_matches(self):
        p = np.array([[.8, .1, .1], [.2, .3, .5]])
        y = np.array([0, 1])
        baseline = np.array([[.2, .3, .5], [.1, .8, .1]])
        result = research.summarize(y, p, baseline, thresholds=(.7,))
        self.assertEqual(result['correct'], 1)
        selection = result['thresholds'][0]
        self.assertEqual(selection['matches'], 1)
        self.assertEqual(selection['accuracy'], 1.)
        self.assertEqual(selection['baseline_accuracy_same_matches'], 0.)
        with self.assertRaises(ValueError):
            research.summarize(y, np.array([[.8, .8, .1], [.2, .3, .5]]))

    def test_paired_week_interval_is_zero_for_identical_predictions(self):
        meta = [dict(day='2026-01-01'), dict(day='2026-01-15')]
        p = np.array([[.4, .2, .4], [.3, .3, .4]])
        result = research.paired_weeks(meta, np.array([0, 2]), p, p)
        self.assertEqual(result['weeks'], 2)
        self.assertEqual(result['approximate_95_interval'], [0., 0.])

    def test_real_catboost_pool_maps_labels_and_categorical_features(self):
        from catboost import CatBoostClassifier
        base = np.arange(60, dtype=np.float32).reshape(20, 3)
        categories = [('39', str(i % 4+1), str(i % 5+10)) for i in range(20)]
        labels = np.arange(20, dtype=np.int32) % 3
        pool = research._pool(base, None, categories, np.arange(20), labels)
        model = CatBoostClassifier(iterations=3, depth=2, thread_count=1, verbose=False,
                                   allow_writing_files=False, class_names=list(features.LABELS), loss_function='MultiClass')
        model.fit(pool)
        self.assertEqual(list(model.classes_), list(features.LABELS))
        result = research.summarize(labels, model.predict_proba(pool))
        self.assertEqual(result['matches'], 20)


if __name__ == '__main__':
    unittest.main()
