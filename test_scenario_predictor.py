import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from itertools import product
from math import fsum, prod
from league_patterns import repeated_scenarios
from league_model import VALUES
from scenario_predictor import distributions, fit_sequences, predict_league, probability


def sample(n=140):
    return [dict(match_id=str(i+1), league='A', value=('1st','Égal','2nd','2nd')[i%4],
                 timestamp=(datetime(2026,1,1)+timedelta(days=i//4)).isoformat())
            for i in range(n)]


class PredictionTests(unittest.TestCase):
    def test_scoring_requires_all_ordered_legs_and_rejects_extra_or_unknown_results(self):
        fit = fit_sequences([('1st','2nd'), ('2nd','2nd')], 2)
        before = deepcopy(fit)
        for sequence in ([], ['1st'], ['1st','2nd','Égal'], ['1st','other'], '1st', None):
            for baseline in (False, True):
                with self.subTest(sequence=sequence, baseline=baseline), self.assertRaises(ValueError):
                    probability(fit, sequence, baseline=baseline)
        self.assertEqual(fit, before)

    def test_probability_rejects_malformed_fit_and_invalid_position_weights(self):
        fit = fit_sequences([('1st','2nd')], 2)
        invalid = [None, (), [fit[0],fit[1]], (fit[0],True,fit[2]),
                   (fit[0],-1,fit[2]), ([],fit[1],fit[2])]
        for value in (float('nan'), float('inf'), True, -.1, 1.1):
            changed = deepcopy(fit)
            changed[2][0]['1st'] = value
            invalid.append(changed)
        changed = deepcopy(fit); changed[2][0]['1st'] += .1; invalid.append(changed)
        changed = deepcopy(fit); del changed[2][0]['Égal']; invalid.append(changed)
        for value in invalid:
            with self.subTest(fit=value), self.assertRaises(ValueError):
                probability(value, ['1st','2nd'])
        for baseline in (0, 1, 'False', None):
            with self.subTest(baseline=baseline), self.assertRaises(ValueError):
                probability(fit, ['1st','2nd'], baseline=baseline)

    def test_valid_absolute_probabilities_keep_original_formula_for_every_size(self):
        for size in range(2, 21):
            fit = fit_sequences([('1st',)*size]*2+[('2nd',)*size], size)
            sequence = ('1st',)*size
            base = prod(position[label] for position,label in zip(fit[2],sequence))
            self.assertAlmostEqual(probability(fit, sequence, baseline=True), base)
            self.assertAlmostEqual(probability(fit, sequence), (2+10*base)/13)
        for size in range(2, 6):
            fit = fit_sequences([('1st',)*size, ('2nd',)*size], size)
            self.assertAlmostEqual(fsum(probability(fit, sequence) for sequence in product(VALUES,repeat=size)), 1)

    def test_normalized_all_scenarios_with_unseen_positive(self):
        for seqs in ([], [('1st',)*4]*25):
            for dist in distributions(seqs):
                self.assertEqual(len(dist),81)
                self.assertAlmostEqual(sum(dist.values()),1)
                self.assertTrue(all(p>0 for p in dist.values()))

    def test_future_and_same_day_do_not_change_earlier_trials(self):
        original=sample()
        a=predict_league(repeated_scenarios(original)['A'])
        changed=[dict(r, value='Égal') if r['timestamp'] >= '2026-02-01' else r for r in original]
        b=predict_league(repeated_scenarios(changed)['A'])
        self.assertEqual([t for t in a['trials'] if t['day']<'2026-02-01'],
                         [t for t in b['trials'] if t['day']<'2026-02-01'])
        together=sample()
        for r in together:r['timestamp']='2026-01-01T12:00:00'
        self.assertEqual(predict_league(repeated_scenarios(together)['A'])['trials'],[])
        self.assertEqual(a['trials'][0]['training'],20)
        self.assertEqual(len(a['trials']),15)

    def test_small_league_and_other_league_isolation(self):
        rows=sample(12)
        other=[dict(r,league='B',match_id=str(1000+i)) for i,r in enumerate(sample())]
        alone=predict_league(repeated_scenarios(rows)['A'])
        self.assertFalse(alone['eligible'])
        self.assertEqual(alone,predict_league(repeated_scenarios(rows+other)['A']))

    def test_ui_opt_in_and_insufficient_data(self):
        from streamlit.testing.v1 import AppTest
        at=AppTest.from_string('from prediction_panel import render_prediction\n'
            'from test_scenario_predictor import sample\n'
            'from league_patterns import repeated_scenarios\n'
            "render_prediction(repeated_scenarios(sample())['A'])").run()
        self.assertFalse(at.exception)
        at.checkbox(key='show_league_prediction').check().run()
        self.assertFalse(at.exception)
        self.assertTrue(any('15 groupes testés' in x.value for x in at.markdown))
        small=AppTest.from_string('from prediction_panel import render_prediction\n'
            'from test_scenario_predictor import sample\n'
            'from league_patterns import repeated_scenarios\n'
            "render_prediction(repeated_scenarios(sample(12))['A'])").run()
        small.checkbox(key='show_league_prediction').check().run()
        self.assertFalse(small.exception)
        self.assertTrue(any('Historique insuffisant' in x.value for x in small.info))
