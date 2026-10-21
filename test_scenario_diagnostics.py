"""Explanations retain the supplied probabilities and never rebuild old data."""
import copy
import json
import math
import unittest
from unittest.mock import patch

import scenario_diagnostics as diagnostics


P = {'1st': .25, 'Égal': .25, '2nd': .5}
BASE = {'1st': .3, 'Égal': .25, '2nd': .45}


def detail(*, home='Home', away='Away', p=None, baseline=None, personalized=True,
           home_matches=16, away_matches=12):
    return dict(home=home, away=away, probabilities=dict(p or P), baseline=dict(baseline or BASE),
                personalized=personalized,
                home_profile=dict(matches=home_matches, scored=[10, 14], conceded=[8, 10]),
                away_profile=dict(matches=away_matches, scored=[7, 11], conceded=[9, 12]))


def estimate():
    return dict(size=3, version='contextual-scenarios-v1', eligible=True,
                personalized_count=3,
                club_estimates=[detail(home=f'Home {i}', away=f'Away {i}') for i in range(3)],
                ranking=[dict(sequence=['2nd']*3, probability=.11361, club_probability=.108302,
                              frequency_probability=.122824),
                         dict(sequence=['1st', '2nd', '2nd'], probability=.07)])


def frozen_methods():
    rank = [dict(sequence=['2nd']*3, probability=.125),
            dict(sequence=['1st', '2nd', '2nd'], probability=.0625),
            dict(sequence=['2nd', '1st', '2nd'], probability=.0625)]
    return json.loads(json.dumps({
        'clubs': dict(version='old-1', ranking=rank, distribution=dict(
            schema='prospective-distribution-v1', type='independent', size=3, distributions=[P]*3)),
        'combined': dict(version='old-1', ranking=rank, alpha=.5, distribution=dict(
            schema='prospective-distribution-v1', type='corrected', size=3, distributions=[P]*3,
            alpha=.5)),
        'frequency': dict(version='old-1', ranking=rank, distribution=dict(
            schema='prospective-distribution-v1', type='frequency', size=3,
            fit=dict(prior=[P]*3))),
    }))


class ScenarioDiagnosticsTests(unittest.TestCase):
    def test_real_statistical_forecast_contract_and_no_prediction_or_io(self):
        # Exercise the actual current forecast shape on a synthetic history.
        from datetime import datetime, timedelta
        from contextual_scenarios import forecast
        rows = []
        for i in range(60):
            rows.append(dict(match_id=str(i+1), league='39', provider='api-football',
                             home='Home', away='Away', ht='0-0', ft='1-0', value='2nd',
                             timestamp=(datetime(2025, 1, 1)+timedelta(days=i)).isoformat()))
        blocks = [dict(matches=rows[i:i+2], sequence=('2nd', '2nd')) for i in range(0, 60, 2)]
        fixtures = [dict(home='Home', away='Away', league='39', provider='api-football',
                         timestamp=f'2026-01-{i+1:02}T12:00:00', match_id=str(i+1000)) for i in range(2)]
        value = forecast(dict(size=2, matches=rows, blocks=blocks), fixtures)
        self.assertTrue(value['eligible'])
        value['fixtures'] = fixtures
        before = copy.deepcopy(value)
        with patch('builtins.open', side_effect=AssertionError('No reading histories')):
            result = diagnostics.analyze(value)
        self.assertEqual(value, before)
        self.assertEqual(result['leader'], list(value['ranking'][0]['sequence']))
        self.assertEqual(result['probability'], value['ranking'][0]['probability'])
        self.assertEqual(result['rows'][0]['probabilities'], value['club_estimates'][0]['probabilities'])
        self.assertEqual(result['rows'][0]['home'], 'Home')
        self.assertTrue(result['product_baseline'])
        self.assertFalse(result['assumes_independence'])

    def test_repeated_leader_explains_marginals_and_retains_all_other_probability(self):
        value = estimate()
        result = diagnostics.analyze(value)
        self.assertEqual(result['leader'], ['2nd']*3)
        self.assertTrue(result['leader_matches_product'])
        self.assertAlmostEqual(result['remaining_probability'], .88639)
        self.assertEqual(result['personalization']['personalized'], 3)
        self.assertEqual(result['personalization']['share'], 1)
        self.assertEqual(result['rows'][0]['first_choice'], '2nd')
        self.assertAlmostEqual(result['rows'][0]['delta_points']['2nd'], 5)
        self.assertEqual(result['baseline_comparison']['changed_first_choices'], 0)
        text = ' '.join(result['explanation'])
        self.assertIn('même résultat répété', text)
        self.assertIn('ne garantit pas', text)
        self.assertIn('pas une mesure causale', text)

    def test_club_changes_are_quantified_even_when_final_leader_does_not_change(self):
        value = estimate()
        value['club_estimates'][0] = detail(p={'1st': .6, 'Égal': .2, '2nd': .2})
        result = diagnostics.analyze(value)
        self.assertFalse(result['leader_matches_product'])
        self.assertEqual(result['marginal_product_leader'], ['1st', '2nd', '2nd'])
        self.assertEqual(result['baseline_comparison']['changed_first_choices'], 1)
        self.assertAlmostEqual(result['baseline_comparison']['max_abs_delta_points'], 30)
        self.assertIn('diffère', ' '.join(result['explanation']))

    def test_low_club_history_and_catboost_coverage_do_not_claim_tree_uses_only_average(self):
        value = estimate()
        value['alpha'] = 0
        value['club_estimates'][0] = detail(personalized=False, home_matches=2)
        value['personalized_count'] = 2
        result = diagnostics.analyze(value, engine='catboost')
        self.assertTrue(result['assumes_independence'])
        self.assertEqual(result['personalization']['personalized'], 2)
        self.assertTrue(result['rows'][0]['limited_history'])
        text = ' '.join(result['explanation'])
        self.assertIn('moins de trois', text)
        self.assertIn('alpha est nulle', text)
        self.assertIn('ne signifie pas un retour intégral', text)
        self.assertNotIn('utilisent la tendance de la compétition.', text)

    def test_context_mixture_does_not_claim_final_independence(self):
        value = estimate()
        value['eta'] = .2
        value['support'] = dict(neighbors=20, ess=18.5, eligible=True, candidates=80)
        value['distribution'] = dict(schema='context-neighbors-distribution-v1', size=3,
                                     baseline=[P]*3, eta=.2)
        result = diagnostics.analyze(value, engine='context_neighbors')
        self.assertEqual(result['distribution_kind'], 'context_mixture')
        self.assertTrue(result['product_baseline'])
        self.assertFalse(result['assumes_independence'])
        self.assertEqual(result['eta'], .2)
        self.assertEqual(result['support']['ess'], 18.5)
        self.assertIn('pas un simple produit indépendant', ' '.join(result['explanation']))
        value['eta'] = 0
        value['distribution']['eta'] = 0
        value['validated_size'] = False
        value['support']['eligible'] = False
        zero = diagnostics.analyze(value, engine='context_neighbors')
        self.assertTrue(zero['assumes_independence'])
        self.assertIn('Cette taille n’a pas été évaluée', ' '.join(zero['explanation']))

    def test_old_json_uses_frozen_probabilities_and_leaves_unsaved_profiles_unknown(self):
        methods = frozen_methods()
        value = estimate()  # Deliberately different live profiles must be ignored.
        value['fixtures'] = [dict(home=f'Old {i}', away=f'Older {i}') for i in range(3)]
        before = copy.deepcopy((value, methods))
        result = diagnostics.analyze(value, saved_methods=methods)
        self.assertEqual((value, methods), before)
        self.assertTrue(result['frozen'])
        self.assertEqual(result['probability'], .125)
        self.assertEqual(result['rows'][0]['probabilities'], P)
        self.assertEqual(result['rows'][0]['home'], 'Old 0')
        self.assertIsNone(result['rows'][0]['baseline'])
        self.assertIsNone(result['rows'][0]['delta_points'])
        self.assertIsNone(result['rows'][0]['home_profile'])
        self.assertFalse(result['personalization']['known'])
        self.assertIsNone(result['personalization']['share'])
        self.assertIn('league_baseline', result['missing'])
        self.assertEqual(len(result['methods']), 3)
        self.assertIn('validations indépendantes', ' '.join(result['explanation']))
        self.assertIn('aucun profil actuel', ' '.join(result['explanation']))

    def test_frequency_prior_is_not_falsely_presented_as_personalized_club_probabilities(self):
        result = diagnostics.analyze({'size': 3}, engine='frequency', saved_methods=frozen_methods())
        self.assertEqual(result['rows'][0]['probability_source'], 'historical_position_prior')
        self.assertFalse(result['product_baseline'])
        self.assertFalse(result['assumes_independence'])
        self.assertIsNone(result['marginal_product_leader'])
        self.assertIn('ne personnalisent pas', ' '.join(result['explanation']))

    def test_ranking_only_legacy_snapshot_does_not_reconstruct_missing_positions(self):
        old = {'combined': {'ranking': [{'sequence': ['2nd']*3, 'probability': .11}]}}
        result = diagnostics.analyze({}, saved_methods=old)
        self.assertEqual(result['size'], 3)
        self.assertEqual(len(result['rows']), 3)
        self.assertIsNone(result['rows'][0]['probabilities'])
        self.assertIsNone(result['rows'][0]['baseline'])
        self.assertIn('per_fixture_probabilities', result['missing'])
        self.assertIsNone(result['baseline_comparison']['max_abs_delta_points'])

    def test_missing_names_and_unknown_personalization_are_not_fabricated(self):
        value = dict(size=2, ranking=[{'sequence': ['1st', 'Égal'], 'probability': .2}],
                     distribution={'type': 'independent', 'size': 2, 'distributions': [P, P]})
        result = diagnostics.analyze(value, engine='clubs')
        self.assertTrue(result['assumes_independence'])
        self.assertIsNone(result['rows'][0]['home'])
        self.assertFalse(result['personalization']['known'])
        self.assertIsNone(result['rows'][0]['limited_history'])

    def test_ties_preserve_domain_labels_and_deterministic_existing_order(self):
        value = estimate()
        value['club_estimates'][0]['probabilities'] = {'2nd': .4, 'Égal': .2, '1st': .4}
        result = diagnostics.analyze(value)
        self.assertEqual(result['rows'][0]['first_choices'], ['1st', '2nd'])
        self.assertEqual(result['rows'][0]['first_choice'], '1st')
        self.assertEqual(result['leader'], value['ranking'][0]['sequence'])

    def test_maximum_size_keeps_twenty_positions_without_scenario_enumeration(self):
        value = dict(size=20, ranking=[dict(sequence=['2nd']*20, probability=.5**20)],
                     distribution=dict(type='independent', size=20, distributions=[P]*20))
        result = diagnostics.analyze(value, engine='clubs')
        self.assertEqual(len(result['rows']), 20)
        self.assertTrue(result['leader_matches_product'])

    def test_damaged_numbers_labels_indices_and_profiles_are_rejected(self):
        for mutation in (
            lambda value: value['ranking'][0].update(probability=math.nan),
            lambda value: value['ranking'][0].update(probability=True),
            lambda value: value['ranking'][0].update(probability=1.1),
            lambda value: value['ranking'][0].update(sequence=['2nd', '2nd']),
            lambda value: value['ranking'][0].update(sequence=['2nd', 'egal', '2nd']),
            lambda value: value.update(size=True),
            lambda value: value['club_estimates'][0]['probabilities'].update({'2nd': math.inf}),
            lambda value: value['club_estimates'][0]['probabilities'].update({'2nd': .9}),
            lambda value: value['club_estimates'][0]['home_profile'].update(matches=-1),
            lambda value: value['club_estimates'][0]['away_profile'].update(scored=[1, math.nan]),
            lambda value: value['club_estimates'][0].update(personalized='yes'),
            lambda value: value.update(fixtures=[{}]),
            lambda value: value.update(alpha=math.nan),
            lambda value: value.update(eta=.6),
            lambda value: value.update(support={'ess': math.nan}),
        ):
            value = estimate()
            mutation(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                diagnostics.analyze(value)

    def test_no_selected_method_fallback_and_empty_analysis_has_no_fake_probability(self):
        with self.assertRaisesRegex(ValueError, 'absente'):
            diagnostics.analyze({}, engine='catboost', saved_methods=frozen_methods())
        with self.assertRaises(ValueError):
            diagnostics.analyze({}, engine='unknown')
        result = diagnostics.analyze({})
        self.assertIsNone(result['leader'])
        self.assertIsNone(result['probability'])
        self.assertIsNone(result['remaining_probability'])
        self.assertEqual(result['rows'], [])
        self.assertIn('ranking', result['missing'])

    def test_malformed_saved_positions_fail_cleanly_without_loading_anything(self):
        methods = frozen_methods()
        methods['frequency']['distribution']['fit'] = None
        with self.assertRaises(ValueError):
            diagnostics.analyze({'size': 3}, engine='frequency', saved_methods=methods)
        methods = frozen_methods()
        methods['combined']['distribution']['distributions'][0]['2nd'] = math.nan
        with self.assertRaises(ValueError):
            diagnostics.analyze({'size': 3}, saved_methods=methods)
        value = estimate()
        value['ranking'] = [{'sequence': ['2nd']*3, 'probability': .8},
                            {'sequence': ['1st']*3, 'probability': .7}]
        with self.assertRaisesRegex(ValueError, 'somme'):
            diagnostics.analyze(value)


if __name__ == '__main__':
    unittest.main()
