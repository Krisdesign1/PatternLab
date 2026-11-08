from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import causal_candidate as candidate
from causal_feature_workspace import digest
from halftime_domain import CLASS_ORDER, validate_probabilities
from scenario_features import FEATURE_COLUMNS, CATEGORICAL_COLUMNS


def development_fixture():
    """Algorithm unit fixture; source-contract integration uses the shared factory."""
    phases, snapshots = {}, {}
    schema = dict(columns=list(FEATURE_COLUMNS), categorical_columns=list(CATEGORICAL_COLUMNS),
                  class_order=list(CLASS_ORDER))
    for index, name in enumerate(candidate.PHASES):
        phases[name] = []
        for number, label in enumerate(CLASS_ORDER):
            sid = digest([name, number])
            values = {column: 'club' if column in CATEGORICAL_COLUMNS else float(number)
                      for column in FEATURE_COLUMNS}
            phases[name].append(dict(features=values, label=label, snapshot_id=sid,
                                     decision_at=f'2026-10-{index+1:02}T08:00:00+00:00',
                                     outcome_source=dict(observed_at_utc=f'2026-10-{index+1:02}T17:00:00+00:00'),
                                     match=dict(match_id=str(index*10+number+1), timestamp=f'2026-10-{index+1:02}T12:00:00')))
            snapshots[sid] = dict(id=sid, references=dict(clubs={c: 1/3 for c in CLASS_ORDER},
                                                         frequency={c: 1/3 for c in CLASS_ORDER},
                                                         clubs_version='synthetic-clubs-v1'))
    import causal_partitions as partitions
    from test_causal_partitions import windows
    plan = partitions.make_plan(windows(), minimums=dict(train=3, tuning=3, calibration=3))
    value = dict(version='causal-development-v1', plan=plan,
                 plan_id=plan['id'], partition_id=digest('partition'), cohort_id=digest('cohort'),
                 feature_schema=schema, phases=phases, snapshots=snapshots,
                 minimums=dict(train=3, tuning=3, calibration=3), ready=True, blockers=[],
                 temporal_quality='retrospective_conditional', prospective_confirmation=False,
                 active_model_changed=False)
    return dict(value, id=digest(value))


def reseal(value):
    value['id'] = digest({k: v for k, v in value.items() if k != 'id'})
    return value


class CausalCandidateTests(unittest.TestCase):
    def fitted_unit(self):
        value = development_fixture()
        objects = [object(), object(), object()]
        calls = []
        def predict(model, rows, schema):
            calls.append((model, [row['snapshot_id'] for row in rows]))
            if model is objects[0]:
                return [{c: 1/3 for c in CLASS_ORDER} for _ in rows]
            return [{c: .8 if c == row['label'] else .1 for c in CLASS_ORDER} for row in rows]
        with patch('causal_partitions.validate_development') as validate, \
             patch.object(candidate, '_fit', side_effect=objects) as fit, \
             patch.object(candidate, '_predict', side_effect=predict):
            model, report = candidate.fit_candidate(value)
        return value, model, report, fit, validate, calls

    def test_contract_and_readiness_checked_before_any_training(self):
        with patch('causal_partitions.validate_development', side_effect=ValueError('invalid')), \
             patch.object(candidate, '_fit') as fit:
            with self.assertRaisesRegex(ValueError, 'invalid'):
                candidate.fit_candidate({})
            fit.assert_not_called()
        value = development_fixture(); value.update(ready=False, blockers=['too few'])
        with patch('causal_partitions.validate_development'), patch.object(candidate, '_fit') as fit:
            with self.assertRaisesRegex(candidate.CandidateError, 'too few'):
                candidate.fit_candidate(value)
            fit.assert_not_called()

    def test_tuning_refit_and_calibration_receive_disjoint_intended_phases(self):
        value, model, report, fit, validate, calls = self.fitted_unit()
        validate.assert_called_once_with(value)
        expected = [[row['snapshot_id'] for row in value['phases'][name]] for name in candidate.PHASES]
        fitted = [[row['snapshot_id'] for row in call.args[0]] for call in fit.call_args_list]
        self.assertEqual(fitted, [expected[0], expected[0], expected[0]+expected[1]])
        self.assertEqual([ids for _, ids in calls], [expected[1], expected[1], expected[2]])
        self.assertEqual(report['selection']['params'], candidate.RECIPE['candidates'][1])
        self.assertEqual(report['plan'], value['plan'])
        self.assertEqual(report['final_training']['phases'], ['train', 'tuning'])
        self.assertEqual(report['calibration']['phase'], 'calibration')
        self.assertFalse(report['confirmation_used'])
        self.assertFalse(report['active_model_changed'])
        self.assertNotIn('confirmation', report['inputs']['phases'])
        self.assertTrue(all(call.args[3] == 20261030 for call in fit.call_args_list))
        self.assertEqual(set(report['references']), {'tuning', 'calibration'})
        self.assertEqual(report['references']['calibration']['clubs']['count'], 3)
        self.assertTrue(report['calibration_metrics_are_in_sample'])

    def test_plan_recipe_cannot_change_or_coerce_a_temperature_to_bool(self):
        for replacement in (True, 42):
            value = development_fixture(); value['plan']['recipe']['temperatures'][2] = replacement
            with patch('causal_partitions.validate_development'), patch.object(candidate, '_fit') as fit:
                with self.assertRaises(candidate.CandidateError):
                    candidate.fit_candidate(value)
                fit.assert_not_called()

    def test_future_source_labels_are_refused_before_any_fit(self):
        value = development_fixture()
        value['phases']['train'][0]['outcome_source']['observed_at_utc'] = (
            datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
        with patch('causal_partitions.validate_development'), patch.object(candidate, '_fit') as fit:
            with self.assertRaisesRegex(candidate.CandidateError, 'début réel'):
                candidate.fit_candidate(value)
            fit.assert_not_called()

    def test_resealed_fit_times_cannot_predate_plan_or_end_in_future(self):
        _, _, report, _, _, _ = self.fitted_unit()
        before = deepcopy(report)
        before['started_at'] = (datetime.fromisoformat(report['plan']['created_at'])-timedelta(seconds=1)).isoformat()
        future = deepcopy(report)
        future['completed_at'] = (datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
        for changed in (before, future):
            reseal(changed)
            with self.assertRaisesRegex(candidate.CandidateError, 'heures'):
                candidate.validate_report(changed)

    def test_temperature_is_identity_at_one_and_strict_on_invalid_input(self):
        probabilities = {'1st': 0., 'Égal': .3, '2nd': .7}
        self.assertEqual(candidate.temperature(probabilities, 1.), probabilities)
        for value in (.5, .75, 1.25, 2., 3.):
            result = candidate.temperature(probabilities, value)
            validate_probabilities(result)
            self.assertEqual(max(result, key=result.get), '2nd')
        for value in (True, 0, -1, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                candidate.temperature(probabilities, value)
        with self.assertRaises(ValueError):
            candidate.metrics([probabilities], [])
        with self.assertRaises(ValueError):
            candidate.metrics([probabilities], ['unknown'])

    def test_uniform_calibration_prefers_identity_without_using_confirmation(self):
        rows = [dict(temperature=value, metrics=dict(logloss=1.)) for value in candidate.RECIPE['temperatures']]
        self.assertEqual(candidate._best_temperature(rows)['temperature'], 1.)

    def test_resealed_report_cannot_change_roles_selection_or_calibration(self):
        _, _, report, _, _, _ = self.fitted_unit()
        mutations = [lambda r: r.update(confirmation_used=True),
                     lambda r: r['final_training'].update(phases=['train', 'tuning', 'calibration']),
                     lambda r: r['selection'].update(params=candidate.RECIPE['candidates'][0]),
                     lambda r: r['calibration'].update(temperature=10.),
                     lambda r: r['calibration'].update(temperature=True),
                     lambda r: r['references']['calibration'].pop('clubs'),
                     lambda r: r['implementation'].update({'catboost_scenarios.py': digest('changed-code')}),
                     lambda r: r['inputs']['phases']['tuning'].update(snapshot_ids=r['inputs']['phases']['train']['snapshot_ids']),
                     lambda r: r['recipe']['temperatures'].__setitem__(2, True)]
        for mutate in mutations:
            changed = deepcopy(report); mutate(changed); reseal(changed)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                candidate.validate_report(changed)

    def test_native_candidate_roundtrip_is_exclusive_and_never_publishes(self):
        value = development_fixture()
        with patch('causal_partitions.validate_development'), \
             patch.object(candidate.ml, '_publish', side_effect=AssertionError('No publication')) as publish:
            model, report = candidate.fit_candidate(value)
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)/'candidate'
            candidate.write_candidate(destination, model, report)
            loaded = candidate.load_candidate(destination)
            features = [row['features'] for row in value['phases']['calibration']]
            first = candidate.predict_candidate(loaded, features)
            self.assertEqual(first, candidate.predict_candidate(loaded, iter(features)))
            self.assertEqual(len(first), 3)
            for row in first:
                validate_probabilities(row)
            with self.assertRaises(FileExistsError):
                candidate.write_candidate(destination, model, report)
            with self.assertRaises(ValueError):
                candidate.predict_candidate(loaded, [dict(features[0], unknown=3)])
            (destination/'candidate.cbm').write_bytes(b'altered')
            with self.assertRaisesRegex(ValueError, 'altéré'):
                candidate.load_candidate(destination)
            partial = Path(temporary)/'partial'; partial.mkdir()
            (partial/'report.json').write_text(json.dumps(report), encoding='utf-8')
            with self.assertRaises(FileNotFoundError):
                candidate.load_candidate(partial)
        publish.assert_not_called()

    def test_serving_directory_is_never_a_candidate_destination(self):
        _, model, report, _, _, _ = self.fitted_unit()
        # A fake model contract keeps this check independent of CatBoost I/O.
        with patch.object(candidate, '_model_contract'):
            with self.assertRaisesRegex(candidate.CandidateError, 'modèles servis'):
                candidate.write_candidate(candidate.ml.DATA_DIR/'forbidden-test', model, report)
            with self.assertRaisesRegex(candidate.CandidateError, 'modèles servis'):
                candidate.write_candidate(Path(__file__).resolve().parent/'data/api_football/forbidden-test', model, report)

    def test_full_source_validated_development_native_fit_and_fresh_process_reopen(self):
        from test_causal_partitions import synthetic_bundle, windows
        import causal_partitions as partitions
        bundle = synthetic_bundle()
        plan = partitions.make_plan(windows(), minimums=dict(train=3, tuning=3, calibration=3))
        partition = partitions.prepare(bundle, plan)
        development = partitions.development_inputs(partition, bundle)
        frozen = deepcopy(development)
        with patch.object(candidate.ml, '_publish', side_effect=AssertionError('No publication')):
            model, report = candidate.fit_candidate(development)
        self.assertEqual(development, frozen)
        self.assertEqual(report['inputs']['development_id'], development['id'])
        self.assertEqual(report['inputs']['phases']['calibration']['count'], 3)
        self.assertEqual(report['reference_clubs_versions'],
                         sorted({s['references']['clubs_version'] for s in development['snapshots'].values()}))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); destination = root/'candidate'
            candidate.write_candidate(destination, model, report)
            features = [row['features'] for row in development['phases']['calibration']]
            (root/'features.json').write_text(json.dumps(features), encoding='utf-8')
            expected = candidate.predict_candidate(candidate.load_candidate(destination), features)
            code = '''import json, sys
from pathlib import Path
from unittest.mock import patch
import causal_candidate as c
root = Path(sys.argv[1])
with patch.object(c, 'fit_candidate', side_effect=AssertionError('No fit while loading')):
    saved = c.load_candidate(root/'candidate')
    print(json.dumps(c.predict_candidate(saved, json.loads((root/'features.json').read_text()))))
'''
            result = subprocess.run([sys.executable, '-c', code, str(root)],
                                    cwd=Path(__file__).resolve().parent, text=True, capture_output=True, check=True)
            self.assertEqual(json.loads(result.stdout), expected)


if __name__ == '__main__':
    unittest.main()
