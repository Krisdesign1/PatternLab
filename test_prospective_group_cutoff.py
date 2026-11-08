"""Common decision boundaries and positional identity for frozen group methods."""
from copy import deepcopy
from datetime import datetime
import unittest
from unittest.mock import patch

import catboost_scenarios as ml
import contextual_scenarios as contextual
import prospective_models as models
from league_patterns import repeated_scenarios
from scenario_predictor import fit_sequences
from test_catboost_scenarios import fixtures, sample
from test_mixed_models import mixed_fixtures, mixed_history


DECISION = '2025-03-19T09:00:00+00:00'


class GroupCutoffTests(unittest.TestCase):
    def setUp(self):
        for patcher in (patch.object(ml, 'load_report', return_value=None),
                        patch.object(models, '_context_neighbors', side_effect=models.SnapshotError('Absent'))):
            patcher.start(); self.addCleanup(patcher.stop)

    def test_same_decision_excludes_every_result_on_or_after_its_utc_day(self):
        history = sample()
        poison = dict(history[0], match_id='80001', timestamp='2025-03-19T18:00:00', value='invalid')
        ordinary = models.build_methods(history, fixtures(), decision_at=DECISION)
        poisoned = models.build_methods(history+[poison], fixtures(), decision_at=DECISION)
        self.assertEqual(ordinary, poisoned)
        self.assertEqual(set(ordinary['methods']), {'frequency', 'clubs', 'combined'})
        for method in ordinary['methods'].values():
            self.assertEqual(method['training_before'], '2025-03-19')
            self.assertEqual(method['decision_at'], DECISION)
            self.assertEqual(method['training_matches'], len(history))

    def test_future_targets_do_not_advance_cutoff_or_infer_unsupported_trained_engine(self):
        with patch.object(models, '_catboost', side_effect=AssertionError('No later cutoff inference')), \
                patch.object(models, '_context_neighbors', side_effect=AssertionError('No later cutoff inference')):
            value = models.build_methods(sample(), fixtures(), decision_at=DECISION)
        self.assertNotIn('catboost', value['methods'])
        self.assertNotIn('context_neighbors', value['methods'])
        self.assertIn('coupure UTC commune', value['unavailable']['catboost'])

    def test_mixed_components_and_parent_share_the_same_decision_and_cutoff(self):
        chosen = mixed_fixtures()
        original = models.build_mixed_methods(mixed_history(), chosen, decision_at=DECISION)
        poison = [dict(row, timestamp='2025-03-19T18:00:00', value='invalid') for row in mixed_history()[:2]]
        changed = models.build_mixed_methods(mixed_history()+poison, chosen, decision_at=DECISION)
        self.assertEqual(original, changed)
        for method in original['methods'].values():
            self.assertEqual(method['training_before'], '2025-03-19')
            self.assertEqual(method['decision_at'], DECISION)
            self.assertEqual(method['ordered_match_ids'], [f['match_id'] for f in chosen])
            for part in method['distribution']['components']:
                child = part['method']
                self.assertEqual(child['training_before'], '2025-03-19')
                self.assertEqual(child['decision_at'], DECISION)
                self.assertEqual(child['ordered_match_ids'], [chosen[i]['match_id'] for i in part['indices']])

    def test_decision_must_be_aware_and_strictly_before_every_kickoff(self):
        for invalid in ('2025-03-19T09:00:00', datetime(2025,3,19,9),
                        '2025-03-20T12:00:00+00:00', '2025-03-21T09:00:00Z', True):
            for build, chosen, history in ((models.build_methods, fixtures(), sample()),
                    (models.build_mixed_methods, mixed_fixtures(), mixed_history())):
                with self.subTest(invalid=invalid, builder=build.__name__), self.assertRaises(models.SnapshotError):
                    build(history, chosen, decision_at=invalid)

    def test_timezone_conversion_defines_one_real_utc_day(self):
        context = models.group_context(fixtures(), '2025-03-20T00:30:00+02:00')
        self.assertEqual(context['decision_at'], '2025-03-19T22:30:00+00:00')
        self.assertEqual(context['training_before'], '2025-03-19')

    def test_unordered_inputs_rejected_before_any_prediction_for_all_builders(self):
        with patch.object(models, '_catboost', side_effect=AssertionError('No inference')):
            for build, chosen, history in ((models.build_methods, fixtures(), sample()),
                    (models.build_mixed_methods, mixed_fixtures(), mixed_history())):
                with self.subTest(builder=build.__name__), self.assertRaises(models.SnapshotError):
                    build(history, list(reversed(chosen)), decision_at=DECISION)

    def test_binding_is_stable_for_timezone_spelling_and_changes_with_any_target_identity(self):
        chosen = fixtures()
        context = models.group_context(chosen, DECISION)
        equivalent = deepcopy(chosen)
        for row in equivalent:
            row['timestamp'] += '+00:00'
        self.assertEqual(context, models.group_context(equivalent, DECISION))
        for key, replacement in (('home_id','777'), ('away_id','888'), ('season',2026), ('match_id','9999')):
            altered = deepcopy(chosen); altered[0][key] = replacement
            self.assertNotEqual(context['fixture_fingerprint'], models.group_context(altered, DECISION)['fixture_fingerprint'])

    def test_binding_canonicalizes_integer_ids_padding_names_and_timezones(self):
        chosen = fixtures()
        canonical = models.group_context(chosen, DECISION)
        equivalent = deepcopy(chosen)
        for row in equivalent:
            for key in ('match_id', 'league', 'home_id', 'away_id'):
                row[key] = int(row[key])
            row['home'] = ' '+row['home']+' '
            row['away'] = '\t'+row['away']+' '
            row['timestamp'] += 'Z'
        self.assertEqual(models.group_context(equivalent, DECISION), canonical)
        for row in equivalent:
            for key in ('match_id', 'league', 'home_id', 'away_id'):
                row[key] = '00'+str(row[key])
        self.assertEqual(models.group_context(equivalent, DECISION), canonical)

    def test_bool_float_nondecimal_ids_and_invalid_seasons_are_rejected(self):
        for key in ('match_id', 'league', 'home_id', 'away_id'):
            for value in (True, False, 1.0, '1.0', '١', '0', None):
                chosen = fixtures(); chosen[0][key] = value
                with self.subTest(key=key,value=value), self.assertRaises(models.SnapshotError):
                    models.group_context(chosen, DECISION)
        for value in (True, '2025', 2025.0, 1899, 2101):
            chosen = fixtures(); chosen[0]['season'] = value
            with self.subTest(season=value), self.assertRaises(models.SnapshotError):
                models.group_context(chosen, DECISION)

    def test_unknown_season_does_not_impersonate_a_known_year(self):
        chosen = fixtures()
        for row in chosen:
            row.pop('season')
        absent = models.group_context(chosen, DECISION)
        for row in chosen:
            row['season'] = None
        self.assertEqual(absent, models.group_context(chosen, DECISION))

    def test_all_group_sizes_two_to_twenty_keep_one_position_per_fixture(self):
        history = sample(480)
        for size in range(2,21):
            chosen = [dict(fixtures()[i%4], match_id=str(9000+i),
                           timestamp=f'2025-06-20T12:{i:02d}:00') for i in range(size)]
            value = models.build_methods(history, chosen, decision_at='2025-06-19T09:00:00Z')
            for method in value['methods'].values():
                self.assertEqual(method['distribution']['size'], size)
                self.assertEqual(method['ordered_match_ids'], [row['match_id'] for row in chosen])
                self.assertEqual(method['training_before'], '2025-06-19')
                models.validate_method(method, size)
            self.assertEqual(set(value['methods']), {'frequency','clubs','combined'})

    def test_omitting_decision_keeps_explicit_legacy_temporal_convention(self):
        value = models.build_methods(sample(), fixtures())
        context = models.group_context(fixtures())
        self.assertIsNone(context['decision_at'])
        self.assertEqual(context['training_before'], '2025-03-20')
        for method in value['methods'].values():
            self.assertEqual(method['decision_at'], None)
            self.assertEqual(method['training_before'], '2025-03-20')
        expected = contextual.forecast(repeated_scenarios(sample(),4)['39'], fixtures())
        self.assertEqual(value['methods']['combined']['ranking'][0]['sequence'], list(expected['ranking'][0]['sequence']))
        self.assertAlmostEqual(value['methods']['combined']['ranking'][0]['probability'], expected['ranking'][0]['probability'])

    def test_catboost_does_not_reuse_unbound_selected_estimate_from_another_group(self):
        chosen = fixtures(); history = sample()
        fit_rows = ml._rows(history, '39', models.SOURCE, '2025-03-01')
        fit = fit_sequences([row['sequence'] for row in ml._blocks(fit_rows, len(chosen))], len(chosen))
        positions = [{'1st':.2,'Égal':.3,'2nd':.5} for _ in chosen]
        z = ml._distribution(fit, positions, .5)['z']
        distribution = models._distribution('corrected',4,fit=fit,distributions=positions,alpha=.5,normalization=z)
        ranking = models._method('test',distribution,{})['ranking']
        report = dict(deployment=dict(trained_through='2025-02-28', cutoff_exclusive='2025-03-01',
            fingerprint=ml._fingerprint(fit_rows), generation='frozen', model_sha256='fake'),
            selection=dict(alpha=.5), feature_version='test')
        prediction = dict(eligible=True,version=ml.VERSION,generation='frozen',before='2025-03-20',
            league='39',size=4,source=models.SOURCE,alpha=.5,
            club_estimates=[dict(probabilities=row) for row in positions],normalization=z,ranking=ranking)
        selected = dict(prediction,club_estimates=[dict(probabilities={'1st':.8,'Égal':.1,'2nd':.1}) for _ in chosen])
        with patch.object(ml,'load_report',return_value=report),patch.object(ml,'forecast',return_value=prediction) as fresh:
            payload = models._catboost(history,chosen,models.SOURCE,'39','2025-03-20',selected,'catboost')
        fresh.assert_called_once_with(history,chosen,models.SOURCE)
        self.assertEqual(payload['distribution']['distributions'],positions)


if __name__ == '__main__':
    unittest.main()
