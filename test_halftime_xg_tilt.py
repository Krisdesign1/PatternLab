"""Independent mathematical checks for the one-parameter xG tilt.

These tests use only synthetic arrays.  The reference likelihood and its
derivatives are deliberately computed here, without the production helpers.
"""

import unittest

import numpy as np
from scipy.optimize import minimize_scalar
from scipy.special import logsumexp

from halftime_xg_tilt import fit, indicator, objective, predict


_CLASS_DIRECTION = np.array([1., 0., -1.])


def _reference_probabilities(basep, signal, beta):
    logits = (np.log(np.asarray(basep, dtype=float))
              + beta * np.asarray(signal)[:, None] * _CLASS_DIRECTION)
    return np.exp(logits - logsumexp(logits, axis=1, keepdims=True))


def _objective(basep, signal, labels, beta, penalty=8.):
    logits = np.log(basep) + beta * signal[:, None] * _CLASS_DIRECTION
    logp = logits - logsumexp(logits, axis=1, keepdims=True)
    return float(-logp[np.arange(len(labels)), labels].sum()
                 + .5 * penalty * beta ** 2)


def _gradient(basep, signal, labels, beta, penalty=8.):
    p = _reference_probabilities(basep, signal, beta)
    return float(np.dot(signal, p[:, 0] - p[:, 2]
                        - _CLASS_DIRECTION[labels]) + penalty * beta)


def _hessian(basep, signal, beta, penalty=8.):
    p = _reference_probabilities(basep, signal, beta)
    expected_direction = p[:, 0] - p[:, 2]
    return float(np.dot(signal ** 2,
                        p[:, 0] + p[:, 2] - expected_direction ** 2) + penalty)


def _rich_with_signal(signal):
    """Encode signed signals using nonnegative xG entries in 48 columns."""
    signal = np.asarray(signal, dtype=float)
    rich = np.zeros((len(signal), 48), dtype=float)
    rich[:, 16] = 4. * np.maximum(signal, 0.)
    rich[:, 20] = 4. * np.maximum(-signal, 0.)
    return rich


class IndicatorTests(unittest.TestCase):
    def test_each_of_the_48_columns_has_the_prespecified_coefficient(self):
        expected = np.zeros(48)
        expected[[16, 17, 32, 33]] = .25
        expected[[20, 21, 36, 37]] = -.25
        actual = indicator(np.eye(48))
        self.assertIsInstance(actual, np.ndarray)
        self.assertEqual(actual.shape, (48,))
        np.testing.assert_array_equal(actual, expected)

    def test_signal_orientation_and_unrelated_features_are_ignored(self):
        rich = np.zeros((3, 48))
        rich[0, [16, 17, 32, 33]] = [1., 2., 3., 4.]
        rich[0, [20, 21, 36, 37]] = [.2, .4, .6, .8]
        rich[1] = rich[0]
        rich[1, [16, 17, 32, 33, 20, 21, 36, 37]] = (
            rich[0, [20, 21, 36, 37, 16, 17, 32, 33]])
        rich[2, [16, 17, 32, 33, 20, 21, 36, 37]] = 2.
        np.testing.assert_array_equal(indicator(rich), [2., -2., 0.])

        unrelated = np.ones(48, dtype=bool)
        unrelated[[16, 17, 32, 33, 20, 21, 36, 37]] = False
        rich[:, unrelated] = np.arange(unrelated.sum()) * 12345. - 600.
        np.testing.assert_array_equal(indicator(rich), [2., -2., 0.])
        np.testing.assert_array_equal(indicator(np.zeros((4, 48))), np.zeros(4))

    def test_invalid_rich_matrices_are_rejected(self):
        for rich in (np.zeros(48), np.zeros((2, 47)), np.zeros((2, 49)),
                     np.full((2, 48), np.nan), np.full((2, 48), np.inf)):
            with self.subTest(shape=rich.shape, first=rich.flat[0]):
                with self.assertRaises(ValueError):
                    indicator(rich)


class PredictionTests(unittest.TestCase):
    def setUp(self):
        self.basep = np.array([[.53, .29, .18], [.12, .31, .57],
                               [.21, .52, .27], [.35, .24, .41]])
        self.signal = np.array([-1.5, -.2, .75, 2.])

    def test_beta_zero_is_an_exact_independent_copy(self):
        before = self.basep.copy()
        actual = predict(self.basep, self.signal, 0.)
        np.testing.assert_array_equal(actual, self.basep)
        self.assertFalse(np.shares_memory(actual, self.basep))
        actual[0, 0] = 0.
        np.testing.assert_array_equal(self.basep, before)

    def test_tilt_matches_independent_log_probability_offset(self):
        for beta in (-1., -.35, .25, 1.):
            with self.subTest(beta=beta):
                actual = predict(self.basep, self.signal, beta)
                expected = _reference_probabilities(self.basep, self.signal, beta)
                np.testing.assert_allclose(actual, expected, rtol=2e-14, atol=1e-15)
                np.testing.assert_allclose(actual.sum(axis=1), 1., atol=2e-15)
                self.assertTrue(np.isfinite(actual).all())
                self.assertTrue((actual >= 0.).all())

    def test_positive_signal_favors_first_class_and_preserves_draw_odds_offset(self):
        basep = np.tile([.3, .4, .3], (3, 1))
        actual = predict(basep, np.array([-1., 0., 1.]), .6)
        self.assertGreater(actual[2, 0], basep[2, 0])
        self.assertLess(actual[2, 2], basep[2, 2])
        self.assertGreater(actual[0, 2], basep[0, 2])
        np.testing.assert_allclose(actual[1], basep[1], atol=1e-15)
        np.testing.assert_allclose(
            np.log(actual[:, 0] / actual[:, 2])
            - np.log(basep[:, 0] / basep[:, 2]), [-1.2, 0., 1.2], atol=2e-15)

    def test_swapping_first_and_second_classes_and_negating_signal_is_symmetric(self):
        actual = predict(self.basep, self.signal, .73)
        reversed_prediction = predict(self.basep[:, ::-1], -self.signal, .73)
        np.testing.assert_allclose(actual, reversed_prediction[:, ::-1],
                                   rtol=2e-14, atol=1e-15)

    def test_extreme_finite_offsets_remain_normalized_and_finite(self):
        actual = predict(self.basep, np.array([-100., 100., -100., 100.]), 1.)
        self.assertTrue(np.isfinite(actual).all())
        self.assertTrue((actual >= 0.).all())
        np.testing.assert_allclose(actual.sum(axis=1), 1., atol=1e-14)
        np.testing.assert_array_equal(actual[[0, 2], 2], [1., 1.])
        np.testing.assert_array_equal(actual[[1, 3], 0], [1., 1.])

    def test_invalid_prediction_inputs_are_rejected(self):
        cases = [
            (self.basep[:, :2], self.signal, .2),
            (self.basep.ravel(), self.signal, .2),
            (self.basep, self.signal[:-1], .2),
            (self.basep, self.signal[:, None], .2),
            (self.basep, np.array([0., 1., np.nan, 2.]), .2),
            (self.basep, np.array([0., 1., np.inf, 2.]), .2),
            (self.basep, self.signal, np.nan),
            (self.basep, self.signal, np.inf),
            (self.basep, self.signal, -1.01),
            (self.basep, self.signal, 1.01),
        ]
        for bad_value in (0., -.1, np.nan, np.inf):
            basep = self.basep.copy()
            basep[0, 0] = bad_value
            cases.append((basep, self.signal, .2))
        cases.append((self.basep * .5, self.signal, .2))
        for i, args in enumerate(cases):
            with self.subTest(case=i):
                with self.assertRaises(ValueError):
                    predict(*args)


class FitTests(unittest.TestCase):
    def test_recovers_positive_and_negative_known_synthetic_tilts(self):
        for seed, true_beta in ((713, .65), (914, -.55)):
            with self.subTest(true_beta=true_beta):
                rng = np.random.default_rng(seed)
                n = 25000
                raw_signal = rng.uniform(-1.4, 2., size=n)
                signal = indicator(_rich_with_signal(raw_signal))
                base_logits = np.column_stack((.35 + .1 * signal,
                                               -.2 + .2 * np.cos(signal),
                                               .4 - .13 * signal))
                basep = np.exp(base_logits - logsumexp(base_logits, axis=1,
                                                      keepdims=True))
                truth = _reference_probabilities(basep, signal, true_beta)
                draws = rng.random(n)
                labels = (draws[:, None] > truth.cumsum(axis=1)[:, :2]).sum(axis=1)
                model = fit(basep, signal, labels)
                self.assertIsInstance(model, dict)
                self.assertAlmostEqual(model['beta'], true_beta, delta=.04)
                self.assertLessEqual(_objective(basep, signal, labels, model['beta']),
                                     _objective(basep, signal, labels, 0.) + 1e-8)

    def test_sum_nll_and_fixed_ridge_match_an_independent_scalar_minimum(self):
        basep = np.array([[.55, .25, .2], [.2, .5, .3], [.3, .2, .5],
                          [.4, .35, .25], [.2, .3, .5], [.25, .25, .5],
                          [.45, .3, .25], [.3, .4, .3]])
        signal = np.array([-.7, .3, 1.4, -.2, 1.8, -.8, .6, 1.1])
        labels = np.array([2, 0, 0, 1, 0, 2, 0, 1])
        model = fit(basep, signal, labels)
        beta = model['beta']
        reference = minimize_scalar(
            lambda b: _objective(basep, signal, labels, b),
            bounds=(-1., 1.), method='bounded', options={'xatol': 1e-13})
        self.assertTrue(reference.success)
        self.assertAlmostEqual(beta, reference.x, delta=2e-7)
        self.assertGreater(beta, 0.)
        self.assertLess(beta, 1.)
        self.assertAlmostEqual(_gradient(basep, signal, labels, beta), 0., delta=2e-7)
        self.assertEqual(model['training_matches'], len(labels))
        self.assertEqual(model['penalty'], 8.)
        np.testing.assert_array_equal(model['bounds'], [-1., 1.])
        self.assertAlmostEqual(model['objective'], _objective(basep, signal, labels, beta),
                               delta=1e-12)
        self.assertAlmostEqual(model['zero_objective'], _objective(basep, signal, labels, 0.),
                               delta=1e-12)
        self.assertAlmostEqual(model['gradient'], _gradient(basep, signal, labels, beta),
                               delta=1e-12)
        self.assertAlmostEqual(model['hessian'], _hessian(basep, signal, beta), delta=1e-12)
        self.assertGreaterEqual(model['kkt_residual'], 0.)
        self.assertLessEqual(model['kkt_residual'], 1e-7)

        # A finite difference checks the independently written analytic gradient.
        step = 1e-5
        for probe in (-.6, beta, .7):
            finite_difference = (_objective(basep, signal, labels, probe + step)
                                 - _objective(basep, signal, labels, probe - step)) / (2 * step)
            self.assertAlmostEqual(_gradient(basep, signal, labels, probe),
                                   finite_difference, delta=2e-8)
            value, gradient, hessian = objective(basep, signal, labels, probe)
            self.assertAlmostEqual(value, _objective(basep, signal, labels, probe), delta=1e-12)
            self.assertAlmostEqual(gradient, finite_difference, delta=2e-8)
            self.assertAlmostEqual(hessian, _hessian(basep, signal, probe), delta=1e-12)
            hessian_difference = (_gradient(basep, signal, labels, probe + step)
                                  - _gradient(basep, signal, labels, probe - step)) / (2 * step)
            self.assertAlmostEqual(hessian, hessian_difference, delta=2e-8)
        optimum = _objective(basep, signal, labels, beta)
        self.assertLessEqual(optimum, _objective(basep, signal, labels, 0.) + 1e-10)
        for candidate in np.linspace(-1., 1., 101):
            self.assertLessEqual(optimum, _objective(basep, signal, labels, candidate) + 1e-10)

    def test_fit_is_invariant_to_class_reversal_with_negative_signal(self):
        rng = np.random.default_rng(82)
        basep = rng.dirichlet([2., 3., 4.], size=150)
        signal = rng.uniform(-1.5, 2.2, size=150)
        labels = rng.integers(0, 3, size=150)
        forward = fit(basep, signal, labels)
        reverse = fit(basep[:, ::-1], -signal, 2 - labels)
        self.assertAlmostEqual(forward['beta'], reverse['beta'], delta=2e-12)

    def test_boundary_optima_obey_the_one_sided_kkt_conditions(self):
        basep = np.tile([.25, .5, .25], (100, 1))
        signal = np.ones(100)
        for target, expected, bounds in ((0, 1., (-1., 1.)),
                                         (2, -1., (-1., 1.)),
                                         (0, .35, (-.25, .35)),
                                         (2, -.25, (-.25, .35))):
            with self.subTest(target=target, bounds=bounds):
                labels = np.full(100, target, dtype=int)
                model = fit(basep, signal, labels, bounds=bounds)
                beta = model['beta']
                self.assertEqual(beta, expected)
                gradient = _gradient(basep, signal, labels, beta)
                if beta == bounds[1]:
                    self.assertLessEqual(gradient, 0.)
                else:
                    self.assertGreaterEqual(gradient, 0.)
                self.assertEqual(model['kkt_residual'], 0.)
                self.assertAlmostEqual(model['gradient'], gradient, delta=1e-12)
                self.assertLessEqual(_objective(basep, signal, labels, beta),
                                     _objective(basep, signal, labels, 0.) + 1e-10)

    def test_zero_signal_returns_exact_zero_and_leaves_base_probabilities_unchanged(self):
        basep = np.array([[.1, .2, .7], [.6, .3, .1], [.3, .4, .3]])
        signal = indicator(np.zeros((3, 48)))
        labels = np.array([0, 2, 1])
        model = fit(basep, signal, labels)
        self.assertEqual(model['beta'], 0.)
        self.assertEqual(model['gradient'], 0.)
        self.assertEqual(model['hessian'], 8.)
        self.assertEqual(model['kkt_residual'], 0.)
        np.testing.assert_array_equal(predict(basep, signal, model['beta']), basep)
        self.assertEqual(_objective(basep, signal, labels, model['beta']),
                         _objective(basep, signal, labels, 0.))

    def test_invalid_training_labels_and_shapes_are_rejected(self):
        basep = np.tile([.3, .4, .3], (3, 1))
        signal = np.array([-.2, .4, 1.])
        for labels in ([0, 1], [[0], [1], [2]], [0, 1, 3], [0, -1, 2],
                       [0, .5, 2], [0., 1., 2.], [0, np.nan, 2], [0, np.inf, 2],
                       ['0', '1', '2']):
            with self.subTest(labels=labels):
                with self.assertRaises(ValueError):
                    fit(basep, signal, labels)
        for invalid_signal in (signal[:-1], signal[:, None],
                               [0., np.nan, 1.], [0., np.inf, 1.]):
            with self.subTest(signal=np.asarray(invalid_signal).shape):
                with self.assertRaises(ValueError):
                    fit(basep, invalid_signal, [0, 1, 2])

    def test_invalid_penalty_and_bounds_are_rejected(self):
        basep = np.tile([.3, .4, .3], (3, 1))
        signal = np.array([-.2, .4, 1.])
        labels = np.array([0, 1, 2])
        for penalty in (0., -1., np.nan, np.inf):
            with self.subTest(penalty=penalty):
                with self.assertRaises(ValueError):
                    fit(basep, signal, labels, penalty=penalty)
        for bounds in ((1., -1.), (0., 0.), (-np.inf, 1.), (-1., np.inf),
                       (np.nan, 1.), (.1, .5), (-.5, -.1), (-1.01, 1.), (-1., 1.01)):
            with self.subTest(bounds=bounds):
                with self.assertRaises(ValueError):
                    fit(basep, signal, labels, bounds=bounds)


if __name__ == '__main__':
    unittest.main()
