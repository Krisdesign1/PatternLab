"""Independently audit the frozen single-xG-indicator experiment.

Imports neither the model nor experiment module. The fixed source, scalar
optimum, probabilities, labels, metrics, bootstrap and decision are reconstructed
from saved research artifacts. No serving artifacts or historical 2026 outcomes
are read or written.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
import argparse
import json

import numpy as np
from scipy.optimize import minimize_scalar


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'data/research/halftime_learning/2026-10-27-v1'
SOURCE_DIGEST = '24bdb67de05a556f289b913e95516fb7e860c64517baa6753a34827a8da6cac2'
LABELS = ('1st', 'Égal', '2nd')
PROFILES = ('home_all', 'home_home', 'away_all', 'away_away')
COLUMNS = tuple(p + '_' + g + '_contrast' for p in PROFILES
                for g in ('ht_for', 'ht_against', 'sh_for', 'sh_against')) + tuple(
    p + '_' + half + '_' + metric + '_' + side + '_contrast'
    for p in PROFILES for half in ('first', 'second')
    for metric in ('xg', 'on_goal') for side in ('for', 'against'))
FIRST_NAMES = tuple(p + '_first_xg_' + side + '_contrast'
                    for p in ('home_all', 'away_all') for side in ('for', 'against'))
SECOND_NAMES = tuple(name.replace('_first_', '_second_') for name in FIRST_NAMES)
FOLDS = (
    dict(name='2025_H1', fit_before='2025-01-01', start='2025-01-01', end='2025-07-01'),
    dict(name='2025_H2', fit_before='2025-07-01', start='2025-07-01', end='2026-01-01'),
)


def read(path):
    return json.loads(path.read_text())


def load(path):
    with np.load(path, allow_pickle=False) as arrays:
        return {key: arrays[key] for key in arrays.files}


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def exact(actual, expected, label):
    try:
        np.testing.assert_array_equal(actual, expected)
    except AssertionError as exc:
        raise AssertionError(label + ': ' + str(exc)) from exc


def same(actual, expected, label, atol=2e-10):
    if isinstance(expected, dict):
        assert set(actual) == set(expected), label + '/keys'
        for key, value in expected.items():
            same(actual[key], value, label + '/' + key, atol)
    elif isinstance(expected, (tuple, list)):
        assert len(actual) == len(expected), label + '/length'
        for i, value in enumerate(expected):
            same(actual[i], value, label + '/' + str(i), atol)
    elif expected is None or isinstance(expected, (str, bool, int)):
        assert actual == expected, label
    else:
        try:
            np.testing.assert_allclose(actual, expected, atol=atol, rtol=2e-10)
        except AssertionError as exc:
            raise AssertionError(label + ': ' + str(exc)) from exc


def probabilities(base, signal, beta):
    """Direct multiplicative tilt, independent of log-softmax implementation."""
    if beta == 0:
        return base.copy()
    factor = np.column_stack((np.exp(beta * signal), np.ones(len(signal)),
                              np.exp(-beta * signal)))
    weights = base * factor
    result = weights / weights.sum(1)[:, None]
    result[signal == 0] = base[signal == 0]
    assert np.isfinite(result).all() and (result > 0).all()
    same(result.sum(1), np.ones(len(result)), 'normalized tilted probabilities')
    return result


def loss_gradient(base, signal, y, beta):
    p = probabilities(base, signal, beta)
    observed = 1 - y
    mean_direction = p[:, 0] - p[:, 2]
    nll = -np.log(p[np.arange(len(y)), y]).sum() + 4 * beta * beta
    gradient = np.dot(signal, mean_direction - observed) + 8 * beta
    curvature = np.dot(signal * signal,
                       p[:, 0] + p[:, 2] - mean_direction * mean_direction) + 8
    return float(nll), float(gradient), float(curvature)


def optimize(base, signal, y):
    """Independent derivative bisection, corroborated by scalar value minimization."""
    low, high = -1., 1.
    if loss_gradient(base, signal, y, 0.)[1] == 0:
        beta = 0.
    elif loss_gradient(base, signal, y, low)[1] >= 0:
        beta = low
    elif loss_gradient(base, signal, y, high)[1] <= 0:
        beta = high
    else:
        for _ in range(80):
            middle = (low + high) / 2
            if loss_gradient(base, signal, y, middle)[1] > 0:
                high = middle
            else:
                low = middle
        beta = (low + high) / 2
    scalar = minimize_scalar(lambda b: loss_gradient(base, signal, y, b)[0],
                             method='bounded', bounds=(-1., 1.),
                             options=dict(xatol=1e-13))
    assert scalar.success, 'Independent scalar optimizer failed'
    assert loss_gradient(base, signal, y, beta)[0] <= scalar.fun + 1e-8
    return beta


def metrics(y, p, reference=None):
    winner, confidence = p.argmax(1), p.max(1)
    correct = winner == y
    result = dict(matches=len(y), correct=int(correct.sum()), accuracy=float(correct.mean()),
                  logloss=float(np.mean(-np.log(p[np.arange(len(y)), y]))),
                  brier=float(np.mean(np.sum((p - np.eye(3)[y]) ** 2, axis=1))),
                  predicted_classes={label: int(np.sum(winner == i))
                                     for i, label in enumerate(LABELS)},
                  confidence_thresholds={}, calibration={})
    for threshold in (.5, .6, .7):
        selected = confidence >= threshold
        n = int(selected.sum())
        result['confidence_thresholds'][str(threshold)] = dict(
            matches=n, coverage=float(selected.mean()), correct=int(correct[selected].sum()),
            accuracy=float(correct[selected].mean()) if n else None,
            mean_confidence=float(confidence[selected].mean()) if n else None,
            reference_accuracy=float(np.mean(reference[selected].argmax(1) == y[selected]))
            if n and reference is not None else None)
    for i, label in enumerate(LABELS):
        bins = []
        for index in range(10):
            lower, upper = index / 10, (index + 1) / 10
            selected = ((p[:, i] >= lower) &
                        ((p[:, i] < upper) if index < 9 else (p[:, i] <= upper)))
            n = int(selected.sum())
            bins.append(dict(lower=lower, upper=upper, count=n,
                             probability=float(p[selected, i].mean()) if n else None,
                             outcome_rate=float(np.mean(y[selected] == i)) if n else None))
        result['calibration'][label] = bins
    return result


def paired(y, p, base, days, fold):
    gain = np.log(p[np.arange(len(y)), y] / base[np.arange(len(y)), y])
    blocks = defaultdict(list)
    beginning, ending = (datetime.fromisoformat(fold[key]) for key in ('start', 'end'))
    for index, day in enumerate(days):
        date = datetime.fromisoformat(str(day))
        monday = date - timedelta(days=date.weekday())
        if beginning <= monday and monday + timedelta(days=7) <= ending:
            blocks[monday.date().isoformat()].append(index)
    members = list(blocks.values())
    closed = [index for rows in members for index in rows]
    interval = None
    if len(members) >= 20:
        counts = np.asarray([len(rows) for rows in members])
        totals = np.asarray([sum(gain[rows]) for rows in members])
        draws = np.random.default_rng(20261027).integers(
            len(members), size=(2000, len(members)))
        bootstrap = totals[draws].sum(axis=1) / counts[draws].sum(axis=1)
        interval = np.quantile(bootstrap, [.025, .975]).tolist()
    return dict(gain_positive_is_better=float(gain.mean()), closed_weeks=len(members),
                matches_in_interval=len(closed),
                closed_week_gain=float(gain[closed].mean()) if closed else None,
                interval=interval)


def audit(directory):
    plan, prep = read(directory / 'plan.json'), read(directory / 'preparation.json')
    for filename, key in (('plan.json', 'plan_sha256'), ('dataset.npz', 'data_sha256')):
        assert digest(directory / filename) == prep[key], filename + '/digest'
    for filename, expected in {**plan['frozen_prior_code'], **plan['code_sha256']}.items():
        assert digest(ROOT / filename) == expected, filename + '/frozen code'
    assert plan['source'] == str(SOURCE.relative_to(ROOT))
    assert digest(SOURCE / 'preparation.json') == plan['source_preparation_sha256']
    source_prep = read(SOURCE / 'preparation.json')
    assert digest(SOURCE / 'dataset.npz') == SOURCE_DIGEST
    assert source_prep['data_sha256'] == plan['source_data_sha256'] == SOURCE_DIGEST
    assert digest(SOURCE / 'plan.json') == source_prep['plan_sha256']
    assert plan['validation_folds'] == list(FOLDS)
    assert plan['learner']['bounds'] == [-1., 1.]
    assert plan['learner']['penalty'] == 8. and plan['learner']['order'] == list(LABELS)
    assert plan['signal']['first_columns'] == list(FIRST_NAMES)
    assert plan['signal']['second_columns'] == list(SECOND_NAMES)
    first = [COLUMNS.index(name) for name in FIRST_NAMES]
    second = [COLUMNS.index(name) for name in SECOND_NAMES]
    assert plan['signal']['first_indices'] == first == [16, 17, 32, 33]
    assert plan['signal']['second_indices'] == second == [20, 21, 36, 37]
    source, a = load(SOURCE / 'dataset.npz'), load(directory / 'dataset.npz')
    assert set(a) == {'ids', 'days', 'leagues', 'y', 'goals', 'clubs', 'signal'}
    for key in ('ids', 'days', 'leagues', 'y', 'goals', 'clubs'):
        exact(a[key], source[key], 'source identity/' + key)
    assert source['rich'].shape == (3990, 48)
    assert len(a['ids']) == len(set(a['ids'])) == prep['examples'] == 3990
    assert np.all(a['days'] >= '2024-01-01') and np.all(a['days'] < '2026-01-01')
    assert prep['max_target_day'] == str(max(a['days'])) == '2025-12-30'
    exact(a['y'], 1 - np.sign(a['goals'][:, 0] - a['goals'][:, 1]), 'score-derived labels')
    assert a['clubs'].shape == (3990, 3) and (a['clubs'] > 0).all()
    same(a['clubs'].sum(1), np.ones(3990), 'normalized baseline')
    signal = (sum(source['rich'][:, i] for i in first) -
              sum(source['rich'][:, i] for i in second)) / 4
    exact(a['signal'], signal, 'independent eight-field signal')
    assert np.isfinite(signal).all() and prep['signal_nonzero'] == int((signal != 0).sum())
    exact(probabilities(a['clubs'], signal, 0.), a['clubs'], 'exact beta-zero baseline')
    exact(probabilities(a['clubs'], np.zeros(3990), .7), a['clubs'], 'exact zero-signal baseline')
    evaluation = directory / 'evaluation'
    results, decision = read(evaluation / 'results.json'), read(evaluation / 'decision.json')
    assert results['status'] == 'exploratory_single_xg_tilt' and results['no2026evaluated']
    assert datetime.fromisoformat(results['created_at_utc']) >= datetime.fromisoformat(plan['created_at_utc'])
    assert set(results['folds']) == {fold['name'] for fold in FOLDS}
    checked, all_y, all_base, all_p, fold_summary = [], [], [], [], {}
    for fold in FOLDS:
        name = fold['name']
        train = np.flatnonzero(a['days'] < fold['fit_before'])
        valid = np.flatnonzero((a['days'] >= fold['start']) & (a['days'] < fold['end']))
        assert len(train) and len(valid) and not set(train) & set(valid)
        assert max(a['days'][train]) < min(a['days'][valid])
        assert (a['days'][valid] >= fold['fit_before']).all()
        saved = load(evaluation / (name + '_predictions.npz'))
        assert set(saved) == {'train_ids', 'ids', 'days', 'y', 'signal', 'clubs', 'candidate'}
        exact(saved['train_ids'], a['ids'][train], name + '/fit IDs')
        for key in ('ids', 'days', 'y', 'signal', 'clubs'):
            exact(saved[key], a[key][valid], name + '/validation identity/' + key)
        assert not set(saved['train_ids']) & set(saved['ids'])
        model = read(evaluation / (name + '_model.json'))
        same(results['folds'][name]['model'], model, name + '/saved model identity')
        beta = model['beta']
        assert model['version'] == 'xg-allocation-offset-v1' and -1 <= beta <= 1
        assert model['penalty'] == 8. and model['bounds'] == [-1., 1.]
        assert model['training_matches'] == len(train) == results['folds'][name]['train_matches']
        base, s, y = a['clubs'][train], signal[train], a['y'][train]
        independent_beta = optimize(base, s, y)
        same(beta, independent_beta, name + '/independent scalar beta', atol=5e-11)
        value, gradient, hessian = loss_gradient(base, s, y, beta)
        zero = loss_gradient(base, s, y, 0.)[0]
        residual = (abs(gradient) if -1 < beta < 1 else
                    max(0., -gradient if beta == -1 else gradient))
        assert hessian >= 8 and residual < 1e-7 and value <= zero + 1e-8
        for key, expected in dict(objective=value, zero_objective=zero, gradient=gradient,
                                  hessian=hessian, kkt_residual=residual, signal_min=float(s.min()),
                                  signal_max=float(s.max()), signal_mean=float(s.mean()),
                                  signal_std=float(s.std())).items():
            same(model[key], expected, name + '/' + key)
        epsilon = 1e-5
        finite_gradient = (loss_gradient(base, s, y, beta + epsilon)[0] -
                           loss_gradient(base, s, y, beta - epsilon)[0]) / (2 * epsilon)
        same(finite_gradient, gradient, name + '/finite-difference gradient', atol=2e-7)
        mutated = a['y'].copy()
        mutated[a['days'] >= fold['fit_before']] = (mutated[a['days'] >= fold['fit_before']] + 1) % 3
        exact(mutated[train], y, name + '/future-label invariance')
        same(optimize(base, s, mutated[train]), independent_beta,
             name + '/future-label independent optimum', atol=0.)
        p = probabilities(a['clubs'][valid], signal[valid], beta)
        same(saved['candidate'], p, name + '/independent prediction')
        exact(p[signal[valid] == 0], a['clubs'][valid][signal[valid] == 0], name + '/neutral rows')
        base, y = a['clubs'][valid], a['y'][valid]
        expected_base, expected_candidate = metrics(y, base, base), metrics(y, p, base)
        entry = results['folds'][name]
        same(entry['baseline'], expected_base, name + '/baseline metrics and calibration')
        same(entry['candidate'], expected_candidate, name + '/candidate metrics and calibration')
        passes = (expected_candidate['logloss'] < expected_base['logloss'] and
                  expected_candidate['correct'] >= expected_base['correct'])
        assert entry['passes'] == passes, name + '/strict fold gate'
        same(entry['paired'], paired(y, p, base, a['days'][valid], fold), name + '/paired ISO-week interval')
        assert set(entry['by_league']) == set(a['leagues'][valid])
        for league, item in entry['by_league'].items():
            subset = a['leagues'][valid] == league
            same(item, dict(baseline=metrics(y[subset], base[subset]),
                            candidate=metrics(y[subset], p[subset])), name + '/league/' + league)
        fold_summary[name] = dict(train_matches=len(train), validation_matches=len(valid), beta=beta,
                                  independent_beta=independent_beta, kkt_residual=residual,
                                  baseline_nll=expected_base['logloss'], candidate_nll=expected_candidate['logloss'],
                                  baseline_correct=expected_base['correct'], candidate_correct=expected_candidate['correct'],
                                  passes=passes, future_labels_leave_fit_unchanged=True)
        checked.append(name)
        all_y.append(y)
        all_base.append(base)
        all_p.append(p)
    assert {path.name.removesuffix('_model.json') for path in evaluation.glob('*_model.json')} == set(checked)
    assert {path.name.removesuffix('_predictions.npz') for path in evaluation.glob('*_predictions.npz')} == set(checked)
    y, base, p = np.concatenate(all_y), np.vstack(all_base), np.vstack(all_p)
    same(results['combined'], dict(baseline=metrics(y, base, base), candidate=metrics(y, p, base)), 'combined metrics')
    passes = all(fold['passes'] for fold in fold_summary.values())
    expected_decision = dict(
        status='historical_pass_requires_prospective' if passes else 'rejected_branch_closed',
        selected_policy='candidate_pending_prospective' if passes else 'clubs',
        aggregate_xg_profile_branch='pending_prospective' if passes else 'closed',
        production_changed=False,
        reason='Everyfoldpassedthefrozenrule.' if passes else
        'Atleastonefoldfailedthefrozenrule.Noadditionalvariantauthorizedbythisprotocol.')
    same(decision, expected_decision, 'frozen branch decision')
    same(results['decision'], decision, 'report decision identity')
    return dict(status='passed', created_at_utc=datetime.now(timezone.utc).isoformat(),
                verifier_sha256=digest(Path(__file__)), source_data_sha256=SOURCE_DIGEST,
                prepared_data_sha256=prep['data_sha256'], plan_sha256=prep['plan_sha256'],
                evaluation_sha256={path.name: digest(path) for path in sorted(evaluation.iterdir()) if path.is_file()},
                examples=3990, no2026evaluated=True, production_changed=False,
                independent_scalar_optimizations=2, folds=fold_summary, decision=expected_decision,
                checks=['frozen source, code and prepared hashes', 'exact source universe and score-derived labels',
                        'eight named xG fields only', 'exact beta-zero and zero-signal baselines',
                        'chronological fit and validation identities', 'independent scalar optimum and finite-difference gradient',
                        'positive Hessian and bounded KKT', 'future labels excluded from fit',
                        'independent multiplicative probabilities', 'NLL, Brier, accuracy and calibration bins',
                        'per-league metrics and confidence thresholds', 'paired complete-ISO-week bootstrap',
                        'per-fold strict gate and branch decision'],
                limitation='Frozen retrospective xG ordering verified by source identity; historical publication availability is not established.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = audit(args.directory)
    if args.output:
        with args.output.open('x', encoding='utf-8') as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False, allow_nan=False)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
