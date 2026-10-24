"""Transparent input-count filters, separate from estimated probabilities.

These operational choices have not been validated as accuracy thresholds. They
never change the model, its outcome ranking, or a previously frozen forecast.
"""
VERSION = 'history-count-filter-v1'
MINIMUM = dict(league_matches=30, history_days=3, team_matches=3)
# Eight role-specific matches give the Clubs component at least half of its
# n/(n+8) blend. 100/10 are conservative operating choices, not confidence levels.
PRUDENT = dict(league_matches=100, history_days=10, team_matches=8)


def _count(value):
    return value if type(value) is int and value >= 0 else None


def counts(row):
    return dict(league_matches=_count(row.get('training')),
                history_days=_count(row.get('history_days')),
                home_matches=_count((row.get('home_profile') or {}).get('matches')),
                away_matches=_count((row.get('away_profile') or {}).get('matches')))


def assess(row, criteria=None):
    criteria = PRUDENT if criteria is None else criteria
    if (not isinstance(criteria, dict) or set(criteria) != set(MINIMUM)
            or any(type(value) is not int or value < MINIMUM[name] for name, value in criteria.items())):
        raise ValueError('Les seuils doivent respecter le minimum de calcul du modèle.')
    values = counts(row)
    team = min(values['home_matches'], values['away_matches']) if (
        values['home_matches'] is not None and values['away_matches'] is not None) else None
    observed = dict(league_matches=values['league_matches'], history_days=values['history_days'], team_matches=team)
    missing = [name for name, value in observed.items() if value is None]
    gaps = {name: criteria[name]-value for name, value in observed.items()
            if value is not None and value < criteria[name]}
    return dict(version=VERSION, criteria=dict(criteria), counts=values,
                eligible=not missing and not gaps, missing=missing, gaps=gaps)


def select(rows, criteria=None):
    """Preserve all input probabilities and order; filter only input counts."""
    return [row for row in rows if assess(row, criteria)['eligible']]
