"""Distribution and numerical-contract checks; no data or fitting is needed."""
import unittest

import numpy as np
from scipy.stats import binom, poisson, skellam

from regulation_shape_research import (CLASS_ORDER, MAX_TOTAL, allocation_distribution,
                                       class_distribution, total_distribution)


class TotalShapeTests(unittest.TestCase):
    def test_null_is_poisson_with_explicit_tail(self):
        for mean in (1e-12, .05, 1., 3.7, 40., 100.):
            law = total_distribution(mean)
            expected = poisson.pmf(np.arange(len(law.probabilities)), mean)
            expected /= expected.sum()
            np.testing.assert_allclose(law.probabilities, expected, atol=3e-16, rtol=2e-14)
            self.assertEqual(law.poisson_rate, mean)
            self.assertEqual(law.log_mean_tilt, 0.)
            self.assertLessEqual(law.omitted_mass, law.tail_tolerance)
            self.assertLessEqual(law.omitted_first_moment, law.tail_tolerance * mean)

    def test_extreme_shapes_normalize_and_preserve_mean_with_reported_drift(self):
        for mean in (1e-12, 1e-7, .1, 1., 2.8, 40., 100.):
            for shape in (-8., -2., 2., 8.):
                law = total_distribution(mean, shape)
                p = np.asarray(law.probabilities)
                retained_mean = float(np.arange(len(p)) @ p)
                self.assertTrue(np.isfinite(p).all())
                self.assertTrue((p >= 0).all())
                self.assertAlmostEqual(float(p.sum()), 1., places=14)
                self.assertLessEqual(law.omitted_mass, law.tail_tolerance)
                self.assertLessEqual(law.omitted_first_moment, law.tail_tolerance * mean)
                self.assertLessEqual(law.mean_constraint_error, 5e-12 * mean)
                self.assertLessEqual(abs(retained_mean - mean), law.mean_error_bound + 2e-13 * mean)
                self.assertAlmostEqual(retained_mean / mean, 1., places=10)

    def test_positive_zero_tilt_increases_zero_mass_at_same_mean(self):
        negative = total_distribution(2.7, -2.)
        neutral = total_distribution(2.7)
        positive = total_distribution(2.7, 2.)
        self.assertLess(negative.probabilities[0], neutral.probabilities[0])
        self.assertGreater(positive.probabilities[0], neutral.probabilities[0])
        self.assertGreater(positive.poisson_rate, neutral.poisson_rate)

    def test_zero_mean_is_exact_for_all_shapes(self):
        for shape in (-8., 0., 8.):
            law = total_distribution(0., shape)
            self.assertEqual(law.probabilities, (1.,))
            self.assertEqual(law.omitted_mass, 0.)
            self.assertEqual(law.omitted_first_moment, 0.)
            self.assertEqual(law.mean_constraint_error, 0.)
            self.assertEqual(law.mean_error_bound, 0.)

    def test_tail_exhaustion_raises_and_tighter_bound_grows_support(self):
        with self.assertRaisesRegex(ValueError, 'tail bounds'):
            total_distribution(100., 8., max_total=100)
        loose = total_distribution(3., 8., tail_tolerance=1e-6)
        tight = total_distribution(3., 8., tail_tolerance=1e-14)
        self.assertGreater(len(tight.probabilities), len(loose.probabilities))
        last = len(loose.probabilities) - 1
        # Independently reconstruct the infinite-law denominator and tail.
        rate = loose.poisson_rate
        denominator = np.exp(8. - rate) - np.expm1(-rate)
        self.assertAlmostEqual(loose.omitted_mass, poisson.sf(last, rate) / denominator, places=17)
        self.assertAlmostEqual(loose.omitted_first_moment,
                               rate * poisson.sf(last - 1, rate) / denominator, places=16)
        expected_mean = (3. - loose.omitted_first_moment) / (1. - loose.omitted_mass)
        self.assertAlmostEqual(np.arange(last + 1) @ loose.probabilities, expected_mean, places=12)


class AllocationShapeTests(unittest.TestCase):
    def test_null_matches_binomial(self):
        for total in (0, 1, 2, 10, 100, MAX_TOTAL):
            for share in (0., 1e-12, .2, .5, .99, 1.):
                p = allocation_distribution(total, share)
                expected = binom.pmf(np.arange(total + 1), total, share)
                np.testing.assert_allclose(p, expected, atol=5e-16, rtol=5e-14)

    def test_mean_preserved_at_shape_and_share_extremes(self):
        for total in (2, 4, 20, 100, MAX_TOTAL):
            for share in (1e-300, 1e-12, 1e-7, .01, .3, .5, .9, 1. - 1e-12):
                for shape in (-8., 8.):
                    p = np.asarray(allocation_distribution(total, share, shape))
                    self.assertTrue(np.isfinite(p).all())
                    self.assertTrue((p >= 0).all())
                    self.assertAlmostEqual(float(p.sum()), 1., places=14)
                    mean = float(np.arange(total + 1) @ p)
                    self.assertLessEqual(abs(mean - total * share), 3e-12 * total * share)

    def test_odd_total_has_no_central_effect_or_equal_cell(self):
        for total in (1, 3, 21, 511):
            ordinary = allocation_distribution(total, .37)
            self.assertEqual(allocation_distribution(total, .37, -8.), ordinary)
            self.assertEqual(allocation_distribution(total, .37, 8.), ordinary)
            self.assertFalse(any(2 * k == total for k in range(total + 1)))

    def test_zero_and_one_sided_shares_are_degenerate(self):
        for shape in (-8., 0., 8.):
            self.assertEqual(allocation_distribution(0, .37, shape), (1.,))
            self.assertEqual(allocation_distribution(4, 0., shape), (1., 0., 0., 0., 0.))
            self.assertEqual(allocation_distribution(4, 1., shape), (0., 0., 0., 0., 1.))

    def test_reflection_and_central_mass(self):
        for shape in (-8., -.7, .7, 8.):
            p = allocation_distribution(20, .25, shape)
            reflected = allocation_distribution(20, .75, shape)
            np.testing.assert_array_equal(p, reflected[::-1])
            symmetric = allocation_distribution(20, .5, shape)
            np.testing.assert_allclose(symmetric, symmetric[::-1], atol=2e-16)
        negative = allocation_distribution(10, .5, -2.)
        neutral = allocation_distribution(10, .5)
        positive = allocation_distribution(10, .5, 2.)
        self.assertLess(negative[5], neutral[5])
        self.assertGreater(positive[5], neutral[5])


class IntegratedShapeTests(unittest.TestCase):
    def test_null_matches_independent_poisson_difference(self):
        self.assertEqual(CLASS_ORDER, ('1st', 'Égal', '2nd'))
        for mean, share in ((.01, .1), (2.8, .4), (5., .5), (40., .99), (100., .2)):
            result = class_distribution(mean, share)
            first, second = mean * share, mean * (1 - share)
            expected = (skellam.sf(0, first, second), skellam.pmf(0, first, second),
                        skellam.cdf(-1, first, second))
            np.testing.assert_allclose(result.probabilities, expected, atol=3e-12, rtol=0)

    def test_normalization_reflection_and_preserved_direction_restriction(self):
        for mean in (.1, 2.7, 40.):
            for zero_shape in (-8., 0., 8.):
                for centre in (-8., 0., 8.):
                    result = class_distribution(mean, .25, zero_shape, centre)
                    p = np.asarray(result.probabilities)
                    reflected = class_distribution(mean, .75, zero_shape, centre)
                    np.testing.assert_allclose(p, reflected.probabilities[::-1], atol=2e-15)
                    self.assertTrue(np.isfinite(p).all())
                    self.assertTrue((p >= 0).all())
                    self.assertAlmostEqual(float(p.sum()), 1., places=14)
                    # This family cannot reverse the original half-share ordering.
                    self.assertLess(p[0], p[2])
        symmetric = class_distribution(3., .5, 8., 8.)
        self.assertAlmostEqual(symmetric.probabilities[0], symmetric.probabilities[2], places=14)

    def test_zero_and_one_sided_integration(self):
        for centre in (-8., 0., 8.):
            self.assertEqual(class_distribution(0., .2, 8., centre).probabilities, (0., 1., 0.))
            first = class_distribution(3., 1., 8., centre)
            second = class_distribution(3., 0., 8., centre)
            self.assertEqual(first.probabilities[2], 0.)
            self.assertEqual(second.probabilities[0], 0.)
            self.assertEqual(first.probabilities[1], first.total.probabilities[0])
            np.testing.assert_array_equal(first.probabilities, second.probabilities[::-1])

    def test_tail_mass_bounds_independent_longer_support_class_error(self):
        loose = class_distribution(4., .37, 8., -8., tail_tolerance=1e-6)
        tight = class_distribution(4., .37, 8., -8., tail_tolerance=1e-14)
        self.assertLessEqual(max(abs(a-b) for a, b in zip(loose.probabilities, tight.probabilities)),
                             loose.total.omitted_mass + tight.total.omitted_mass + 1e-14)

    def test_rejects_nonfinite_out_of_range_and_ambiguous_inputs(self):
        for mean in (-1., 1e-13, 100.1, float('inf'), float('nan'), True, '3'):
            with self.assertRaises(ValueError):
                total_distribution(mean)
        for shape in (-8.1, 8.1, float('nan'), float('inf'), True):
            with self.assertRaises(ValueError):
                total_distribution(3., shape)
            with self.assertRaises(ValueError):
                allocation_distribution(2, .4, shape)
        for share in (-.1, 1.1, float('nan'), float('inf'), True):
            with self.assertRaises(ValueError):
                allocation_distribution(2, share)
            with self.assertRaises(ValueError):
                class_distribution(3., share)
        for total in (-1, 2., MAX_TOTAL + 1, True):
            with self.assertRaises(ValueError):
                allocation_distribution(total, .5)
        for tolerance in (0., 1e-15, 1e-5, float('nan'), True):
            with self.assertRaises(ValueError):
                total_distribution(3., tail_tolerance=tolerance)
        for maximum in (0, 2., MAX_TOTAL + 1, True):
            with self.assertRaises(ValueError):
                total_distribution(3., max_total=maximum)


if __name__ == '__main__':
    unittest.main()
