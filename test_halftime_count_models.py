"""Mathematical and fitting checks for the isolated count-law experiment."""
import json
import unittest

import numpy as np
from scipy.stats import binom, skellam

from halftime_count_models import (_power_binomial_weights, class_probabilities, fit_count,
                                   fit_dispersion, predict_means)


class CountLawTests(unittest.TestCase):
    def test_poisson_integral_matches_independent_difference_law(self):
        means = np.array([[.15, 4.], [1.2, 1.8], [5., .4], [20., 20.]])
        actual = class_probabilities(means)
        expected = np.column_stack((skellam.sf(0, means[:, 0], means[:, 1]),
                                    skellam.pmf(0, means[:, 0], means[:, 1]),
                                    skellam.cdf(-1, means[:, 0], means[:, 1])))
        np.testing.assert_allclose(actual, expected, atol=3e-12, rtol=0)
        self.assertGreater(actual[0, 2], actual[0, 0])
        self.assertGreater(actual[2, 0], actual[2, 2])

    def test_both_laws_normalize_and_reverse_symmetrically(self):
        means = np.array([[.7, 2.1], [1.4, 1.4], [6., .25]])
        dispersion = dict(total_concentration=2., allocation_power=1.5)
        for shape in ('poisson', 'compound'):
            p = class_probabilities(means, shape, dispersion)
            reverse = class_probabilities(means[:, ::-1], shape, dispersion)
            np.testing.assert_allclose(p.sum(axis=1), 1., atol=1e-13)
            np.testing.assert_allclose(p, reverse[:, ::-1], atol=2e-12)
            self.assertTrue((p >= 0).all())

    def test_zero_and_one_sided_means_have_exact_orientation(self):
        for shape in ('poisson', 'compound'):
            p = class_probabilities([[0, 0], [0, 2], [2, 0]], shape,
                                    dict(total_concentration=3., allocation_power=2.))
            np.testing.assert_array_equal(p[0], [0, 1, 0])
            self.assertEqual(p[1, 0], 0.)
            self.assertEqual(p[2, 2], 0.)
            np.testing.assert_allclose(p[1], p[2, ::-1], atol=1e-13)

    def test_compound_near_infinite_concentration_approaches_poisson(self):
        means = [[.6, 2.], [1.1, 1.1]]
        p = class_probabilities(means, 'compound',
                                dict(total_concentration=1e6, allocation_power=1.))
        np.testing.assert_allclose(p, class_probabilities(means), atol=2e-6)

    def test_power_one_is_binomial_and_power_controls_symmetric_variance(self):
        allocation = np.arange(7)
        ordinary = _power_binomial_weights([6], .5, 1.)[0]
        np.testing.assert_allclose(ordinary, binom.pmf(allocation, 6, .5), atol=1e-15)
        narrow = _power_binomial_weights([6], .5, 2.)[0]
        wide = _power_binomial_weights([6], .5, .5)[0]
        variance = lambda weights: np.dot((allocation - 3.)**2, weights)
        self.assertLess(variance(narrow), variance(ordinary))
        self.assertGreater(variance(wide), variance(ordinary))

    def test_invalid_targets_and_unknown_shapes_are_rejected(self):
        with self.assertRaises(ValueError):
            class_probabilities([[1, -1]])
        with self.assertRaises(ValueError):
            class_probabilities([[1, 1]], 'unregistered')
        with self.assertRaises(ValueError):
            fit_count([[1]], [[.5, 1]], ['39'])


class CountFitTests(unittest.TestCase):
    def test_synthetic_count_signal_and_serialized_round_trip(self):
        rng = np.random.default_rng(306)
        n = 1500
        x = np.column_stack((rng.normal(size=n), np.full(n, 17.)))
        leagues = np.where(np.arange(n) % 2, '39', '61')
        shift = np.where(leagues == '39', .15, -.15)
        means = np.column_stack((np.exp(.2 + .5*x[:, 0] + shift),
                                 np.exp(.3 - .35*x[:, 0] - shift)))
        y = rng.poisson(means)
        model = fit_count(x, y, leagues, penalty=5.)
        restored = json.loads(json.dumps(model, allow_nan=False))
        probe = np.array([[-1., 17.], [1., 17.]])
        predicted = predict_means(restored, probe, ['39', '39'])
        self.assertGreater(predicted[1, 0], predicted[0, 0] * 2.)
        self.assertLess(predicted[1, 1], predicted[0, 1] * .7)
        np.testing.assert_array_equal(predicted, predict_means(model, probe, ['39', '39']))
        np.testing.assert_allclose(model['center'], x.mean(axis=0))
        self.assertEqual(model['scale'][1], 1.)
        # A count-likelihood fit must improve actual held-out count likelihood.
        holdout = np.column_stack((rng.normal(size=800), np.full(800, 17.)))
        truth = np.column_stack((np.exp(.35 + .5*holdout[:, 0]),
                                 np.exp(.15 - .35*holdout[:, 0])))
        observed = rng.poisson(truth)
        estimate = predict_means(model, holdout, ['39']*len(holdout))
        fitted_nll = (estimate - observed*np.log(estimate)).mean()
        constant_nll = (y.mean(axis=0) - observed*np.log(y.mean(axis=0))).mean()
        self.assertLess(fitted_nll, constant_nll - .08)

    def test_unknown_league_fails_and_predictions_obey_bounds(self):
        model = fit_count([[-1], [0], [1], [2]], [[0, 3], [1, 2], [2, 1], [3, 0]],
                          ['39']*4)
        with self.assertRaisesRegex(ValueError, 'unknown league'):
            predict_means(model, [[0]], ['999'])
        predicted = predict_means(model, [[-1e6], [1e6]], ['39', '39'])
        self.assertTrue((predicted >= .01 - 1e-14).all())
        self.assertTrue((predicted <= 20. + 1e-12).all())

    def test_dispersion_recovers_synthetic_overdispersion(self):
        rng = np.random.default_rng(530)
        n = 5000
        total = rng.negative_binomial(2.5, 2.5/(2.5+3.), size=n)
        first = np.empty(n, dtype=int)
        for i, goals in enumerate(total):
            weights = _power_binomial_weights([goals], .4, 1.7)[0]
            first[i] = rng.choice(np.arange(goals+1), p=weights)
        y = np.column_stack((first, total-first))
        fitted = fit_dispersion(y, np.tile([1.2, 1.8], (n, 1)))
        self.assertGreater(fitted['total_concentration'], 1.7)
        self.assertLess(fitted['total_concentration'], 3.5)
        self.assertGreater(fitted['allocation_power'], 1.4)
        self.assertLess(fitted['allocation_power'], 2.1)
        json.dumps(fitted, allow_nan=False)


if __name__ == '__main__':
    unittest.main()
