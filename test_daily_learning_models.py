"""Causal, immutable candidate tests, isolated from provider and served files."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from unittest.mock import patch
import json

import daily_learning_models as daily


NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
REAL_FIT, REAL_CLASSIFIER, REAL_POOL = daily.ml._fit, daily.ml._classifier, daily.ml._pool


def rows(count=36):
    result = []
    start = datetime(2025, 1, 1, 12, tzinfo=timezone.utc)
    scores = [('1-0', '1-0', '1st'), ('0-0', '0-0', 'Égal'), ('0-0', '0-1', '2nd')]
    for index in range(count):
        ht, ft, value = scores[index % 3]
        stamp = (start+timedelta(days=index)).replace(tzinfo=None).isoformat()
        result.append(dict(provider='api-football', league='1', match_id=str(index+1),
            home='Home', away='Away', home_id='10', away_id='20', season=2025,
            timestamp=stamp, ht=ht, ft=ft, value=value))
    return result


def fixture(mid='1000'):
    return dict(provider='api-football', league='1', match_id=mid, home='Home', away='Away',
                home_id='10', away_id='20', season=2026, timestamp='2026-10-30T12:00:00')


class FakeClassifier:
    def __init__(self):
        self.classes_ = list(daily.VALUES)
        self.probabilities = [.2, .3, .5]

    def save_model(self, path, format):
        Path(path).write_text(json.dumps(self.probabilities))

    def load_model(self, path, format):
        self.probabilities = json.loads(Path(path).read_text())

    def predict_proba(self, features):
        return [list(self.probabilities) for _ in features]


class DailyLearningModelsTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fit_calls = []

        def fit(examples, params, columns, categorical):
            self.fit_calls.append((examples, params, columns, categorical))
            return FakeClassifier()

        for target, value in (('_fit', fit), ('_classifier', lambda: FakeClassifier),
                              ('_pool', lambda features, columns, categorical, labels=None: features)):
            mocked = patch.object(daily.ml, target, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        storage = patch.object(daily.cloud_storage, 'configured', return_value=False)
        storage.start(); self.addCleanup(storage.stop)

    def train(self, values=None, now=NOW):
        return daily.train(rows() if values is None else values, '1', self.root, now=now)

    def incumbent(self, params=None, probabilities=None):
        params = dict(params or dict(depth=5, iterations=300, l2_leaf_reg=8))
        generation = 'a'*64
        path = self.root/'models'/'api-football'/'1'/generation
        path.mkdir(parents=True)
        (path/'model.cbm').write_text(json.dumps(probabilities or [.25, .35, .4]))
        module = daily.ml._features()
        history = rows()
        report = dict(version=daily.ml.VERSION, source='API-Football', league='1',
            feature_version=module.FEATURE_VERSION, feature_columns=list(module.FEATURE_COLUMNS),
            categorical_columns=list(module.CATEGORICAL_COLUMNS), labels=list(daily.VALUES),
            selection=dict(alpha=0., params=params), deployment=dict(generation=generation,
                model_sha256=daily._file_digest(path/'model.cbm'), trained_through=history[-1]['timestamp'][:10],
                cutoff_exclusive='2026-10-28', fingerprint=daily.ml._fingerprint(history)))
        (path/'report.json').write_text(daily._json(report))
        (path.parent/'CURRENT.json').write_text(daily._json(dict(generation=generation,
            report_sha256=daily._file_digest(path/'report.json'))))
        return path, report

    def test_plan_is_read_only_and_protocol_is_explicit_immutable(self):
        before = list(self.root.rglob('*'))
        self.assertFalse(daily.plan(rows(), '1', self.root, now=NOW)['ready'])
        self.assertEqual(list(self.root.rglob('*')), before)
        p = daily.prepare_protocol(self.root, now=NOW)
        self.assertEqual(daily.prepare_protocol(self.root, now=NOW+timedelta(days=1)), p)
        self.assertFalse(p['automatic_promotion'])
        self.assertEqual(p['labels'], ['1st', 'Égal', '2nd'])
        self.assertTrue(daily.plan(rows(), '1', self.root, now=NOW)['changed'])

    def test_training_ignores_target_day_future_and_foreign_scores(self):
        values = rows()+[dict(rows()[0], match_id='500', timestamp='2026-10-29T01:00:00',ht='bad',ft='bad'),
                         dict(rows()[0], match_id='501', timestamp='2026-10-30T01:00:00',value='bad'),
                         dict(rows()[0], match_id='502', provider='foreign', ht='bad')]
        result = self.train(values)
        self.assertEqual(result['history_matches'], 36)
        self.assertEqual(len(self.fit_calls), 1)
        self.assertEqual([r['match']['match_id'] for r in self.fit_calls[0][0]], [str(i) for i in range(1,37)])
        self.assertEqual(result['recipe_origin'], 'declared_fallback_not_validated')
        self.assertFalse(result['promoted'])
        self.assertFalse((self.root/'models').exists())

    def test_same_input_reopens_no_refit_and_changed_input_new_version(self):
        first = self.train()
        same = self.train(now=NOW+timedelta(days=1))
        self.assertEqual(first, same)
        self.assertEqual(len(self.fit_calls), 1)
        self.assertFalse(daily.plan(rows(), '1', self.root, now=NOW+timedelta(days=1))['changed'])
        added = dict(rows()[0], match_id='900', timestamp='2026-10-28T12:00:00')
        second = self.train(rows()+[added],now=NOW+timedelta(days=1))
        self.assertNotEqual(first['candidate_id'],second['candidate_id'])
        self.assertEqual(len(self.fit_calls), 2)
        self.assertEqual(daily.latest('1',self.root),second)
        self.assertEqual(daily.load_candidate('1',first['candidate_id'],self.root),first)

    def test_recipe_and_incumbent_are_pinned_without_serving_mutation(self):
        source, report = self.incumbent()
        original = {str(p):p.read_bytes() for p in source.parent.rglob('*') if p.is_file()}
        first = self.train()
        self.assertEqual(first['params'], report['selection']['params'])
        self.assertEqual(first['incumbent_generation'], report['deployment']['generation'])
        self.assertEqual(original,{str(p):p.read_bytes() for p in source.parent.rglob('*') if p.is_file()})
        self.assertEqual(daily.predict_incumbent(first, rows(), [fixture()], self.root),
                         [dict(zip(daily.VALUES,[.25,.35,.4]))])
        # A later served pointer never changes the comparison pinned in first.
        (source.parent/'CURRENT.json').write_text('{}')
        self.assertEqual(daily.predict_incumbent(first,rows(),[fixture()],self.root)[0]['2nd'], .4)

    def test_predictions_are_score_free_individual_and_never_fit(self):
        candidate = self.train()
        clean = daily.predict(candidate,rows(),[fixture()],self.root)
        poisoned = dict(fixture(), ht='99-0',ft='0-99',value='1st',actual='1st',score={'halftime':'bad'})
        self.assertEqual(clean,daily.predict(candidate,rows(),[poisoned],self.root))
        self.assertEqual(clean,[{'1st':.2,'Égal':.3,'2nd':.5}])
        self.assertIsNone(daily.predict_incumbent(candidate,rows(),[fixture()],self.root))
        self.assertEqual(len(self.fit_calls),1)
        with self.assertRaises(daily.DailyLearningError):
            daily.predict(candidate,rows(),[fixture(),fixture()],self.root)
        with self.assertRaises(daily.DailyLearningError):
            daily.predict(candidate,rows(),[dict(fixture(),league='2')],self.root)

    def test_insufficient_history_classes_and_dates_are_explicit(self):
        daily.prepare_protocol(self.root,now=NOW)
        for history in (rows(20),[dict(r,value='Égal',ht='0-0',ft='0-0') for r in rows()]):
            planned=daily.plan(history,'1',self.root,now=NOW)
            self.assertFalse(planned['ready'])
            with self.assertRaises(daily.DailyLearningError):self.train(history)
        self.assertEqual(len(self.fit_calls),0)

    def test_artifact_input_and_protocol_tampering_fail_closed(self):
        candidate=self.train()
        model=daily._path('1',candidate['candidate_id'],self.root)/'fit'/'model.cbm'
        model.write_text('[.1,.1,.8]')
        with self.assertRaises(daily.DailyLearningError):daily.load_candidate('1',candidate['candidate_id'],self.root)
        protocol=daily.protocol_path(self.root)
        value=json.loads(protocol.read_text());value['automatic_promotion']=True
        protocol.write_text(json.dumps(value))
        with self.assertRaises(daily.DailyLearningError):daily.load_protocol(self.root)

    def test_corrected_or_late_backfilled_training_input_requires_new_candidate(self):
        candidate=self.train()
        changed=rows();changed[0]=dict(changed[0],ht='0-0',ft='0-1',value='2nd')
        with self.assertRaises(daily.DailyLearningError):daily.predict(candidate,changed,[fixture()],self.root)
        self.assertTrue(daily.plan(changed,'1',self.root,now=NOW)['changed'])

    def test_failed_fit_can_resume_first_snapshot_without_new_incumbent(self):
        with patch.object(daily.ml,'_fit',side_effect=RuntimeError('stop')):
            with self.assertRaises(RuntimeError):self.train()
        candidate=self.train(now=NOW+timedelta(days=1))
        self.assertEqual(candidate['frozen_at'],NOW.isoformat())
        self.assertEqual(candidate['cutoff_exclusive'],'2026-10-29')
        self.assertEqual(candidate['completed_at'],(NOW+timedelta(days=1)).isoformat())

    def test_real_native_catboost_round_trip_isolated_from_serving(self):
        with patch.object(daily.ml,'_fit',REAL_FIT), patch.object(daily.ml,'_classifier',REAL_CLASSIFIER), patch.object(daily.ml,'_pool',REAL_POOL):
            candidate=self.train(rows(60))
            p=daily.predict(candidate,rows(60),[fixture()],self.root)
            self.assertEqual(set(p[0]),set(daily.VALUES))
            self.assertAlmostEqual(sum(p[0].values()),1.,places=12)
            self.assertTrue(all(0<value<1 for value in p[0].values()))
            self.assertFalse((self.root/'models').exists())

    def test_real_native_artifact_resumes_after_manifest_interruption_without_refit(self):
        writes = daily._write_new
        fits = []

        def interrupted(path, value):
            if Path(path).name == 'manifest.json':
                raise RuntimeError('interrupted before final manifest')
            return writes(path, value)

        def fitting(*args):
            fits.append(True)
            return REAL_FIT(*args)

        with patch.object(daily.ml,'_fit',fitting), patch.object(daily.ml,'_classifier',REAL_CLASSIFIER), patch.object(daily.ml,'_pool',REAL_POOL):
            with patch.object(daily,'_write_new',interrupted):
                with self.assertRaises(RuntimeError):self.train(rows(60))
            location = next((self.root/'daily_learning'/'candidates'/'1').glob('*/fit'))
            original = (location/'model.cbm').read_bytes()
            self.assertTrue((location/'checkpoint.json').is_file())
            self.assertFalse((location.parent/'manifest.json').exists())
            candidate = self.train(rows(60),now=NOW+timedelta(hours=1))
            self.assertEqual(len(fits),1)
            self.assertEqual((location/'model.cbm').read_bytes(),original)
            self.assertEqual(candidate['completed_at'],(NOW+timedelta(hours=1)).isoformat())
            self.assertAlmostEqual(sum(daily.predict(candidate,rows(60),[fixture()],self.root)[0].values()),1.)
            self.assertFalse((self.root/'models').exists())


if __name__=='__main__':main()
