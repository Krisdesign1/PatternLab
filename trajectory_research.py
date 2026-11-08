"""Isolated retrospective experiment: club history summaries, bags and order.

This module deliberately does not import the app, API client or model publishers.
Source SQLite is read-only; outputs and fitted models live only in a research run.
"""
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
import gc
import gzip
import json
import sqlite3

import trajectory_research_features as features


ROOT = Path(__file__).resolve().parent
VERSION = 'club-trajectory-experiment-v1'
MODES = ('summary', 'bag', 'ordered')
FROZEN_FILES = ('catboost_scenarios.py', 'prospective_models.py', 'match_predictor.py',
                'scenario_features.py', 'scenario_predictor.py', 'league_patterns.py',
                'candidate_tracking.py', 'contextual_scenarios.py', 'club_identity.py')
CODE_FILES = ('trajectory_research.py', 'trajectory_research_features.py')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hashes(names):
    return {n: sha256((ROOT/n).read_bytes()).hexdigest() for n in names}


def _write(path, value):
    with path.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)


def new_plan(cutoff='2026-10-25', *, now=None):
    from importlib.metadata import version
    current = now or datetime.now(timezone.utc)
    if date.fromisoformat(cutoff) > current.astimezone(timezone.utc).date():
        raise ValueError('Research cutoff cannot include future dates.')
    return dict(version=VERSION, created_at=current.isoformat(), cutoff_exclusive_utc=cutoff,
                feature_version=features.FEATURE_VERSION,
                status='EXPLORATORY: previously examined historical periods, not prospective confirmation',
                objective='Does exact order improve individual probabilities beyond the same unordered history vectors?',
                partitions=dict(train_before='2025-01-01', validation_start='2025-01-01',
                                test_start='2026-01-01', test_end_exclusive=cutoff),
                cohort=dict(league_training_rows_before_2025=500, minimum_past_league=30,
                            minimum_past_overall_each_club=10, minimum_past_role_each_club=3),
                views=dict(summary='Context plus symmetric summaries of the same last ten records per profile.',
                           bag='Summary plus complete records canonically sorted by values; no per-record date or ID.',
                           ordered='Summary plus the same complete records in reverse chronological order.'),
                learning=dict(params=dict(iterations=200, depth=4, learning_rate=.05, l2_leaf_reg=10),
                              common=dict(loss_function='MultiClass', class_names=list(features.LABELS),
                                          random_seed=20261025, thread_count=2, has_time=True,
                                          allow_writing_files=False, verbose=False),
                              selection='Fixed identical parameters: no search, early stopping, calibration or threshold tuning.',
                              validation='Fit train, report 2025, no parameter changes from validation scores.',
                              evaluation='Refit train+validation; model parameters frozen through 2026; lagged histories update by UTC day.'),
                metrics=dict(primary='Paired multiclass logloss, ordered versus bag',
                             secondary=['multiclass Brier', 'argmax accuracy', 'class breakdown', 'fixed threshold coverage'],
                             thresholds=[.5, .6, .7],
                             uncertainty='95% paired UTC-week cluster bootstrap, 2000 replicates seed20261025; approximate, not anytime-valid or causal.'),
                safeguards=['one distinct provider match ID', 'all targets emitted before whole-day history update',
                            'same cohort and availability cutoff for all methods', 'no target score or total in features',
                            'same past vectors for bag and ordered', 'API-Football only, club IDs scoped by league',
                            'no API, no app model publication, no journal writes', 'frozen serving files unchanged'],
                limitations=['Latest historical projections do not prove original real-time availability.',
                             'Single fixed model capacity and seed cannot exhaust all sequence methods.',
                             'Windows, current rest and context are shared; bag removes explicit within-window chronology only.',
                             'Retrospective known-data selection bias remains; no 70% guarantee or automatic promotion.'],
                versions={name: version(name) for name in ('catboost', 'numpy')},
                code_hashes=_hashes(CODE_FILES), frozen_code_hashes=_hashes(FROZEN_FILES))


def prepare(directory, cutoff='2026-10-25'):
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=False)
    plan = new_plan(cutoff)
    _write(path/'plan.json', plan)
    return path


def _source_snapshot(database, path, cutoff):
    uri = Path(database).resolve().as_uri()+'?mode=ro'
    con = sqlite3.connect(uri, uri=True)
    con.execute('PRAGMA query_only=ON')
    con.execute('BEGIN')
    rows, fingerprint = [], sha256()
    try:
        columns = {r[1] for r in con.execute('PRAGMA table_info(fixtures)')}
        if not {'match_id', 'normalized_json', 'kickoff_utc'} <= columns:
            raise ValueError('Unexpected fixture schema.')
        with gzip.open(path/'source.ndjson.gz', 'xt', encoding='utf-8') as handle:
            for mid, raw in con.execute('SELECT match_id, normalized_json FROM fixtures '
                    'WHERE normalized_json IS NOT NULL AND kickoff_utc < ? ORDER BY kickoff_utc, match_id',
                    (cutoff+'T00:00:00',)):
                row = json.loads(raw)
                if str(row.get('match_id')) != str(mid) or row.get('timestamp', '')[:10] >= cutoff:
                    raise ValueError('Fixture identity or cutoff mismatch.')
                rows.append(row)
                fingerprint.update(str(mid).encode()+b'\0'+raw.encode()+b'\n')
                handle.write(raw+'\n')
    finally:
        con.rollback()
        con.close()
    return rows, fingerprint.hexdigest()


def partition(day, plan):
    limits = plan['partitions']
    if day < limits['train_before']:
        return 'train'
    if day < limits['test_start']:
        return 'validation'
    if day < limits['test_end_exclusive']:
        return 'test'
    return None


def eligible(example, plan):
    context, rule = example['context'], plan['cohort']
    return (context['league_count'] >= rule['minimum_past_league']
            and min(context['home_overall_count'], context['away_overall_count']) >= rule['minimum_past_overall_each_club']
            and min(context['home_role_count'], context['away_role_count']) >= rule['minimum_past_role_each_club'])


def summarize(actual, predictions, baseline=None, thresholds=(.5, .6, .7)):
    import numpy as np
    p = np.asarray(predictions, dtype=float)
    y = np.asarray(actual, dtype=int)
    if len(y) == 0:
        return dict(matches=0)
    if p.shape != (len(y), 3) or not np.isfinite(p).all() or (p <= 0).any() or not np.allclose(p.sum(1), 1):
        raise ValueError('Invalid probability matrix.')
    winner = p.argmax(1)
    loss = -np.log(np.maximum(p[np.arange(len(y)), y], 1e-15))
    brier = (p*p).sum(1)-2*p[np.arange(len(y)), y]+1
    confidence = p.max(1)
    result = dict(matches=len(y), correct=int((winner == y).sum()), accuracy=float((winner == y).mean()),
                  logloss=float(loss.mean()), brier=float(brier.mean()),
                  favorites={label: int((winner == i).sum()) for i, label in enumerate(features.LABELS)},
                  actual={label: int((y == i).sum()) for i, label in enumerate(features.LABELS)})
    bins = []
    for low in thresholds:
        mask = confidence >= low
        n = int(mask.sum())
        row = dict(threshold=low, matches=n, coverage=n/len(y),
                   accuracy=float((winner[mask] == y[mask]).mean()) if n else None,
                   mean_probability=float(confidence[mask].mean()) if n else None)
        if baseline is not None and n:
            row['baseline_accuracy_same_matches'] = float((np.asarray(baseline)[mask].argmax(1) == y[mask]).mean())
        bins.append(row)
    result['thresholds'] = bins
    return result


def _pool(base, vectors, categories, indices, labels):
    import numpy as np
    from catboost import FeaturesData, Pool
    numeric = np.asarray(base[indices], dtype=np.float32)
    if vectors is not None:
        numeric = np.concatenate((numeric, vectors[indices]), axis=1)
    numeric = np.ascontiguousarray(numeric)
    cat = np.asarray([[value.encode('ascii') for value in categories[i]] for i in indices], dtype=object)
    targets = [features.LABELS[int(i)] for i in labels[indices]]
    return Pool(FeaturesData(num_feature_data=numeric, cat_feature_data=cat), label=targets)


def paired_weeks(metadata, actual, a, b):
    """Approximate paired interval; positive gain means b has lower logloss."""
    import numpy as np
    y = np.asarray(actual)
    la = -np.log(np.maximum(a[np.arange(len(y)), y], 1e-15))
    lb = -np.log(np.maximum(b[np.arange(len(y)), y], 1e-15))
    groups = defaultdict(list)
    for i, row in enumerate(metadata):
        day = date.fromisoformat(row['day'])
        groups[(day.isocalendar().year, day.isocalendar().week)].append(i)
    blocks = list(groups.values())
    sums = np.array([float((la-lb)[idx].sum()) for idx in blocks])
    sizes = np.array([len(idx) for idx in blocks])
    rng = np.random.default_rng(20261025)
    selected = rng.integers(0, len(blocks), size=(2000, len(blocks)))
    bootstrap = sums[selected].sum(1)/sizes[selected].sum(1)
    lo, hi = np.quantile(bootstrap, [.025, .975])
    return dict(matches=len(y), weeks=len(blocks), mean_logloss_gain_b_over_a=float((la-lb).mean()),
                approximate_95_interval=[float(lo), float(hi)],
                warning='UTC-week resampling is approximate; cross-week dependencies and selection are not removed.')


def run(directory, database=None):
    import numpy as np
    from catboost import CatBoostClassifier
    from importlib.metadata import version
    path = Path(directory)
    plan = json.loads((path/'plan.json').read_text())
    if plan != dict(plan, code_hashes=_hashes(CODE_FILES), frozen_code_hashes=_hashes(FROZEN_FILES)):
        raise ValueError('Implementation changed after the research plan was created.')
    if plan['versions'] != {name: version(name) for name in plan['versions']}:
        raise ValueError('Package versions changed after the research plan was created.')
    if (path/'results.json').exists():
        raise ValueError('An experiment result is immutable; create another run.')
    _write(path/'started.json', dict(started_at=datetime.now(timezone.utc).isoformat(), plan_sha256=sha256((path/'plan.json').read_bytes()).hexdigest()))
    rows, fingerprint = _source_snapshot(database or ROOT/'data/api_football/fixtures.sqlite3', path, plan['cutoff_exclusive_utc'])
    source_count = len(rows)
    counts = Counter(str(r['league']) for r in rows if r['timestamp'][:10] < plan['partitions']['train_before'])
    leagues = {league for league, n in counts.items() if n >= plan['cohort']['league_training_rows_before_2025']}
    selected_rows = [r for r in rows if str(r['league']) in leagues]
    del rows
    numeric, _ = features.columns('summary')
    n_max = len(selected_rows)
    base = np.memmap(path/'summary.float32', dtype=np.float32, mode='w+', shape=(n_max, len(numeric)))
    bag = np.memmap(path/'bag.float32', dtype=np.float32, mode='w+', shape=(n_max, len(features.VECTOR_COLUMNS)))
    ordered = np.memmap(path/'ordered.float32', dtype=np.float32, mode='w+', shape=bag.shape)
    metadata, categories = [], []
    excluded = Counter()
    for example in features.iter_examples(selected_rows):
        if not eligible(example, plan):
            excluded[partition(example['day'], plan)] += 1
            continue
        i = len(metadata)
        values = dict(example['context'], **example['summary'])
        base[i] = [values[name] for name in numeric]
        bag[i] = [example['bag'][name] for name in features.VECTOR_COLUMNS]
        ordered[i] = [example['ordered'][name] for name in features.VECTOR_COLUMNS]
        categories.append(tuple(example['context'][name] for name in features.CATEGORICAL_COLUMNS))
        metadata.append(dict(match_id=example['match_id'], day=example['day'], league=example['league'],
                             actual=example['actual'], partition=partition(example['day'], plan),
                             home_support=example['context']['home_overall_count'],
                             away_support=example['context']['away_overall_count']))
        if len(metadata) % 10000 == 0:
            print(f'Historical features: {len(metadata)} eligible fixtures', flush=True)
    del selected_rows
    for array in (base, bag, ordered):
        array.flush()
    labels = np.array([features.LABELS.index(r['actual']) for r in metadata], dtype=np.int32)
    indices = {part: np.array([i for i, row in enumerate(metadata) if row['partition'] == part])
               for part in ('train', 'validation', 'test')}
    if any(len(x) < 100 for x in indices.values()):
        raise ValueError('Not enough common targets in one chronological partition.')
    with gzip.open(path/'cohort.ndjson.gz', 'xt', encoding='utf-8') as handle:
        for row in metadata:
            handle.write(_json(row)+'\n')
    class_columns = [numeric.index('league_rate_'+tag) for tag in ('first', 'equal', 'second')]
    baseline = np.asarray(base[:len(metadata), class_columns], dtype=float)
    cohort = dict(source_matches=source_count, source_fingerprint=fingerprint, eligible_leagues=len(leagues),
                  eligible_matches=len(metadata), excluded_insufficient_past=dict(excluded),
                  partitions={part: dict(matches=len(ids), start=metadata[ids[0]]['day'], end=metadata[ids[-1]]['day'],
                                         fingerprint=sha256(_json([metadata[i]['match_id'] for i in ids]).encode()).hexdigest())
                              for part, ids in indices.items()},
                  numeric_columns={mode: len(features.columns(mode)[0]) for mode in MODES})
    _write(path/'cohort.json', cohort)
    predictions, metrics, models = {}, {}, {}
    for phase in ('validation', 'test'):
        fit_indices = indices['train'] if phase == 'validation' else np.concatenate((indices['train'], indices['validation']))
        target_indices = indices[phase]
        y = labels[target_indices]
        preds = {'frequency': baseline[target_indices]}
        for mode in MODES:
            print(f'Fitting {phase}/{mode}: {len(fit_indices)} train, {len(target_indices)} evaluate', flush=True)
            vectors = None if mode == 'summary' else bag if mode == 'bag' else ordered
            train_pool = _pool(base, vectors, categories, fit_indices, labels)
            model = CatBoostClassifier(**plan['learning']['params'], **plan['learning']['common'])
            model.fit(train_pool)
            del train_pool
            target_pool = _pool(base, vectors, categories, target_indices, labels)
            raw = model.predict_proba(target_pool)
            if list(map(str, model.classes_)) != list(features.LABELS):
                raise ValueError('Unexpected classifier label order.')
            preds[mode] = np.asarray(raw, dtype=float)
            model_path = path/f'{phase}_{mode}.cbm'
            model.save_model(str(model_path))
            models[f'{phase}_{mode}'] = sha256(model_path.read_bytes()).hexdigest()
            current = summarize(y, preds[mode], preds['frequency'], plan['metrics']['thresholds'])
            print(f"Completed {phase}/{mode}: logloss={current['logloss']:.6f}, accuracy={current['accuracy']:.4%}", flush=True)
            del model, target_pool
            gc.collect()
        predictions[phase] = preds
        metrics[phase] = {mode: summarize(y, p, preds['frequency'], plan['metrics']['thresholds']) for mode, p in preds.items()}
        np.savez_compressed(path/f'{phase}_predictions.npz', actual=y,
                            match_ids=np.array([metadata[i]['match_id'] for i in target_indices]), **preds)
        _write(path/f'{phase}_metrics.json', metrics[phase])
    test_ids = indices['test']
    test_meta, y = [metadata[i] for i in test_ids], labels[test_ids]
    preds = predictions['test']
    comparisons = {f'{a}_vs_{b}': paired_weeks(test_meta, y, preds[a], preds[b])
                   for a, b in (('frequency', 'summary'), ('summary', 'bag'), ('bag', 'ordered'))}
    by_league = {}
    for league in sorted({row['league'] for row in test_meta}, key=int):
        selected = np.array([i for i, row in enumerate(test_meta) if row['league'] == league])
        by_league[league] = {name: summarize(y[selected], p[selected], thresholds=()) for name, p in preds.items()}
    if (_hashes(FROZEN_FILES) != plan['frozen_code_hashes']
            or _hashes(CODE_FILES) != plan['code_hashes']):
        raise ValueError('Serving or experiment code changed during the isolated experiment.')
    result = dict(version=VERSION, completed_at=datetime.now(timezone.utc).isoformat(), plan_sha256=sha256((path/'plan.json').read_bytes()).hexdigest(),
                  cohort=cohort, metrics=metrics, comparisons=comparisons, test_by_league=by_league,
                  model_hashes=models, frozen_code_unchanged=True, limitations=plan['limitations'],
                  interpretation='Exploratory representation comparison only. Not a prospective study, not proof of sequence causality, not automatic serving promotion.')
    _write(path/'results.json', result)
    # Inputs are reproducible from the immutable source snapshot; discard large scratch matrices.
    del base, bag, ordered
    gc.collect()
    for name in ('summary.float32', 'bag.float32', 'ordered.float32'):
        (path/name).unlink()
    print(_json(dict(result_path=str(path/'results.json'), metrics=metrics['test'], comparisons=comparisons)), flush=True)
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--cutoff', default='2026-10-25')
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    if not (args.directory/'plan.json').exists():
        prepare(args.directory, args.cutoff)
        print(f'Research plan recorded: {args.directory / "plan.json"}', flush=True)
    if args.run:
        run(args.directory)


if __name__ == '__main__':
    main()
