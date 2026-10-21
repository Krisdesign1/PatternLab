"""Fixed, experimental half-goal model. Never trains on the target calendar day."""
from collections import Counter
from datetime import date
from math import exp, log, prod
from coupon_sizes import validate_size, top_independent
from league_model import VALUES, score, outcome

MIN_LEAGUE = 30
MIN_DAYS = 3
MIN_TEAM = 3
SHRINK = 8
VERSION = 'half-goals-v1'


def training_rows(rows, league, before):
    date.fromisoformat(before)
    selected = {}
    for row in rows:
        if row['league'] != league or row['timestamp'][:10] >= before:
            continue
        mid = str(row['match_id'])
        if mid in selected and selected[mid] != row:
            raise ValueError('Identifiant contradictoire')
        if outcome(score(row['ht']), score(row['ft'])) != row['value']:
            raise ValueError('Score et résultat contradictoires')
        selected[mid] = row
    return sorted(selected.values(), key=lambda r: (r['timestamp'], int(r['match_id'])))


def goals(row):
    ht, ft = score(row['ht']), score(row['ft'])
    return (ht[0], ft[0]-ht[0]), (ht[1], ft[1]-ht[1])


def profile(rows, team, venue):
    matches = [r for r in rows if r[venue] == team]
    idx = 0 if venue == 'home' else 1
    return {'matches': len(matches),
            'scored': [sum(goals(r)[idx][i] for r in matches) for i in range(2)],
            'conceded': [sum(goals(r)[1-idx][i] for r in matches) for i in range(2)]}


def poisson(mean):
    if not 0 <= mean <= 100:
        raise ValueError('Moyenne de buts hors plage prise en charge')
    terms = [exp(-mean)]
    while sum(terms) < 1-1e-12:
        terms.append(terms[-1] * mean / len(terms))
    total = sum(terms)
    return [p/total for p in terms]


def half_probabilities(first, second):
    p = dict.fromkeys(VALUES, 0.0)
    for i, a in enumerate(poisson(first)):
        for j, b in enumerate(poisson(second)):
            p['1st' if i > j else '2nd' if j > i else 'Égal'] += a*b
    return p


def estimate(rows, league, home, away, before):
    if not home or not away or home == away:
        raise ValueError('Choisis deux équipes différentes')
    train = training_rows(rows, league, before)
    n = len(train)
    counts = Counter(r['value'] for r in train)
    baseline = {v: (counts[v]+1)/(n+3) for v in VALUES}
    h, a = profile(train, home, 'home'), profile(train, away, 'away')
    eligible = n >= MIN_LEAGUE and len({r['timestamp'][:10] for r in train}) >= MIN_DAYS
    personalized = eligible and min(h['matches'], a['matches']) >= MIN_TEAM
    p = baseline.copy()
    means = None
    if personalized:
        means = []
        for i in range(2):
            home_mean = sum(goals(r)[0][i] for r in train)/n
            away_mean = sum(goals(r)[1][i] for r in train)/n
            hs = (h['scored'][i]+SHRINK*home_mean)/(h['matches']+SHRINK)
            ac = (a['conceded'][i]+SHRINK*home_mean)/(a['matches']+SHRINK)
            ass = (a['scored'][i]+SHRINK*away_mean)/(a['matches']+SHRINK)
            hc = (h['conceded'][i]+SHRINK*away_mean)/(h['matches']+SHRINK)
            means.append((hs+ac+ass+hc)/2)
        team_p = half_probabilities(*means)
        weight = min(h['matches'], a['matches'])/(min(h['matches'], a['matches'])+SHRINK)
        p = {v: weight*team_p[v]+(1-weight)*baseline[v] for v in VALUES}
    return {'eligible': eligible, 'personalized': personalized, 'probabilities': p,
            'baseline': baseline, 'training': n, 'home_profile': h, 'away_profile': a,
            'expected_goals': means, 'before': before,
            'last_training_day': train[-1]['timestamp'][:10] if train else None}


def backtest(rows, league):
    targets = sorted((r for r in rows if r['league'] == league),
                     key=lambda r: (r['timestamp'], int(r['match_id'])))
    records = []
    seen = set()
    for r in targets:
        if r['match_id'] in seen:
            continue
        seen.add(r['match_id'])
        e = estimate(rows, league, r['home'], r['away'], r['timestamp'][:10])
        if not e['eligible']:
            continue
        p, base, actual = e['probabilities'], e['baseline'], r['value']
        records.append({'match_id': r['match_id'], 'day': r['timestamp'][:10],
                        'training': e['training'], 'personalized': e['personalized'],
                        'probabilities': p, 'baseline': base,
                        'correct': max(VALUES, key=p.get) == actual,
                        'baseline_correct': max(VALUES, key=base.get) == actual,
                        'loss': -log(p[actual]), 'baseline_loss': -log(base[actual])})
    return records


def summarize(records):
    n = len(records)
    return {'tested': n, 'personalized': sum(r['personalized'] for r in records),
            'correct': sum(r['correct'] for r in records),
            'baseline_correct': sum(r['baseline_correct'] for r in records),
            'loss': sum(r['loss'] for r in records)/n if n else None,
            'baseline_loss': sum(r['baseline_loss'] for r in records)/n if n else None}


def group_scenarios(estimates, limit=81):
    validate_size(len(estimates))
    if not all(e['eligible'] for e in estimates):
        raise ValueError('Chaque match doit disposer de suffisamment de données')
    return top_independent([e['probabilities'] for e in estimates], limit)
