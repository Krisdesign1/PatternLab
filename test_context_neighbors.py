"""Causality, exact mixture probabilities, and saved-context safeguards."""
import copy
from datetime import date, timedelta
from itertools import product
import json
from math import prod
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import api_football
import catboost_scenarios as ml
import context_neighbors as neighbors
from league_model import VALUES
from match_predictor import estimate
import scenario_features as features
import seasonal_validation as seasonal
from test_seasonal_validation import sample


def fixtures(size=4, day='2027-08-01'):
    return [dict(match_id=str(900000+i), league='39', provider='api-football',
        home='Home'+str(i%4), away='Away'+str(i%5), home_id=str(100+i%4), away_id=str(200+i%5),
        season=int(day[:4]), timestamp=f'{day}T12:{i:02}:00') for i in range(size)]


def context(rows, chosen):
    before = min(ml._utc_stamp(row['timestamp'])[:10] for row in chosen)
    return dict(block=dict(matches=chosen), before=before,
        features=[features.feature_for_fixture(rows, row, before=before) for row in chosen],
        clubs=[estimate(rows, str(row['league']), row['home'], row['away'], before)['probabilities'] for row in chosen])


class ContextNeighborTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = sample(60)
        cls.catalogs, cls.targets = {}, {}
        for size in (2, 4, 20):
            groups = seasonal._groups(cls.rows, cls.rows, size)
            contexts = seasonal._contexts(cls.rows, {size: groups})[size]
            cls.catalogs[size] = neighbors.fit_catalog(contexts)
            cls.targets[size] = context(cls.rows, fixtures(size))

    def parameters(self, **kwargs):
        return dict(k=20, half_life_days=None, mix=.5, **kwargs)

    def test_full_distribution_and_top_three_match_brute_force_for_two_and_four(self):
        for size in (2, 4):
            for mix in neighbors.MIX_VALUES:
                with self.subTest(size=size, mix=mix):
                    model = neighbors.distribution(self.catalogs[size], self.targets[size],
                        dict(k=20, half_life_days=365, mix=mix))
                    actual = {seq: neighbors.probability_for(model, seq) for seq in product(VALUES, repeat=size)}
                    self.assertAlmostEqual(sum(actual.values()), 1)
                    expected = sorted(actual, key=lambda seq: (-actual[seq], seq))[:3]
                    self.assertEqual([tuple(r['sequence']) for r in neighbors.rank(model)], expected)
                    frozen = json.loads(json.dumps(model, allow_nan=False))
                    self.assertEqual(model, frozen)
                    self.assertAlmostEqual(sum(n['weight'] for n in model['neighbors']), 1)

    def test_twenty_match_exact_top_three_without_enumeration(self):
        model = neighbors.distribution(self.catalogs[20], self.targets[20], self.parameters())
        ranked = neighbors.rank(model)
        self.assertEqual(len(ranked), 3)
        self.assertTrue(model['support']['eligible'])
        local = {tuple(r['sequence']) for r in model['local_mass']}
        candidates = local | {tuple(r['sequence']) for r in neighbors.top_independent(model['baseline'], 3)}
        expected = sorted(candidates, key=lambda seq: (-neighbors.probability_for(model, seq), seq))[:3]
        self.assertEqual([tuple(r['sequence']) for r in ranked], expected)
        self.assertLess(len(json.dumps(model)), 100000)

    def test_minimum_neighbor_count_and_effective_support_disable_correction(self):
        empty = neighbors.distribution([], self.targets[4], self.parameters())
        sparse = neighbors.distribution(self.catalogs[4][:11], self.targets[4], self.parameters())
        for model in (empty, sparse):
            self.assertFalse(model['support']['eligible'])
            self.assertEqual(model['eta'], 0)
            for row in model['ranking']:
                self.assertAlmostEqual(row['probability'], prod(p[v] for p,v in zip(model['baseline'], row['sequence'])))
        entries = copy.deepcopy(self.catalogs[4][:20])
        for i, entry in enumerate(entries):
            # One recent and nineteen ancient otherwise comparable groups.
            entry['end_day'] = '2027-07-31' if i == 0 else '1901-01-01'
            entry['first_day'] = entry['end_day']
        concentrated = neighbors.distribution(entries, self.targets[4], dict(k=20, half_life_days=365, mix=.5))
        self.assertEqual(concentrated['support']['neighbors'], 20)
        self.assertLess(concentrated['support']['ess'], 8)
        self.assertEqual(concentrated['eta'], 0)

    def test_eta_uses_effective_support_and_recency_uses_only_past_end_days(self):
        model = neighbors.distribution(self.catalogs[4], self.targets[4], self.parameters())
        support = model['support']
        self.assertAlmostEqual(model['eta'], .5*support['ess']/(support['ess']+20))
        recent = neighbors.distribution(self.catalogs[4], self.targets[4], dict(k=20, half_life_days=365, mix=.5))
        for row in recent['neighbors']:
            self.assertGreater(row['age_days'], 0)
            self.assertAlmostEqual(row['raw_weight'], 2**(-row['age_days']/365)/(1+row['distance']))
        future = copy.deepcopy(self.catalogs[4][0])
        future['first_day'] = future['end_day'] = '2027-08-01'
        changed = neighbors.distribution(list(self.catalogs[4])+[future], self.targets[4], self.parameters())
        self.assertEqual(changed, model)

    def test_target_results_never_enter_descriptors_or_neighbor_selection(self):
        target = copy.deepcopy(self.targets[4])
        original = neighbors.distribution(self.catalogs[4], target, self.parameters())
        target['block']['sequence'] = ['invalid target outcome']*4
        for row in target['block']['matches']:
            row.update(ht=None, ft='unreadable target result', value='invalid')
        self.assertEqual(neighbors.distribution(self.catalogs[4], target, self.parameters()), original)
        self.assertTrue(all(0 <= x <= 1 for p in neighbors.describe_group(target)['positions'] for x in p['numeric']))

    def test_whole_group_descriptors_exclude_all_its_first_day_and_later_results(self):
        groups = seasonal._groups(self.rows, [r for r in self.rows if r['season'] == 2024], 6)
        contexts = seasonal._contexts(self.rows, {6: groups})[6]
        target = next(g for g in contexts if len({r['timestamp'][:10] for r in g['block']['matches']}) > 1)
        changed = copy.deepcopy(self.rows)
        for row in changed:
            if row['timestamp'][:10] >= target['before']:
                row.update(ht='7-0', ft='7-0', value='1st')
        altered = seasonal._contexts(changed, {6: [target['block']]})[6][0]
        self.assertEqual(neighbors.describe_group(target), neighbors.describe_group(altered))
        self.assertEqual(target['clubs'], altered['clubs'])

    def test_exact_lexical_ties_and_same_role_club_identity_bonus_are_deterministic(self):
        target = copy.deepcopy(self.targets[2])
        target['clubs'] = [dict.fromkeys(VALUES, 1/3)]*2
        model = neighbors.distribution([], target, dict(k=20, half_life_days=None, mix=0))
        self.assertEqual([tuple(row['sequence']) for row in neighbors.rank(model)], sorted(product(VALUES, repeat=2))[:3])
        descriptor = neighbors.describe_group(target)
        other = copy.deepcopy(descriptor)
        for row in other['positions']:
            row['home_id'], row['away_id'] = row['away_id'], row['home_id']
        self.assertEqual(neighbors._distance(descriptor, descriptor), 0)
        self.assertAlmostEqual(neighbors._distance(descriptor, other), neighbors.IDENTITY_PENALTY)

    def test_wrong_source_competition_identity_order_and_same_target_members_are_refused_or_excluded(self):
        for field, value in (('provider','football-data.org'), ('league','61'), ('home_id','invalid')):
            target = copy.deepcopy(self.targets[4])
            target['block']['matches'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                neighbors.distribution(self.catalogs[4], target, self.parameters())
        reversed_context = copy.deepcopy(self.targets[4])
        reversed_context['block']['matches'].reverse()
        with self.assertRaises(neighbors.ContextError): neighbors.describe_group(reversed_context)
        entry = copy.deepcopy(self.catalogs[4][0])
        entry['match_ids'][0] = self.targets[4]['block']['matches'][0]['match_id']
        self.assertEqual(neighbors.distribution([entry], self.targets[4], self.parameters())['support']['neighbors'], 0)

    def test_distance_cache_reuses_neighborhood_across_grid_without_changing_public_catalog(self):
        catalog = neighbors.fit_catalog(seasonal._contexts(self.rows,
            {4: seasonal._groups(self.rows, self.rows, 4)})[4])
        before = copy.deepcopy(list(catalog))
        with patch.object(neighbors, '_distance', wraps=neighbors._distance) as distance:
            neighbors.distribution(catalog, self.targets[4], self.parameters())
            n = distance.call_count
            neighbors.distribution(catalog, self.targets[4], dict(k=50, half_life_days=365, mix=.1))
            self.assertEqual(distance.call_count, n)
        self.assertEqual(list(catalog), before)

    def test_damaged_frozen_distribution_and_invalid_parameters_are_rejected(self):
        model = neighbors.distribution(self.catalogs[4], self.targets[4], self.parameters())
        for kind in ('baseline', 'local_mass', 'eta'):
            changed = copy.deepcopy(model)
            if kind == 'baseline': changed[kind][0]['1st'] = -1
            elif kind == 'local_mass': changed[kind][0]['probability'] *= .5
            else: changed[kind] = float('nan')
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                neighbors.probability_for(changed, ['1st']*4)
        for parameters in (dict(k=True, half_life_days=None, mix=.1), dict(k=20, half_life_days=30, mix=.1),
                           dict(k=20, half_life_days=365, mix=.75)):
            with self.assertRaises(neighbors.ContextError):
                neighbors.distribution(self.catalogs[4], self.targets[4], parameters)


class SavedContextForecastTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = sample(60)
        selection_through = '2025-08-31'
        selected = ml._rows(cls.rows, '39', 'API-Football', '2025-09-01')
        cls.report = dict(source='API-Football', league='39', descriptor_version=neighbors.DESCRIPTOR_VERSION,
            deployment=dict(source='API-Football', league='39', context_version=neighbors.VERSION,
                feature_version=features.FEATURE_VERSION,
                selection_through=selection_through, selection_fingerprint=ml._fingerprint(selected),
                generation='a'*64, parameters={'4': dict(k=20, half_life_days=None, mix=.25)}))

    def forecast(self, rows=None, chosen=None, report=None, **kwargs):
        module = SimpleNamespace(load_report=lambda league: copy.deepcopy(report or self.report))
        with patch.dict(sys.modules, {'contextual_validation': module}), \
             patch.object(api_football, 'ApiFootballClient', side_effect=AssertionError('No network')), \
             patch.object(ml, '_publish', side_effect=AssertionError('No model publication')):
            return neighbors.forecast(rows or self.rows, chosen or fixtures(), **kwargs)

    def test_forecast_uses_saved_validated_configuration_and_is_frozen_json(self):
        result = self.forecast()
        self.assertTrue(result['eligible'])
        self.assertEqual(result['generation'], 'a'*64)
        self.assertEqual(result['parameters']['mix'], .25)
        self.assertEqual(result['ranking'], neighbors.rank(result['distribution']))
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_corrected_selection_history_requires_reevaluation_but_new_past_results_are_allowed(self):
        rows = copy.deepcopy(self.rows)
        rows[0].update(ht='0-0', ft='0-0', value='Égal')
        with self.assertRaisesRegex(neighbors.ContextError, 'ont changé'): self.forecast(rows)
        rows = copy.deepcopy(self.rows)
        rows[-1].update(ht='0-0', ft='0-0', value='Égal')
        self.assertTrue(self.forecast(rows)['eligible'])

    def test_periods_used_for_selection_are_blocked_and_nonvalidated_sizes_have_no_mixture(self):
        with self.assertRaisesRegex(neighbors.ContextError, 'validation'):
            self.forecast(chosen=fixtures(day='2025-08-31'))
        result = self.forecast(chosen=fixtures(3))
        self.assertEqual(result['parameters']['mix'], 0)
        self.assertFalse(result['validated_size'])
        self.assertTrue(any('taille' in text for text in result['diagnostics']))
        with self.assertRaises(neighbors.ContextError): self.forecast(source='football-data.org')

    def test_changed_versions_are_rejected_and_fingerprint_binds_ordered_fixtures(self):
        for field in ('context_version', 'feature_version'):
            report = copy.deepcopy(self.report)
            report['deployment'][field] = 'older-version'
            with self.subTest(field=field), self.assertRaisesRegex(neighbors.ContextError, 'version'):
                self.forecast(report=report)
        chosen = fixtures()
        prediction = self.forecast(chosen=chosen)
        self.assertEqual(prediction['fixture_fingerprint'], neighbors.fixture_fingerprint(chosen))
        changed = copy.deepcopy(chosen)
        changed[-1]['timestamp'] = changed[-1]['timestamp'].replace('12:', '13:')
        self.assertNotEqual(prediction['fixture_fingerprint'], neighbors.fixture_fingerprint(changed))

    def test_target_future_and_other_competition_outcomes_are_not_read(self):
        original = self.forecast()
        extras = [dict(self.rows[0], match_id='999999', timestamp='2027-08-01T11:00:00', ht=None, ft=None, value='invalid'),
                  dict(self.rows[0], match_id='999998', league='61', ht=None, ft=None, value='invalid')]
        chosen = [dict(row, ht=None, ft=None, value='must not be read') for row in fixtures()]
        self.assertEqual(self.forecast(self.rows+extras, chosen), original)


if __name__ == '__main__':
    unittest.main()
