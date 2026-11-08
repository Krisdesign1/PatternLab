"""Pure, bounded distribution-shape hypotheses for regulation half-goal counts.

No files, datasets, application models or fitting code are imported. ``mean``
and ``first_share`` are supplied prematch parameters, never realized target
counts. Two shape coefficients change zero-total/central-allocation mass while
preserving the *infinite-law* mean and the conditional allocation mean.

This is a calibration family, not a new discrimination signal. For first_share
< .5, the symmetric central tilt still gives P(1st) < P(2nd) when mean > 0.
It is not a production candidate or a claim of predictive improvement.

Finite total PMFs are explicitly conditioned on retained support. Both omitted
probability and omitted first moment are bounded; their mean drift is reported.
NumPy and SciPy are the only non-standard dependencies, as in the existing
isolated count-law research. No existing research/serving module is imported.
"""
from dataclasses import dataclass
from math import exp, fsum, isfinite, log, log1p
from numbers import Integral, Real

import numpy as np
from scipy.optimize import brentq
from scipy.special import gammaln, logsumexp
from scipy.stats import binom, poisson


VERSION = 'regulation-shape-calibration-v1'
CLASS_ORDER = ('1st', 'Égal', '2nd')
SHAPE_BOUNDS = (-8.0, 8.0)
MIN_POSITIVE_MEAN = 1e-12
MAX_MEAN = 100.0
MAX_TOTAL = 512
DEFAULT_TAIL_TOLERANCE = 1e-12
TAIL_TOLERANCE_BOUNDS = (1e-14, 1e-6)
MAX_LOG_MOMENT_RESIDUAL = 5e-12
_ROOT_XTOL = 1e-13
_ROOT_RTOL = 4 * np.finfo(float).eps


@dataclass(frozen=True)
class TotalDistribution:
    """Normalized PMF on integers 0..len(probabilities)-1, plus true tail.

    ``omitted_first_moment`` is E[N * I(N > last)], not its conditional mean.
    ``mean_constraint_error`` is the evaluated infinite-law root residual in
    count units. ``mean_error_bound`` combines it with a truncation-drift bound
    for the returned normalized PMF; floating-point evaluation rounding is
    additional. ``poisson_rate`` is the positive-count kernel rate, and is
    generally different from the requested ``mean``.
    """
    probabilities: tuple[float, ...]
    mean: float
    zero_adjustment: float
    poisson_rate: float
    log_mean_tilt: float
    omitted_mass: float
    omitted_first_moment: float
    mean_constraint_error: float
    mean_error_bound: float
    tail_tolerance: float


@dataclass(frozen=True)
class ClassDistribution:
    """Probabilities in CLASS_ORDER and the total-law truncation diagnostics.

    Each class's absolute truncation error is at most total.omitted_mass.
    This bound excludes root-solver/evaluation error. Allocation is never
    truncated.
    """
    probabilities: tuple[float, float, float]
    total: TotalDistribution
    first_share: float
    central_adjustment: float


def _real(value, name):
    if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(value):
        raise ValueError(name + ' must be a finite real number.')
    return float(value)


def _shape(value, name):
    value = _real(value, name)
    if not SHAPE_BOUNDS[0] <= value <= SHAPE_BOUNDS[1]:
        raise ValueError(name + ' must be within [-8, 8].')
    return value


def _share(value):
    value = _real(value, 'first_share')
    if not 0 <= value <= 1:
        raise ValueError('first_share must be within [0, 1].')
    return value


def _integer(value, name, minimum):
    if isinstance(value, bool) or not isinstance(value, Integral) or not minimum <= value <= MAX_TOTAL:
        raise ValueError(name + ' must be an integer within [' + str(minimum) + ', 512].')
    return int(value)


def _log_denominator(rate, zero_adjustment):
    # D = exp(a-rate) + (1-exp(-rate)); each term is nonnegative.
    # This remains stable when a is negative and rate is very small.
    return float(np.logaddexp(zero_adjustment - rate,
                              log(-np.expm1(-rate))))


def _total_rate(mean, zero_adjustment):
    if zero_adjustment == 0:
        return mean, 0.0, 0.0
    log_mean = log(mean)

    def residual(log_rate):
        rate = exp(log_rate)
        return log_rate - _log_denominator(rate, zero_adjustment) - log_mean

    # E[N] = rate/D. D lies between min(1,exp(a)) and max(1,exp(a)).
    # Padding protects the mathematical bracket against rounding at endpoints.
    low = log_mean + min(0., zero_adjustment) - 1e-12
    high = log_mean + max(0., zero_adjustment) + 1e-12
    log_rate = brentq(residual, low, high, xtol=_ROOT_XTOL,
                      rtol=_ROOT_RTOL, maxiter=200)
    if abs(residual(log_rate)) > MAX_LOG_MOMENT_RESIDUAL:
        raise ArithmeticError('Total mean constraint did not converge.')
    rate = exp(log_rate)
    return rate, log_rate - log_mean, _log_denominator(rate, zero_adjustment)


def total_distribution(mean, zero_adjustment=0., *,
                       tail_tolerance=DEFAULT_TAIL_TOLERANCE,
                       max_total=MAX_TOTAL):
    """Return the mean-preserving zero-adjusted Poisson law on bounded support.

    P(N=n) is proportional to Pois(n; mean)*exp(a*I(n=0)+b*n), with b
    solved in log-rate space so E[N]=mean (maximum accepted log-moment residual
    5e-12; actual residual is reported). a=0 is exactly the Poisson family.
    Positive means must be in [1e-12,100]; zero is an exact point mass at zero.
    a is in [-8,8]. The positive lower bound prevents underflow in the root.

    Support grows until omitted mass <= tolerance AND omitted first moment
    <= tolerance*mean. Exceeding max_total raises; a tail is never silently
    accepted. Tolerance must be in [1e-14,1e-6]; max_total is in [1,512].
    """
    mean = _real(mean, 'mean')
    if mean != 0 and not MIN_POSITIVE_MEAN <= mean <= MAX_MEAN:
        raise ValueError('mean must be zero or within [1e-12, 100].')
    zero_adjustment = _shape(zero_adjustment, 'zero_adjustment')
    tail_tolerance = _real(tail_tolerance, 'tail_tolerance')
    if not TAIL_TOLERANCE_BOUNDS[0] <= tail_tolerance <= TAIL_TOLERANCE_BOUNDS[1]:
        raise ValueError('tail_tolerance must be within [1e-14, 1e-6].')
    max_total = _integer(max_total, 'max_total', 1)
    if mean == 0:
        # The tilt is unidentifiable for a point mass; use its neutral value.
        return TotalDistribution((1.,), mean, zero_adjustment, 0., 0., 0., 0., 0., 0., tail_tolerance)
    rate, tilt, log_denominator = _total_rate(mean, zero_adjustment)

    def tail(last):
        # Exact infinite-law upper-tail probability and unnormalized moment.
        mass = exp(float(poisson.logsf(last, rate)) - log_denominator)
        moment = exp(log(rate) + float(poisson.logsf(last - 1, rate)) - log_denominator)
        return mass, moment

    last = 0
    omitted_mass, omitted_moment = tail(last)
    while omitted_mass > tail_tolerance or omitted_moment > tail_tolerance * mean:
        last += 1
        if last > max_total:
            raise ValueError('max_total cannot meet the probability and first-moment tail bounds.')
        omitted_mass, omitted_moment = tail(last)
    totals = np.arange(last + 1, dtype=float)
    log_probabilities = poisson.logpmf(totals, rate) - log_denominator
    log_probabilities[0] += zero_adjustment
    probabilities = np.exp(log_probabilities)
    probabilities /= fsum(probabilities)
    implied_mean = exp(log(rate) - log_denominator)
    constraint_error = abs(implied_mean - mean)
    # Bound the conditioning drift without cancellation, then add root error.
    error_bound = constraint_error + (omitted_moment + implied_mean * omitted_mass) / (1 - omitted_mass)
    return TotalDistribution(tuple(map(float, probabilities)), mean, zero_adjustment,
                             rate, tilt, omitted_mass, omitted_moment, constraint_error,
                             error_bound, tail_tolerance)


def allocation_distribution(total, first_share, central_adjustment=0.):
    """Return P(K=0..total | N=total), preserving E[K|N]=total*first_share.

    Weights are C(n,k)*exp(alpha*k+c*I(2*k=n)); alpha is solved in log
    space. c=0 is binomial. Odd n has no central cell, so c has no effect.
    n=0 and shares 0/1 are exact point masses. For share=.5 symmetry gives
    alpha=0. Inputs are bounded by n<=512 and c in [-8,8]. The maximum
    accepted log-moment root residual is 5e-12, in addition to rounding of the
    returned PMF (which can underflow negligible individual cells to zero).
    """
    total = _integer(total, 'total', 0)
    first_share = _share(first_share)
    central_adjustment = _shape(central_adjustment, 'central_adjustment')
    if total == 0:
        return (1.,)
    if first_share in (0., 1.):
        values = [0.] * (total + 1)
        values[0 if first_share == 0. else total] = 1.
        return tuple(values)
    if central_adjustment == 0. or total % 2:
        values = binom.pmf(np.arange(total + 1), total, first_share)
        return tuple(map(float, values / fsum(values)))
    # Reflect shares above .5 to use the accurately represented smaller side
    # and obtain reverse-share symmetry from the same numerical computation.
    if first_share > .5:
        return allocation_distribution(total, 1. - first_share, central_adjustment)[::-1]
    allocation = np.arange(total + 1, dtype=float)
    log_weights = (gammaln(total + 1) - gammaln(allocation + 1)
                   - gammaln(total - allocation + 1))
    log_weights[total // 2] += central_adjustment

    def raw(alpha):
        return log_weights + alpha * allocation

    if first_share == .5:
        alpha = 0.
    else:
        log_target = log(total) + log(first_share)

        def residual(alpha):
            weights = raw(alpha)
            log_moment = logsumexp(weights[1:] + np.log(allocation[1:])) - logsumexp(weights)
            return float(log_moment - log_target)

        ordinary_log_odds = log(first_share) - log1p(-first_share)
        # Adjacent central-tilt log-weight changes are bounded by abs(c),
        # bracketing the binomial mean in stochastic order.
        width = abs(central_adjustment) + 1e-12
        alpha = brentq(residual, ordinary_log_odds - width, ordinary_log_odds + width,
                       xtol=_ROOT_XTOL, rtol=_ROOT_RTOL, maxiter=200)
        if abs(residual(alpha)) > MAX_LOG_MOMENT_RESIDUAL:
            raise ArithmeticError('Allocation mean constraint did not converge.')
    weights = raw(alpha)
    probabilities = np.exp(weights - logsumexp(weights))
    probabilities /= fsum(probabilities)
    return tuple(map(float, probabilities))


def class_distribution(mean, first_share, zero_adjustment=0., central_adjustment=0., *,
                       tail_tolerance=DEFAULT_TAIL_TOLERANCE, max_total=MAX_TOTAL):
    """Integrate unknown N and K into (1st, Égal, 2nd) probabilities.

    No realized target total or half score is accepted. The returned total law
    makes truncation explicit. This function has no league-frequency mixture;
    a caller would have to specify any such separate baseline mixture.
    """
    first_share = _share(first_share)
    central_adjustment = _shape(central_adjustment, 'central_adjustment')
    law = total_distribution(mean, zero_adjustment, tail_tolerance=tail_tolerance,
                             max_total=max_total)
    contributions = [[], [], []]
    for total, mass in enumerate(law.probabilities):
        allocation = allocation_distribution(total, first_share, central_adjustment)
        for first, probability in enumerate(allocation):
            index = 0 if 2 * first > total else 1 if 2 * first == total else 2
            contributions[index].append(mass * probability)
    values = [fsum(part) for part in contributions]
    normalizer = fsum(values)
    probabilities = tuple(value / normalizer for value in values)
    return ClassDistribution(probabilities, law, first_share, central_adjustment)
