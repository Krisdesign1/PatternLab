"""Independently audit saved halftime learning artifacts without refitting.

This verifier deliberately imports neither the experiment nor its model module.
It only reads the frozen research dataset and saved artifacts, then reconstructs
fit membership, labels, preprocessing, predictions, objectives and scored losses.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
import argparse
import json

import numpy as np
from scipy.special import gammaln
from scipy.stats import binom, poisson, skellam


ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_text())


def same(actual, expected, label, atol=2e-10):
    try:
        np.testing.assert_allclose(actual, expected, atol=atol, rtol=2e-10)
    except AssertionError as exc:
        raise AssertionError(label + ': ' + str(exc)) from exc


def exact(actual, expected, label):
    try:
        np.testing.assert_array_equal(actual, expected)
    except AssertionError as exc:
        raise AssertionError(label + ': ' + str(exc)) from exc


def sample(days, leagues, fraction, seed):
    strata = defaultdict(set)
    for day, league in zip(days, leagues):
        strata[str(league), str(day)[:7]].add(str(day))
    chosen = set()
    for (league, month), dates in sorted(strata.items()):
        rank = sorted(dates, key=lambda day: (sha256(
            f'{seed}|{league}|{month}|{day}'.encode()).digest(), day))
        chosen.update((league, day) for day in rank[:int(fraction * len(rank) + .5)])
    return np.asarray([i for i, pair in enumerate(zip(leagues, days))
                       if tuple(map(str, pair)) in chosen], dtype=int)


def shuffle(goals, days, leagues, seed):
    strata = defaultdict(list)
    for i, (day, league) in enumerate(zip(days, leagues)):
        strata[str(league), str(day)[:7]].append(i)
    rng = np.random.default_rng(seed)
    result = goals.copy()
    for _, rows in sorted(strata.items()):
        result[rows] = goals[rng.permutation(rows)]
    return result


def synthetic(train_x, valid_x, seed):
    center, scale = train_x[:, 16].mean(), train_x[:, 16].std()
    rng = np.random.default_rng(seed)
    labels, laws = [], []
    for x in (train_x, valid_x):
        z = np.clip((x[:, 16] - center) / scale, -3, 3)
        law = np.column_stack((1.2 * np.exp(.5 * z), 1.5 * np.exp(-.5 * z)))
        laws.append(law)
        labels.append(rng.poisson(law))
    return labels, laws


def probabilities(means):
    first, second = means.T
    p = np.column_stack((skellam.sf(0, first, second),
                         skellam.pmf(0, first, second),
                         skellam.cdf(-1, first, second)))
    p = np.maximum(p, 1e-15)
    return p / p.sum(1)[:, None]


def losses(goals, means):
    total, mu = goals.sum(1), means.sum(1)
    y = np.where(goals[:, 0] > goals[:, 1], 0,
                 np.where(goals[:, 0] == goals[:, 1], 1, 2))
    out = dict(class_nll=-np.log(probabilities(means)[np.arange(len(y)), y]),
               total_nll=-poisson.logpmf(total, mu),
               allocation_nll=-binom.logpmf(goals[:, 0], total, means[:, 0] / mu),
               joint_nll=-poisson.logpmf(goals, means).sum(1))
    same(out['total_nll'] + out['allocation_nll'], out['joint_nll'], 'NLL identity')
    return out


def check_scores(saved, goals, means, label):
    l = losses(goals, means)
    for key, values in l.items():
        metric = 'class_logloss' if key == 'class_nll' else key
        same(saved[metric], values.mean(), label + '/' + metric)
    same(saved['mean_predicted_halves'], means.mean(0), label + '/mean prediction')
    same(saved['mean_observed_halves'], goals.mean(0), label + '/mean target')
    same(saved['total_mae'], np.abs(goals.sum(1) - means.sum(1)).mean(), label + '/MAE')
    assert saved['matches'] == len(goals), label + '/match count'


def check_model(model, x, goals, leagues, saved_train_means, saved_valid_means, valid_x, valid_leagues):
    assert model['version'] == 'halftime-count-ridge-v1'
    assert model['training_matches'] == len(x) and model['feature_count'] == x.shape[1]
    assert model['penalty'] == 30 and model['league_intercept_penalty'] == 1
    center, scale = x.mean(0), x.std(0)
    constant = np.ptp(x, axis=0) == 0
    center[constant], scale[constant] = x[0, constant], 1
    same(model['center'], center, 'fit-only center')
    same(model['scale'], scale, 'fit-only scale')
    levels = sorted(set(map(str, leagues)))
    assert model['league_levels'] == levels
    coefficients = np.asarray(model['coefficients'])
    assert np.isfinite(coefficients).all()

    def design(values, ids):
        result = np.zeros((len(values), len(levels) + values.shape[1]))
        result[np.arange(len(values)), [levels.index(str(l)) for l in ids]] = 1
        result[:, len(levels):] = (values - center) / scale
        return result

    train_design = design(x, leagues)
    for matrix, saved in ((train_design, saved_train_means),
                          (design(valid_x, valid_leagues), saved_valid_means)):
        predicted = np.exp(np.clip(matrix @ coefficients.T, np.log(.01), np.log(20)))
        same(saved, predicted, 'prediction reconstructed from fitted coefficients')
    ridge = np.r_[np.ones(len(levels)), np.full(x.shape[1], 30.)]
    for half in (0, 1):
        beta, target = coefficients[half], goals[:, half]
        eta = train_design @ beta
        excess = np.maximum(eta - 30, 0)
        intensity = np.exp(np.minimum(eta, 30))
        derivative = intensity * (1 + excess)
        intensity *= 1 + excess + .5 * excess ** 2
        gradient = (train_design.T @ (derivative - target) + ridge * beta) / len(x)
        objective = (intensity - target * eta + gammaln(target + 1)).mean()
        objective += .5 * np.dot(ridge, beta * beta) / len(x)
        assert model['diagnostics'][half]['success']
        same(model['diagnostics'][half]['penalized_mean_nll'], objective, 'fit objective')
        same(model['diagnostics'][half]['max_abs_gradient'], np.abs(gradient).max(), 'fit gradient')


def audit(directory):
    prepared, plan = read(directory / 'preparation.json'), read(directory / 'plan.json')
    for filename, key in (('dataset.npz', 'data_sha256'), ('plan.json', 'plan_sha256')):
        assert sha256((directory / filename).read_bytes()).hexdigest() == prepared[key]
    assert sha256((ROOT / 'halftime_learning_diagnostic.py').read_bytes()).hexdigest() == plan['code_sha256']
    for filename, digest in plan['frozen_prior_code'].items():
        assert sha256((ROOT / filename).read_bytes()).hexdigest() == digest, filename
    with np.load(directory / 'dataset.npz', allow_pickle=False) as f:
        a = {key: f[key] for key in f.files}
    assert len(a['ids']) == 3990 and len(set(a['ids'])) == 3990
    assert (a['days'] < '2026-01-01').all()
    assert (a['warmup_days'] < '2024-01-01').all() and len(a['warmup_ids']) == 1047
    assert set(a['warmup_leagues']) == {'39', '61'}
    assert not set(a['ids']) & set(a['warmup_ids'])
    exact(a['y'], np.where(a['goals'][:, 0] > a['goals'][:, 1], 0,
                          np.where(a['goals'][:, 0] == a['goals'][:, 1], 1, 2)), 'prepared labels')
    evaluation = directory / 'evaluation'
    results = read(evaluation / 'results.json')
    assert results['no2026evaluated'] and not results['production_changed']
    entries = {entry['name']: entry for entry in results['entries']}
    assert len(entries) == len(results['entries']) == 88
    checked, full_means, sizes = [], {}, {}
    for fnum, fold in enumerate(plan['validation_folds']):
        tr = np.flatnonzero(a['days'] < fold['fit_before'])
        va = np.flatnonzero((a['days'] >= fold['start']) & (a['days'] < fold['end']))
        assert not set(tr) & set(va)
        labels, truth = synthetic(a['rich'][tr], a['rich'][va], 2026102799 + fnum)
        specifications = []
        for fraction in plan['fractions']:
            for seed in (plan['seeds'][:1] if fraction == 1 else plan['seeds']):
                ix = tr[sample(a['days'][tr], a['leagues'][tr], fraction, seed)]
                sizes.setdefault(fold['name'], {}).setdefault(str(fraction), []).append(len(ix))
                for family in ('scores', 'rich'):
                    name = fold['name'] + f'_curve_{int(fraction * 100)}_{seed}_{family}'
                    specifications.append((name, family, ix, a['goals'][ix], a['goals'][va], False))
        specifications.append((fold['name'] + '_league_anchor', 'league', tr, a['goals'][tr], a['goals'][va], False))
        for family in ('scores', 'rich'):
            specifications.append((fold['name'] + '_old_labels_' + family, family, tr, a['goals'][tr], a['goals'][va], True))
        for seed in (2026102711, 2026102712, 2026102713):
            target = shuffle(a['goals'][tr], a['days'][tr], a['leagues'][tr], seed)
            for family in ('scores', 'rich'):
                specifications.append((fold['name'] + f'_null_{seed}_{family}', family, tr, target, a['goals'][va], False))
        for family in ('league', 'scores', 'rich'):
            specifications.append((fold['name'] + '_synthetic_' + family, family, tr, labels[0], labels[1], False))
        for name, family, ix, target, valid_target, old in specifications:
            entry = entries[name]
            x = a[family][ix] if family != 'league' else np.empty((len(ix), 0))
            vx = a[family][va] if family != 'league' else np.empty((len(va), 0))
            ids, leagues = a['ids'][ix], a['leagues'][ix]
            if old:
                x = np.vstack((a['warmup_' + family], x))
                target = np.vstack((a['warmup_goals'], target))
                ids = np.r_[a['warmup_ids'], ids]
                leagues = np.r_[a['warmup_leagues'], leagues]
            assert not set(ids) & set(a['ids'][va]), name + '/fit-validation overlap'
            with np.load(evaluation / (name + '_predictions.npz'), allow_pickle=False) as f:
                saved = {key: f[key] for key in f.files}
            exact(saved['train_ids'], ids, name + '/fit IDs')
            exact(saved['train_goals'], target, name + '/fit targets')
            exact(saved['valid_ids'], a['ids'][va], name + '/fixed validation IDs')
            exact(saved['goals'], valid_target, name + '/validation targets')
            model = read(evaluation / (name + '_model.json'))
            check_model(model, x, target, leagues, saved['train_means'], saved['means'], vx, a['leagues'][va])
            same(saved['probabilities'], probabilities(saved['means']), name + '/prematch probabilities')
            check_scores(entry['train'], target, saved['train_means'], name + '/train')
            check_scores(entry['validation'], valid_target, saved['means'], name + '/validation')
            assert entry['fit_matches'] == len(ids)
            for league, metrics in entry['by_league'].items():
                mask = a['leagues'][va] == league
                check_scores(metrics, valid_target[mask], saved['means'][mask], name + '/league/' + league)
            if '_curve_100_' in name:
                full_means[fold['name'], family] = saved['means']
            checked.append(name)
        factors = results['factors'][fold['name']]
        for volume in ('scores', 'rich'):
            for allocation in ('scores', 'rich'):
                mu = full_means[fold['name'], volume].sum(1)
                law = full_means[fold['name'], allocation]
                share = law[:, 0] / law.sum(1)
                hybrid = np.column_stack((mu * share, mu * (1 - share)))
                key = 'volume_' + volume + '_allocation_' + allocation
                check_scores(factors['hybrids'][key], a['goals'][va], hybrid, fold['name'] + '/' + key)
        check_scores(factors['synthetic_true_law_score'], labels[1], truth[1], fold['name'] + '/synthetic truth')
        rich_loss = losses(a['goals'][va], full_means[fold['name'], 'rich'])
        score_loss = losses(a['goals'][va], full_means[fold['name'], 'scores'])
        blocks = defaultdict(list)
        for index, day in enumerate(a['days'][va]):
            dt = datetime.fromisoformat(str(day))
            monday = dt - timedelta(days=dt.weekday())
            if monday >= datetime.fromisoformat(fold['start']) and monday + timedelta(days=7) <= datetime.fromisoformat(fold['end']):
                blocks[monday.date().isoformat()].append(index)
        rows = list(blocks.values())
        draws = np.random.default_rng(20261027).integers(0, len(rows), (2000, len(rows))) if len(rows) >= 20 else None
        for key, rich_values in rich_loss.items():
            gain = score_loss[key] - rich_values
            item = factors['paired_rich_vs_scores'][key]
            same(item['gain_positive_is_better'], gain.mean(), 'full gain')
            flat = [i for block in rows for i in block]
            assert item['matches_in_interval'] == len(flat) and item['closed_weeks'] == len(rows)
            same(item['gain_closed_weeks'], gain[flat].mean(), 'closed-week gain')
            if draws is not None:
                sums = np.array([gain[block].sum() for block in rows])
                counts = np.array([len(block) for block in rows])
                interval = np.quantile(sums[draws].sum(1) / counts[draws].sum(1), [.025, .975])
                same(item['interval'], interval, 'paired weekly interval')
    assert set(checked) == set(entries)
    assert {p.name[:-11] for p in evaluation.glob('*_model.json')} == set(checked)
    assert {p.name[:-16] for p in evaluation.glob('*_predictions.npz')} == set(checked)
    return dict(status='passed',created_at_utc=datetime.now(timezone.utc).isoformat(),
                verifier_sha256=sha256(Path(__file__).read_bytes()).hexdigest(), models_checked=len(checked),
                no_refits=True, no2026evaluated=True, curve_fit_sizes=sizes,
                checks=['frozen hashes', 'fit and validation identities', 'actual/null/synthetic labels',
                        'fit-only standardization', 'coefficient predictions', 'penalized objectives and gradients',
                        'joint/total/allocation losses', 'per-league scores', 'prematch factor hybrids',
                        'paired weekly bootstrap'])


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
