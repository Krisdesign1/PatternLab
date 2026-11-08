"""Isolated count-likelihood models for retrospective halftime research.

Only NumPy and SciPy are imported. This module does not read or write files,
call services, or import serving-model code. Class order is first/equal/second.
The compound law factors the *unknown* total N and first-half allocation K|N;
no realized target total is used to construct a class forecast.
"""
from math import log

import numpy as np
from scipy.optimize import minimize, minimize_scalar
from scipy.special import gammaln, logsumexp
from scipy.stats import binom, nbinom, poisson


VERSION = 'halftime-count-ridge-v1'
MIN_MEAN = .01
MAX_MEAN = 20.
TAIL = 1e-12
CONCENTRATION_BOUNDS = (1., 1e6)
ALLOCATION_POWER_BOUNDS = (.25, 4.)


def _matrix(x, *, columns=None):
    x = np.asarray(x, dtype=float)
    if x.ndim != 2 or not np.isfinite(x).all():
        raise ValueError('Features must be a finite two-dimensional matrix.')
    if columns is not None and x.shape[1] != columns:
        raise ValueError('Feature count differs from the fitted model.')
    return x


def _counts(y, n=None):
    y = np.asarray(y, dtype=float)
    if (y.ndim != 2 or y.shape[1] != 2 or not np.isfinite(y).all()
            or (y < 0).any() or (y != np.floor(y)).any()
            or (n is not None and len(y) != n)):
        raise ValueError('Targets must have shape (matches, 2) with nonnegative integer counts.')
    return y


def _means(values, n=None):
    values = np.asarray(values, dtype=float)
    if (values.ndim != 2 or values.shape[1] != 2 or not np.isfinite(values).all()
            or (values < 0).any() or (n is not None and len(values) != n)):
        raise ValueError('Means must have shape (matches, 2) and be finite and nonnegative.')
    return values


def _leagues(leagues, n):
    values = np.asarray(leagues)
    if values.ndim != 1 or len(values) != n:
        raise ValueError('One league identifier is required per match.')
    return [str(value) for value in values.tolist()]


def _design(z, leagues, levels):
    lookup = {level: i for i, level in enumerate(levels)}
    if any(league not in lookup for league in leagues):
        raise ValueError('An unknown league has no fitted intercept.')
    x = np.zeros((len(z), len(levels) + z.shape[1]), dtype=float)
    if len(z):
        x[np.arange(len(z)), [lookup[league] for league in leagues]] = 1.
    x[:, len(levels):] = z
    return x


def _safe_exp(eta):
    """Convex quadratic continuation prevents overflow during line searches.

    At ordinary goal intensities this is exactly exp(eta). Above eta=30,
    value and first two derivatives agree at the join. This continuation is
    only an optimizer safeguard; predictions have explicit intensity bounds.
    """
    excess = np.maximum(eta - 30., 0.)
    value = np.exp(np.minimum(eta, 30.))
    derivative = value.copy()
    high = excess > 0
    value[high] *= 1. + excess[high] + .5 * excess[high] ** 2
    derivative[high] *= 1. + excess[high]
    return value, derivative


def fit_count(x, y, leagues, penalty=30.0):
    """Fit two Poisson ridge GLMs and return a JSON-serializable model.

    Feature centering/scaling and league levels use only the supplied fit rows.
    Each loss is mean Poisson NLL plus penalty/(2*n) for numeric coefficients;
    league intercepts have weak ridge strength 1. There is no global intercept
    in addition to the league intercepts. Neither class labels nor validation
    outcomes are used for fitting or convergence decisions.
    """
    x = _matrix(x)
    if not len(x):
        raise ValueError('At least one fitting match is required.')
    y = _counts(y, len(x))
    league_ids = _leagues(leagues, len(x))
    if not np.isfinite(penalty) or penalty < 0:
        raise ValueError('Ridge penalty must be finite and nonnegative.')
    center = x.mean(axis=0)
    scale = x.std(axis=0)
    constant = np.ptp(x, axis=0) == 0
    center[constant] = x[0, constant]
    scale[constant] = 1.
    z = (x - center) / scale
    if not np.isfinite(z).all():
        raise ValueError('Feature standardization is not finite.')
    levels = sorted(set(league_ids))
    design = _design(z, league_ids, levels)
    ridge = np.full(design.shape[1], float(penalty))
    ridge[:len(levels)] = 1.
    coefficients, diagnostics = [], []
    n = len(x)
    for half in range(2):
        target = y[:, half]
        initial = np.zeros(design.shape[1])
        for index in range(len(levels)):
            initial[index] = log(max(target[design[:, index] == 1].mean(), MIN_MEAN))
        constant = float(gammaln(target + 1).mean())

        def objective(beta):
            eta = design @ beta
            intensity, derivative = _safe_exp(eta)
            value = float((intensity - target * eta).mean()
                          + .5 * np.dot(ridge, beta * beta) / n)
            gradient = (design.T @ (derivative - target) + ridge * beta) / n
            return value, gradient

        fitted = minimize(objective, initial, jac=True, method='L-BFGS-B',
                          options={'maxiter': 1000, 'ftol': 1e-12, 'gtol': 1e-7,
                                   'maxls': 50})
        if not fitted.success or not np.isfinite(fitted.x).all():
            raise RuntimeError('Poisson optimization failed: ' + str(fitted.message))
        coefficients.append(fitted.x.tolist())
        diagnostics.append(dict(success=True, iterations=int(fitted.nit),
                                message=str(fitted.message),
                                penalized_mean_nll=float(fitted.fun + constant),
                                max_abs_gradient=float(np.abs(fitted.jac).max())))
    return dict(version=VERSION, feature_count=x.shape[1], training_matches=n,
                center=center.tolist(), scale=scale.tolist(), league_levels=levels,
                coefficients=coefficients, penalty=float(penalty),
                league_intercept_penalty=1., prediction_bounds=[MIN_MEAN, MAX_MEAN],
                diagnostics=diagnostics)


def predict_means(model, x, leagues):
    """Return prematch G1/G2 means; reject leagues absent from fit rows."""
    if model.get('version') != VERSION:
        raise ValueError('Unknown count model version.')
    x = _matrix(x, columns=model['feature_count'])
    league_ids = _leagues(leagues, len(x))
    center = np.asarray(model['center'], dtype=float)
    scale = np.asarray(model['scale'], dtype=float)
    coefficients = np.asarray(model['coefficients'], dtype=float)
    if (center.shape != (x.shape[1],) or scale.shape != center.shape or (scale <= 0).any()
            or not np.isfinite(center).all() or not np.isfinite(scale).all()
            or coefficients.shape != (2, len(model['league_levels']) + x.shape[1])
            or not np.isfinite(coefficients).all()):
        raise ValueError('Invalid serialized count model.')
    design = _design((x - center) / scale, league_ids, model['league_levels'])
    eta = design @ coefficients.T
    if not np.isfinite(eta).all():
        raise ValueError('Prediction linear predictors are not finite.')
    return np.exp(np.clip(eta, log(MIN_MEAN), log(MAX_MEAN)))


def _concentration(value):
    value = float(value)
    if not np.isfinite(value) or not CONCENTRATION_BOUNDS[0] <= value <= CONCENTRATION_BOUNDS[1]:
        raise ValueError('Concentration must be between 1 and 1e6.')
    return value


def fit_dispersion(y, means):
    """Estimate total and allocation concentrations on supplied fitting rows.

    A concentration controls NB total overdispersion, bounded to [1, 1e6]. A
    power in [.25, 4] transforms Binomial(K|N; p) by raising each probability
    to that power and renormalizing. Power >1 concentrates allocation and <1
    disperses it; 1 is binomial. The share p is a nominal parameter: power !=1
    need not preserve E[K|N]=N*p. Means remain fixed during this global fit.
    """
    y = _counts(y)
    means = _means(means, len(y))
    if not len(y):
        raise ValueError('At least one fitting match is required.')
    total = y.sum(axis=1)
    mu = means.sum(axis=1)
    if ((mu == 0) & (total > 0)).any():
        raise ValueError('A positive observed total has zero predicted mean.')
    share = np.divide(means[:, 0], mu, out=np.full(len(mu), .5), where=mu > 0)
    impossible = ((share == 0) & (y[:, 0] > 0)) | ((share == 1) & (y[:, 1] > 0))
    if impossible.any():
        raise ValueError('An observed allocation has zero predicted probability.')
    allocations = np.arange(int(total.max()) + 1)
    allocation_logpmf = binom.logpmf(allocations[None, :], total[:, None], share[:, None])
    observed_logpmf = allocation_logpmf[np.arange(len(y)), y[:, 0].astype(int)]

    def total_loss(kappa):
        return float(-nbinom.logpmf(total, kappa, kappa / (kappa + mu)).mean())

    def allocation_loss(power):
        return float((logsumexp(power * allocation_logpmf, axis=1)
                      - power * observed_logpmf).mean())

    def estimate(loss):
        fitted = minimize_scalar(lambda log_k: loss(np.exp(log_k)),
                                 bounds=(0., log(CONCENTRATION_BOUNDS[1])),
                                 method='bounded', options={'xatol': 1e-5})
        if not fitted.success:
            raise RuntimeError('Dispersion optimization failed.')
        candidates = [1., float(np.exp(fitted.x)), CONCENTRATION_BOUNDS[1]]
        # A flat likelihood prefers the near-Poisson/binomial endpoint.
        best = min(candidates, key=lambda k: (loss(k), -k))
        return best, float(loss(best))

    total_k, total_nll = estimate(total_loss)
    power_fit = minimize_scalar(lambda log_power: allocation_loss(np.exp(log_power)),
                                bounds=tuple(map(log, ALLOCATION_POWER_BOUNDS)),
                                method='bounded', options={'xatol': 1e-6})
    if not power_fit.success:
        raise RuntimeError('Allocation-power optimization failed.')
    powers = [.25, 1., 4., float(np.exp(power_fit.x))]
    power = min(powers, key=lambda value: (allocation_loss(value), abs(log(value))))
    allocation_nll = allocation_loss(power)
    return dict(total_concentration=total_k, allocation_power=power,
                training_matches=len(y), mean_total_nll=total_nll,
                mean_allocation_nll=allocation_nll,
                limitation='Total overdispersion only; powered allocation changes nominal mean N*p.')


def _power_binomial_weights(totals, share, power):
    """Normalized K|N arrays; impossible K>N cells are zero."""
    totals = np.asarray(totals, dtype=int)
    allocation = np.arange(int(totals.max()) + 1)
    log_weights = power * binom.logpmf(allocation[None, :], totals[:, None], share)
    return np.exp(log_weights - logsumexp(log_weights, axis=1)[:, None])


def class_probabilities(means, shape='poisson', dispersion=None):
    """Integrate unknown N,K into probabilities ordered first/equal/second.

    shape='poisson' uses Poisson(N; G1+G2) and Binomial(K|N; G1/(G1+G2)),
    exactly the independent-Poisson law. shape='compound' uses a negative
    binomial total and powered-binomial allocation with fit_dispersion's keys.
    The nominal first share is unchanged, but the power can change its implied
    conditional mean, except for power=1 or the symmetric share p=.5.
    Each total distribution is truncated at survival mass <=1e-12 and the
    resulting three probabilities are normalized. Zero means are supported.
    """
    means = _means(means)
    if shape not in ('poisson', 'compound'):
        raise ValueError('Shape must be poisson or compound.')
    if shape == 'compound':
        if dispersion is None:
            raise ValueError('Compound predictions require fitted dispersion.')
        total_k = _concentration(dispersion['total_concentration'])
        power = float(dispersion['allocation_power'])
        if not np.isfinite(power) or not ALLOCATION_POWER_BOUNDS[0] <= power <= ALLOCATION_POWER_BOUNDS[1]:
            raise ValueError('Allocation power must be between .25 and 4.')
    result = np.zeros((len(means), 3), dtype=float)
    for index, (first, second) in enumerate(means):
        mu = first + second
        if mu == 0:
            result[index, 1] = 1.
            continue
        share = first / mu
        law = poisson(mu) if shape == 'poisson' else nbinom(total_k, total_k / (total_k + mu))
        last = law.isf(TAIL)
        if not np.isfinite(last) or last > 5000:
            raise ValueError('Count law needs unsupported truncation support.')
        total = np.arange(int(last) + 1)
        mass = law.pmf(total)
        if share == 0 or share == 1:
            result[index, 1] = mass[0]
            result[index, 0 if share == 1 else 2] = mass[1:].sum()
        else:
            if shape == 'poisson':
                allocation = binom(total, share)
                first_wins = allocation.sf(total // 2)
                second_wins = allocation.cdf((total - 1) // 2)
                equal = np.where(total % 2 == 0, allocation.pmf(total // 2), 0.)
            else:
                weights = _power_binomial_weights(total, share, power)
                delta = 2 * total[None, :] - total[:, None]
                first_wins = np.where(delta > 0, weights, 0.).sum(axis=1)
                second_wins = np.where(delta < 0, weights, 0.).sum(axis=1)
                equal = np.where(delta == 0, weights, 0.).sum(axis=1)
            result[index] = [np.dot(mass, first_wins), np.dot(mass, equal),
                             np.dot(mass, second_wins)]
        if not np.isfinite(result[index]).all() or (result[index] < -1e-12).any():
            raise RuntimeError('Invalid integrated class probabilities.')
        result[index] = np.maximum(result[index], 0.)
        result[index] /= result[index].sum()
    return result
