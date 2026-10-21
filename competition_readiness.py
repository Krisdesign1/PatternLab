"""Local eligibility checks for the statistical engine, without predictions.

The counts share the engine's source filtering, UTC day cutoff, stable ordering
and complete-block definition. They describe sufficient statistical history,
not model quality or the availability of a saved CatBoost/context artifact.
"""
from datetime import date

import catboost_scenarios as ml
from coupon_sizes import validate_size
from match_predictor import MIN_DAYS, MIN_LEAGUE
from scenario_predictor import MIN_TRAIN


VERSION = 'statistical-readiness-v1'


def assess_sizes(rows, league, sizes, before):
    """Return causal history requirements per size; no inference or API calls."""
    before = date.fromisoformat(before).isoformat()
    sizes = list(dict.fromkeys(validate_size(size) for size in sizes))
    history = ml._rows(rows, str(league), 'API-Football', before)
    n = len(history)
    days = len({row['timestamp'][:10] for row in history})
    output = []
    for size in sizes:
        groups = n // size
        missing_groups = max(0, MIN_TRAIN-groups)
        missing_days = max(0, MIN_DAYS-days)
        minimum_matches = max(MIN_LEAGUE, MIN_TRAIN*size)
        missing_matches = max(0, minimum_matches-n, missing_days)
        eligible = groups >= MIN_TRAIN and n >= MIN_LEAGUE and days >= MIN_DAYS
        if eligible:
            reason = f'{groups} groupes historiques complets sur {days} jours avant le {before} (UTC).'
        else:
            reasons = []
            if groups < MIN_TRAIN:
                reasons.append(f'{groups}/{MIN_TRAIN} groupes complets de {size} matchs')
            if n < MIN_LEAGUE:
                reasons.append(f'{n}/{MIN_LEAGUE} résultats utilisables')
            if days < MIN_DAYS:
                reasons.append(f'{days}/{MIN_DAYS} jours distincts')
            reason = 'Historique à compléter : ' + ' ; '.join(reasons) + f', avant le {before} (UTC).'
        output.append(dict(size=size, before=before, history_matches=n, history_groups=groups,
                           history_days=days, min_groups=MIN_TRAIN, min_matches=minimum_matches,
                           min_days=MIN_DAYS, missing_groups=missing_groups, missing_days=missing_days,
                           missing_matches=missing_matches, eligible=eligible, reason=reason))
    return output


def assess_history(rows, league, size, before):
    return assess_sizes(rows, league, [size], before)[0]
