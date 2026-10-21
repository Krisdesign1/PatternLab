import copy
from itertools import product
import json
from math import fsum, prod
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import catboost_scenarios as ml
import contextual_scenarios as contextual
from league_model import VALUES
from league_patterns import repeated_scenarios
import prospective_models as models
from test_catboost_scenarios import sample, fixtures


def context_distribution(size=4, eta=.35):
    return dict(schema=models.DISTRIBUTION_VERSION, type='context_mixture', size=size,
                baseline=[dict(zip(VALUES, (.6, .25, .15))) for _ in range(size)], eta=eta,
                local_mass=[dict(sequence=['2nd']*size, probability=.7, observed=7),
                            dict(sequence=['Égal']*size, probability=.3, observed=3)])


def context_method(size=4):
    return models._method('context-neighbors-v1', context_distribution(size),
        dict(source='API-Football', league='39', training_before='2025-03-20',
             history_fingerprint='frozen-history', selection_fingerprint='frozen-selection',
             selection_through='2025-02-28'), generation='context-generation',
        support=dict(neighbors=10, ess=8.4, eligible=True), parameters=dict(k=25), neighbors=[])


class FrozenMethodTests(unittest.TestCase):
    def build(self, rows=None, chosen=None):
        with patch.object(ml, 'load_report', return_value=None), patch.object(models, '_context_neighbors',
                side_effect=models.SnapshotError('Configuration par contexte absente.')):
            return models.build_methods(rows or sample(), chosen or fixtures())

    def test_stats_snapshots_match_existing_model_and_normalize_exhaustively(self):
        rows, chosen = sample(), fixtures()
        frozen = self.build(rows, chosen)
        self.assertEqual(set(frozen['methods']), {'frequency', 'clubs', 'combined'})
        self.assertIn('catboost', frozen['unavailable'])
        self.assertIn('context_neighbors', frozen['unavailable'])
        result = repeated_scenarios(rows, 4)['39']
        expected = contextual.forecast(result, chosen)
        self.assertEqual([list(r['sequence']) for r in expected['ranking']],
                         [r['sequence'] for r in frozen['methods']['combined']['ranking']])
        for name, payload in frozen['methods'].items():
            values = [models.probability_for_sequence(payload, seq) for seq in product(VALUES, repeat=4)]
            self.assertAlmostEqual(sum(values), 1)
            models.validate_method(json.loads(json.dumps(payload)), 4)
        self.assertAlmostEqual(expected['ranking'][0]['probability'], frozen['methods']['combined']['ranking'][0]['probability'])

    def test_json_distribution_does_not_recompute_after_history_changes(self):
        rows = sample()
        frozen = json.loads(json.dumps(self.build(rows)))
        before = models.probability_for_sequence(frozen['methods']['combined'], ['2nd']*4)
        for row in rows:
            row.update(ht='0-0', ft='0-0', value='Égal')
        with patch.object(ml, 'forecast', side_effect=AssertionError('Never infer at settlement')):
            self.assertEqual(models.probability_for_sequence(frozen['methods']['combined'], ['2nd']*4), before)

    def test_wrong_sources_and_competitions_cannot_enter_snapshots(self):
        chosen = fixtures()
        chosen[-1]['league'] = '61'
        with self.assertRaises(ValueError): self.build(chosen=chosen)
        with self.assertRaises(ValueError): models.build_methods(sample(), fixtures(), 'football-data.org')
        other = [dict(r, league='61', ht=None, value='invalid') for r in sample()]
        self.assertEqual(self.build(sample()), self.build(sample()+other))

    def test_tampered_ranking_or_normalizer_is_rejected(self):
        payload = self.build()['methods']['combined']
        for altered in ('ranking', 'normalizer'):
            value = copy.deepcopy(payload)
            if altered == 'ranking': value['ranking'][0]['probability'] *= .5
            else: value['distribution']['normalization'] *= 2
            with self.assertRaises(ValueError): models.validate_method(value, 4)

    def test_twenty_match_distribution_remains_compact_and_exact(self):
        chosen = fixtures()
        chosen = [dict(chosen[i%4], match_id=str(9000+i), timestamp=f'2026-03-20T12:{i:02d}:00') for i in range(20)]
        frozen = self.build(sample(480), chosen)
        self.assertEqual(len(frozen['methods']['combined']['ranking']), 3)
        self.assertEqual(frozen['methods']['combined']['distribution']['size'], 20)
        self.assertLess(len(json.dumps(frozen)), 100000)

    def test_context_mixture_full_mass_and_exact_top_three(self):
        for size in (2, 4):
            for eta in (0, .15, .5):
                distribution = context_distribution(size, eta)
                payload = models._method('context-neighbors-v1', distribution, {})
                all_sequences = list(product(VALUES, repeat=size))
                local = {tuple(row['sequence']): row['probability'] for row in distribution['local_mass']}
                expected = {seq: (1-eta)*prod(p[label] for p,label in zip(distribution['baseline'], seq))
                            + eta*local.get(seq, 0) for seq in all_sequences}
                for sequence, probability in expected.items():
                    self.assertAlmostEqual(models.probability(distribution, sequence), probability)
                self.assertAlmostEqual(fsum(models.probability(distribution, s) for s in all_sequences), 1)
                self.assertEqual([tuple(row['sequence']) for row in payload['ranking']],
                                 sorted(expected, key=lambda s: (-expected[s], s))[:3])
                models.validate_method(json.loads(json.dumps(payload)), size)

    def test_context_twenty_match_ranking_and_empty_support_fallback(self):
        distribution = context_distribution(20, .5)
        distribution['local_mass'] = [dict(sequence=['2nd']*20, probability=1)]
        ranking = models._method('context-neighbors-v1', distribution, {})['ranking']
        self.assertEqual([r['sequence'] for r in ranking],
                         [['2nd']*20, ['1st']*20, ['1st']*19+['Égal']])
        distribution.update(eta=0, local_mass=[])
        self.assertAlmostEqual(models.probability(distribution, ['1st']*20), .6**20)
        self.assertEqual(len(models._method('context-neighbors-v1', distribution, {})['ranking']), 3)

    def test_context_rejects_damaged_masses_and_unnormalized_baseline(self):
        for issue in ('eta', 'nan', 'negative', 'missing', 'sum', 'duplicate', 'baseline', 'observed'):
            d = context_distribution()
            if issue == 'eta': d['eta'] = .50001
            elif issue == 'nan': d['local_mass'][0]['probability'] = float('nan')
            elif issue == 'negative': d['local_mass'][0]['probability'] = -.1
            elif issue == 'missing': d['local_mass'] = []
            elif issue == 'sum': d['local_mass'][0]['probability'] = .6
            elif issue == 'duplicate': d['local_mass'][1]['sequence'] = d['local_mass'][0]['sequence']
            elif issue == 'baseline': d['baseline'][0]['1st'] = .5
            else: d['local_mass'][0]['observed'] = True
            with self.subTest(issue=issue), self.assertRaises(models.SnapshotError):
                models.probability(d, ['1st']*4)
        payload = context_method()
        payload['ranking'][0]['probability'] *= .9
        with self.assertRaises(models.SnapshotError): models.validate_method(payload, 4)

    def test_build_adds_context_with_frozen_provenance_for_any_selected_engine(self):
        import contextual_validation as validation
        payload = context_method()
        original = copy.deepcopy(payload['distribution'])
        original.update(schema='context-neighbors-distribution-v1', source='API-Football',
                        provider='api-football', league='39', before='2025-03-20')
        fingerprint = ml._fingerprint(ml._rows(sample(), '39', 'API-Football', '2025-03-20'))
        selection_fingerprint = ml._fingerprint(ml._rows(sample(), '39', 'API-Football', '2025-03-01'))
        prediction = dict(eligible=True, version='context-neighbors-v1', source='API-Football',
                          provider='api-football', league='39', before='2025-03-20', size=4, training_groups=40,
                          generation='context-generation', selection_through='2025-02-28',
                          history_fingerprint=fingerprint, selection_fingerprint=selection_fingerprint,
                          fixture_fingerprint='these-ordered-fixtures',
                          ranking=payload['ranking'], distribution=original,
                          support=payload['support'], parameters=payload['parameters'], neighbors=[])
        core = SimpleNamespace(VERSION='context-neighbors-v1', SCHEMA='context-neighbors-distribution-v1',
                               forecast=Mock(return_value=prediction), fixture_fingerprint=Mock(return_value='these-ordered-fixtures'),
                               _params=lambda value:value, DEFAULT_PARAMETERS=payload['parameters'])
        report = dict(source='API-Football', league='39',
                      deployment=dict(source='API-Football', league='39', context_version='context-neighbors-v1',
                                     feature_version=ml._features().FEATURE_VERSION,
                                     generation=prediction['generation'], selection_through='2025-02-28',
                                     selection_fingerprint=selection_fingerprint, parameters={'4':payload['parameters']}))
        with patch.dict(sys.modules, context_neighbors=core), patch.object(ml, 'load_report', return_value=None), \
             patch.object(validation, 'load_report', return_value=report):
            for engine in ('combined', 'catboost', 'context_neighbors'):
                frozen = models.build_methods(sample(), fixtures(), engine=engine)
                saved = frozen['methods']['context_neighbors']
                self.assertEqual(saved['support'], payload['support'])
                self.assertEqual(saved['generation'], 'context-generation')
                self.assertEqual(saved['selection_fingerprint'], selection_fingerprint)
                self.assertEqual(saved['context_history_fingerprint'], fingerprint)
                self.assertEqual(saved['ranking'],
                                 [{'sequence': row['sequence'], 'probability': row['probability']}
                                  for row in prediction['ranking']])
                self.assertNotIn('context_neighbors', frozen['unavailable'])
            core.forecast.assert_called_with(sample(), fixtures(), source='API-Football', limit=3)
            core.forecast.reset_mock()
            frozen = models.build_methods(sample(), fixtures(), estimate=copy.deepcopy(prediction), engine='context_neighbors')
            self.assertEqual(frozen['methods']['context_neighbors']['ranking'], prediction['ranking'])
            core.forecast.assert_not_called()
            for issue in ('generation', 'selection', 'history', 'fixtures', 'parameters'):
                altered = copy.deepcopy(prediction)
                key = {'generation':'generation', 'selection':'selection_fingerprint', 'history':'history_fingerprint',
                       'fixtures':'fixture_fingerprint', 'parameters':'parameters'}[issue]
                altered[key] = 'changed' if issue != 'parameters' else {'k':50}
                rejected = models.build_methods(sample(), fixtures(), estimate=altered, engine='context_neighbors')
                self.assertNotIn('context_neighbors', rejected['methods'])
                self.assertIn('context_neighbors', rejected['unavailable'])
                core.forecast.assert_not_called()
            for issue in ('source', 'period', 'ranking', 'ineligible'):
                altered = copy.deepcopy(prediction)
                if issue == 'source': altered['source'] = 'other'
                elif issue == 'period': altered['selection_through'] = '2025-03-20'
                elif issue == 'ranking': altered['ranking'][0]['probability'] *= .5
                else: altered.update(eligible=False, diagnostic='Historique insuffisant.')
                core.forecast.return_value = altered
                frozen = models.build_methods(sample(), fixtures())
                self.assertNotIn('context_neighbors', frozen['methods'])
                self.assertIn('context_neighbors', frozen['unavailable'])

    def test_actual_context_forecast_roundtrip_reuses_selected_prediction_and_guards_changes(self):
        import context_neighbors as neighbors
        import contextual_validation as validation
        rows, chosen = sample(), fixtures()
        cutoff = '2025-02-28'
        config = dict(source='API-Football', league='39', context_version=neighbors.VERSION,
                      feature_version=neighbors.features.FEATURE_VERSION,
                      selection_through=cutoff, generation='synthetic-context-generation',
                      selection_fingerprint=ml._fingerprint(ml._rows(rows, '39', 'API-Football', '2025-03-01')),
                      parameters={'4':dict(k=20, half_life_days=None, mix=.25)})
        with patch.object(validation, 'load_report', return_value=dict(source='API-Football', league='39', deployment=config)), \
             patch.object(ml, 'load_report', return_value=None):
            prediction = neighbors.forecast(rows, chosen)
            self.assertTrue(prediction['eligible'])
            with patch.object(neighbors, 'forecast', side_effect=AssertionError('Selected prediction must not be recomputed')):
                frozen = models.build_methods(rows, chosen, estimate=prediction, engine='context_neighbors')
                saved = frozen['methods']['context_neighbors']
                self.assertEqual(saved['ranking'],
                                 [{'sequence': row['sequence'], 'probability': row['probability']}
                                  for row in prediction['ranking']])
                self.assertEqual(saved['fixture_fingerprint'], neighbors.fixture_fingerprint(chosen))
                self.assertEqual(saved['support'], prediction['support'])
                self.assertAlmostEqual(fsum(models.probability_for_sequence(saved, seq)
                                           for seq in product(VALUES, repeat=4)), 1)
                for issue in ('fixtures', 'history', 'configuration'):
                    changed_rows, changed_fixtures = copy.deepcopy(rows), copy.deepcopy(chosen)
                    changed_config = copy.deepcopy(config)
                    if issue == 'fixtures': changed_fixtures[0]['timestamp'] = '2025-03-20T11:59:00'
                    elif issue == 'history': changed_rows[-1].update(ht='0-0', ft='0-0', value='Égal')
                    else: changed_config['generation'] = 'changed-config'
                    with patch.object(validation, 'load_report', return_value=dict(source='API-Football', league='39', deployment=changed_config)):
                        result = models.build_methods(changed_rows, changed_fixtures, estimate=prediction,
                                                      engine='context_neighbors')
                    self.assertNotIn('context_neighbors', result['methods'])
                    self.assertIn('context_neighbors', result['unavailable'])


if __name__ == '__main__': unittest.main()
