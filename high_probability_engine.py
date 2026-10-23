"""Pure, uncapped ranking of today's API-Football club probabilities.

The underlying model is the fixed ``match_predictor`` Clubs model. This module
builds its sufficient statistics once per league instead of rescanning the full
history for every fixture. It does not fetch, persist, fit, or settle forecasts.
"""
from collections import Counter, defaultdict
from datetime import timezone
from hashlib import sha256
import json
from math import isfinite
from numbers import Real
from zoneinfo import ZoneInfo

import api_football as api
import api_match_workspace as workspace
from league_model import VALUES
import match_predictor as predictor


VERSION = 'high-probability-clubs-v1'
SOURCE = 'API-Football'
ZONE = ZoneInfo('Africa/Kinshasa')


def _profile():
    return dict(matches=0, scored=[0, 0], conceded=[0, 0])


def _copy_profile(value):
    return dict(matches=value['matches'], scored=list(value['scored']),
                conceded=list(value['conceded']))


def _aggregate(rows, league, before):
    """Exactly the training validation and integer sums used by estimate()."""
    train = predictor.training_rows(rows, league, before)
    home, away = defaultdict(_profile), defaultdict(_profile)
    totals = [[0, 0], [0, 0]]
    for row in train:
        goals = predictor.goals(row)
        for venue, name, index in ((home, row['home'], 0), (away, row['away'], 1)):
            current = venue[name]
            current['matches'] += 1
            for half in range(2):
                current['scored'][half] += goals[index][half]
                current['conceded'][half] += goals[1-index][half]
        for index in range(2):
            for half in range(2):
                totals[index][half] += goals[index][half]
    counts, n = Counter(row['value'] for row in train), len(train)
    days = len({row['timestamp'][:10] for row in train})
    return dict(training=n, history_days=days, home=home, away=away,
                means=[[value/n for value in side] for side in totals] if n else None,
                baseline={value: (counts[value]+1)/(n+3) for value in VALUES},
                history_fingerprint=sha256(json.dumps(train, sort_keys=True, ensure_ascii=False,
                    separators=(',', ':'), allow_nan=False).encode()).hexdigest(),
                last_training_day=train[-1]['timestamp'][:10] if train else None)


def _estimate(state, home, away, before):
    """Preserve predictor.estimate arithmetic, including shrinkage and fallback."""
    h = _copy_profile(state['home'].get(home, _profile()))
    a = _copy_profile(state['away'].get(away, _profile()))
    eligible = (state['training'] >= predictor.MIN_LEAGUE
                and state['history_days'] >= predictor.MIN_DAYS)
    personalized = eligible and min(h['matches'], a['matches']) >= predictor.MIN_TEAM
    baseline, means = dict(state['baseline']), None
    probabilities = dict(baseline)
    if personalized:
        means = []
        for half in range(2):
            home_mean, away_mean = state['means'][0][half], state['means'][1][half]
            hs = (h['scored'][half]+predictor.SHRINK*home_mean)/(h['matches']+predictor.SHRINK)
            ac = (a['conceded'][half]+predictor.SHRINK*home_mean)/(a['matches']+predictor.SHRINK)
            ass = (a['scored'][half]+predictor.SHRINK*away_mean)/(a['matches']+predictor.SHRINK)
            hc = (h['conceded'][half]+predictor.SHRINK*away_mean)/(h['matches']+predictor.SHRINK)
            means.append((hs+ac+ass+hc)/2)
        team_p = predictor.half_probabilities(*means)
        weight = min(h['matches'], a['matches'])/(min(h['matches'], a['matches'])+predictor.SHRINK)
        probabilities = {value: weight*team_p[value]+(1-weight)*baseline[value] for value in VALUES}
    return dict(eligible=eligible, personalized=personalized, probabilities=probabilities,
                baseline=baseline, training=state['training'], history_days=state['history_days'],
                home_profile=h, away_profile=a, expected_goals=means, before=before,
                history_fingerprint=state['history_fingerprint'],
                last_training_day=state['last_training_day'])


def scan(raw, now=None):
    """Rank all eligible future NS fixtures today in Kinshasa by max probability.

    All histories stop before the first UTC date of today's confirmed targets,
    additionally bounded by today's current UTC date. No result from that UTC
    day enters any prediction. Conflicting fixture IDs are excluded entirely;
    exact duplicates are counted once. Targets' score fields are never read.
    """
    current = workspace._now(now)
    if not isinstance(raw, (list, tuple)):
        raise ValueError('Les données API-Football doivent être une liste de matchs.')
    day = current.astimezone(ZONE).date()
    unique, conflicts, excluded = {}, set(), []
    diagnostics = dict(raw_count=len(raw), unique_count=0, day_count=0, upcoming_count=0,
                       eligible_count=0, history_count=0, history_leagues=0,
                       duplicate_count=0, invalid_history_count=0, invalid_history=[])

    def exclude(mid, reason, detail, fixture=None):
        item = dict(match_id=mid, reason=reason, detail=detail)
        if fixture is not None:
            item['fixture'] = fixture
        excluded.append(item)

    for item in raw:
        if not isinstance(item, dict):
            exclude(None, 'invalid_fixture', 'Données du match absentes ou invalides.')
            continue
        if item.get('provider') not in (None, 'api-football', 'API-Football'):
            exclude(None, 'foreign_provider', 'Seules les données API-Football sont acceptées.')
            continue
        try:
            mid = api._numeric_id(item['fixture']['id'], 'du match')
        except (KeyError, TypeError, ValueError):
            exclude(None, 'invalid_fixture_id', 'Identifiant du match absent ou invalide.')
            continue
        if mid in unique:
            if unique[mid] != item:
                conflicts.add(mid)
            else:
                diagnostics['duplicate_count'] += 1
        else:
            unique[mid] = item
    diagnostics['unique_count'] = len(unique)
    for mid in sorted(conflicts, key=int):
        exclude(mid, 'conflicting_duplicate', 'Plusieurs relevés contradictoires pour ce match.')

    dated, upcoming = [], []
    for mid, item in unique.items():
        if mid in conflicts:
            continue
        try:
            stamp = workspace._stamp(item)
        except ValueError as error:
            exclude(mid, 'invalid_fixture_date', str(error))
            continue
        dated.append((mid, item, stamp))
        if stamp.astimezone(ZONE).date() != day:
            continue
        diagnostics['day_count'] += 1
        short = api._status_short(item)
        if short != 'NS':
            exclude(mid, 'not_upcoming', f'Statut {short or "inconnu"} : seul un match NS confirmé est analysé.')
            continue
        if stamp <= current:
            exclude(mid, 'kickoff_elapsed', 'Horaire dépassé sans confirmation d’un match à venir.')
            continue
        try:
            fixture = workspace.fixture_from_raw(item)
        except ValueError as error:
            exclude(mid, 'unconfirmed_fixture', str(error))
            continue
        fixture.update(status='TIMED', raw_status='NS', is_upcoming=True,
                       is_historical=False, selectable=True, status_reason='')
        upcoming.append((fixture, stamp))
    diagnostics['upcoming_count'] = len(upcoming)
    first = min((stamp for _, stamp in upcoming), default=current)
    before = min(first.date(), current.date()).isoformat()
    leagues = {fixture['league'] for fixture, _ in upcoming}
    histories = defaultdict(list)
    for mid, item, stamp in dated:
        if stamp >= current or stamp.date().isoformat() >= before:
            continue
        if api._status_short(item) not in api.FINISHED_STATUSES:
            continue
        try:
            league = api._numeric_id(item['league']['id'], 'de compétition')
            if league not in leagues:
                continue
            histories[league].append(api.normalize_fixture(item))
        except (KeyError, TypeError, ValueError) as error:
            diagnostics['invalid_history'].append(dict(match_id=mid, reason=str(error)))
    diagnostics['history_count'] = sum(map(len, histories.values()))
    diagnostics['history_leagues'] = len(histories)
    diagnostics['invalid_history_count'] = len(diagnostics['invalid_history'])
    states = {league: _aggregate(histories[league], league, before) for league in leagues}
    rows = []
    for fixture, _ in upcoming:
        state = states[fixture['league']]
        estimate = _estimate(state, fixture['home'], fixture['away'], before)
        if state['training'] < predictor.MIN_LEAGUE:
            exclude(fixture['match_id'], 'insufficient_league_history',
                    f"{state['training']}/{predictor.MIN_LEAGUE} résultats historiques exploitables.", fixture)
            continue
        if state['history_days'] < predictor.MIN_DAYS:
            exclude(fixture['match_id'], 'insufficient_history_days',
                    f"{state['history_days']}/{predictor.MIN_DAYS} journées UTC historiques.", fixture)
            continue
        if not estimate['personalized']:
            exclude(fixture['match_id'], 'insufficient_club_history',
                    f"Précédents dans leur rôle : domicile {estimate['home_profile']['matches']}, "
                    f"extérieur {estimate['away_profile']['matches']} ; minimum {predictor.MIN_TEAM} chacun.", fixture)
            continue
        choice = max(VALUES, key=estimate['probabilities'].get)
        rows.append(dict(fixture=fixture, **estimate, choice=choice,
                         max_probability=estimate['probabilities'][choice]))
    rows.sort(key=lambda row: (-row['max_probability'], row['fixture']['timestamp'],
                               int(row['fixture']['match_id'])))
    for rank, row in enumerate(rows, 1):
        row['rank'] = rank
    diagnostics['eligible_count'] = len(rows)
    diagnostics['excluded_counts'] = dict(Counter(item['reason'] for item in excluded))
    history_fingerprint = sha256(json.dumps(
        dict(before=before, leagues={league: state['history_fingerprint'] for league, state in sorted(states.items())}),
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return dict(version=VERSION, source=SOURCE, timezone=ZONE.key, day=day.isoformat(),
                before=before, generated_at=current.astimezone(timezone.utc).isoformat(timespec='seconds'),
                history_fingerprint=history_fingerprint,
                rows=rows, excluded=excluded, diagnostics=diagnostics)


def filter_rows(report, minimum=.50, outcome=None):
    """Rank each requested outcome independently, then apply its threshold.

    With no outcome, the search uses each match's greatest probability. A
    specific outcome instead uses that outcome's probability for every match,
    even when another outcome is the model's first choice. Returned row copies
    carry the search value separately; model choices and probabilities stay
    unchanged. Ranks describe the requested population before the threshold.
    """
    if (isinstance(minimum, bool) or not isinstance(minimum, Real)
            or not isfinite(minimum) or not 0 <= minimum <= 1):
        raise ValueError('Le seuil doit être un nombre entre 0 et 1.')
    if outcome is not None and outcome not in VALUES:
        raise ValueError('Le résultat choisi doit être 1st, Égal ou 2nd.')
    if not isinstance(report, dict) or not isinstance(report.get('rows'), list):
        raise ValueError('Rapport de probabilités invalide.')
    ranked = [dict(row, search_outcome=outcome,
                   search_probability=(row['max_probability'] if outcome is None
                                       else row['probabilities'][outcome]))
              for row in report['rows']]
    ranked.sort(key=lambda row: (-row['search_probability'], row['fixture']['timestamp'],
                                 int(row['fixture']['match_id'])))
    for rank, row in enumerate(ranked, 1):
        row['rank'] = rank
    return [row for row in ranked if row['search_probability'] >= minimum]
