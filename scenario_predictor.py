"""League-specific joint scenario estimates, evaluated on strictly earlier days."""
from collections import Counter
from itertools import product
from math import log
from league_model import VALUES
from coupon_sizes import validate_size, top_independent

SCENARIOS = tuple(product(VALUES, repeat=4))
MIN_TRAIN = 20
MIN_TEST = 10
PRIOR_WEIGHT = 10.0


def fit_sequences(sequences, size):
    validate_size(size)
    sequences = [tuple(s) for s in sequences]
    if any(len(s) != size or any(v not in VALUES for v in s) for s in sequences):
        raise ValueError('Scénario invalide ou de taille différente')
    counts = Counter(sequences)
    n = len(sequences)
    positions = [Counter(s[i] for s in sequences) for i in range(size)]
    prior = [{v: (c[v]+1)/(n+3) for v in VALUES} for c in positions]
    return counts, n, prior


def probability(fit, sequence, baseline=False):
    counts, n, prior = fit
    p = 1.0
    for dist, v in zip(prior, sequence):
        p *= dist[v]
    return p if baseline else (counts[tuple(sequence)] + PRIOR_WEIGHT*p)/(n+PRIOR_WEIGHT)


def best_sequences(fit, limit=3, baseline=False):
    # An unseen sequence keeps its prior rank. Top k must be among the top k
    # prior sequences or the observed sequences, regardless of coupon length.
    prior_best = top_independent(fit[2], limit)
    candidates = {r['sequence'] for r in prior_best}
    if not baseline:
        candidates.update(fit[0])
    return sorted(candidates, key=lambda s: (-probability(fit, s, baseline), s))[:limit]


def distributions(sequences):
    """Compatibility helper: complete four-match distributions only."""
    fit = fit_sequences(sequences, 4)
    return ({s: probability(fit, s) for s in SCENARIOS},
            {s: probability(fit, s, True) for s in SCENARIOS})


def ranked(probabilities):
    return sorted(probabilities, key=lambda s: (-probabilities[s], s))


def predict_league(result):
    blocks = result['blocks']
    size = result.get('size', 4)
    validate_size(size)
    trials = []
    # Entire earlier calendar days only: kickoff is not a result-availability time.
    for target in blocks:
        start_day = min(r['timestamp'][:10] for r in target['matches'])
        training = [b for b in blocks
                    if max(r['timestamp'][:10] for r in b['matches']) < start_day]
        if len(training) < MIN_TRAIN:
            continue
        fit = fit_sequences([b['sequence'] for b in training], size)
        order, reference = best_sequences(fit), best_sequences(fit, baseline=True)
        actual = target['sequence']
        trials.append({'group': target['number'], 'day': start_day,
                       'training': len(training), 'top1': actual == order[0],
                       'top3': actual in order[:3], 'baseline_top1': actual == reference[0],
                       'baseline_top3': actual in reference[:3],
                       'loss': -log(probability(fit, actual)), 'baseline_loss': -log(probability(fit, actual, True))})
    fit = fit_sequences([b['sequence'] for b in blocks], size)
    return {'size': size, 'training': len(blocks), 'eligible': len(blocks) >= MIN_TRAIN,
            'tested': len(trials) >= MIN_TEST, 'trials': trials,
            'ranking': [{'sequence': s, 'probability': probability(fit, s)} for s in best_sequences(fit)],
            'latest': max((r['timestamp'] for r in result['matches']), default=None)}
