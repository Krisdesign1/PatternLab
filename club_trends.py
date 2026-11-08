"""Descriptive club/competition profiles, streamed from the read-only fixture store.

The outcome compares *both teams' total goals* between halves. It is not a
forecast, a club-only scoring outcome, or a replay of pre-match availability.
Identity and result validation share the competition trend import boundary.
"""
from collections import Counter, deque
import json

import competition_trends as base
import fixture_store
from halftime_domain import CLASS_ORDER, parse_score

VERSION = 'club-trends-v1'
TrendsError = base.TrendsError
catalogue = base.catalogue
ROLES = ('all', 'home', 'away')
_IDS = """CASE WHEN normalized_json IS NOT NULL
    THEN json_extract(normalized_json, '$.home_id', '$.away_id')
    ELSE json_extract(raw_json, '$.teams.home.id', '$.teams.away.id') END AS club_ids"""


def _role(value):
    if value not in ROLES:
        raise TrendsError('Le rôle doit être tous les matchs, domicile ou extérieur.')
    return value


def _record(row):
    record = base._record(row)
    # A failed current import must never be rescued by a stale projection.
    if row['issue']:
        record.update(included=False, excluded=True, value=None, reason=row['issue'])
    try:
        home, away = json.loads(row['club_ids'])
        home, away = base._identity(home), base._identity(away)
        if home == away:
            raise TrendsError('Les deux clubs possèdent le même identifiant.')
        record.update(home_id=home, away_id=away)
    except (TypeError, ValueError):
        record.update(home_id=None, away_id=None, included=False, excluded=True,
                      value=None, reason='Identifiants des clubs non confirmés')
    try:
        record['league'] = base._identity(record['league'])
    except TrendsError:
        record.update(league=None, included=False, excluded=True, value=None,
                      reason='Identifiant de compétition non confirmé')
    return record


def _appearances(record, role):
    if not record['league'] or not record['home_id'] or not record['away_id']:
        return
    # The shared validation boundary already verified both score strings.
    ht, ft = (parse_score(record['ht']), parse_score(record['ft'])) if record['included'] else (None, None)
    for side, other, index in (('home', 'away', 0), ('away', 'home', 1)):
        if role != 'all' and role != side:
            continue
        item = dict(record, team_id=record[side + '_id'], club=record[side],
                    role=side, opponent=record[other])
        goals = dict(goals_for_h1=None, goals_against_h1=None,
                     goals_for_h2=None, goals_against_h2=None)
        if record['included']:
            goals.update(goals_for_h1=ht[index], goals_against_h1=ht[1-index],
                         goals_for_h2=ft[index]-ht[index],
                         goals_against_h2=ft[1-index]-ht[1-index])
        yield dict(item, **goals)


def _finish(item):
    latest = item.pop('latest')
    item['seasons'] = sorted(item['seasons'])
    item['counts'] = dict(item['counts'])
    item['recent_results'] = list(latest)
    item['recent_matches'] = len(latest)
    item['recent_counts'] = {value: latest.count(value) for value in CLASS_ORDER}
    item['previous_matches'] = item['matches'] - len(latest)
    item['previous_counts'] = {value: item['counts'][value] - item['recent_counts'][value]
                               for value in CLASS_ORDER}
    for prefix, total in (('', 'matches'), ('recent_', 'recent_matches'), ('previous_', 'previous_matches')):
        item[prefix + 'percentages'] = base._percent(item[prefix + 'counts'], item[total])
    item['delta_points'] = {value: item['recent_percentages'][value] - item['previous_percentages'][value]
        if item['recent_matches'] and item['previous_matches'] else None for value in CLASS_ORDER}
    maximum = max(item['counts'].values()) if item['matches'] else None
    item['leaders'] = [value for value in CLASS_ORDER if maximum is not None and item['counts'][value] == maximum]
    recent_max = max(item['recent_counts'].values()) if latest else None
    item['recent_leaders'] = [value for value in CLASS_ORDER if recent_max is not None and item['recent_counts'][value] == recent_max]
    item['goals_per_match'] = {key: total / item['matches'] if item['matches'] else None
                               for key, total in item.pop('goals').items()}
    return item


def build(start, end, *, seasons=None, recent=10, role='all', now=None, path=None):
    """One profile per (API team ID, league ID), without blending competitions.

    Each fixture contributes once to totals and once to each applicable club.
    Memory is bounded by profile count × recent window, never raw payload size.
    """
    role = _role(role)
    if type(recent) is not int or not 1 <= recent <= 100:
        raise TrendsError('La fenêtre récente doit contenir de 1 à 100 matchs.')
    where, args, current = base._bounds(start, end, seasons, now)
    groups, reasons, totals = {}, Counter(), Counter(observed=0, matches=0, excluded=0, not_finished=0)
    unassigned = 0
    try:
        with fixture_store._connection(base._database(path)) as connection:
            meta = fixture_store._meta(connection)
            query = 'SELECT ' + base._COLUMNS + ', ' + _IDS + ' FROM fixtures WHERE ' + where + base._ORDER
            for row in connection.execute(query, args):
                record = _record(row)
                totals['observed'] += 1
                if record['included']:
                    totals['matches'] += 1
                elif record['excluded']:
                    totals['excluded'] += 1
                    reasons[record['reason']] += 1
                else:
                    totals['not_finished'] += 1
                appearances = list(_appearances(record, role))
                if not appearances:
                    unassigned += 1
                for match in appearances:
                    key = (match['team_id'], match['league'])
                    item = groups.get(key)
                    if item is None:
                        item = groups[key] = dict(team_id=key[0], league=key[1],
                        profile_id=':'.join(key), club=match['club'], competition=match['competition'],
                        country=match['country'], role=role, matches=0, observed=0, excluded=0, not_finished=0,
                        counts=Counter({value: 0 for value in CLASS_ORDER}), latest=deque(maxlen=recent),
                        goals=Counter({name: 0 for name in ('goals_for_h1', 'goals_against_h1', 'goals_for_h2', 'goals_against_h2')}),
                        seasons=set(), first_day=None, last_day=None)
                    # Invalid metadata must not rename an established valid profile.
                    if match['included'] or not item['matches']:
                        item.update(club=match['club'], competition=match['competition'], country=match['country'])
                    item['observed'] += 1
                    if type(match['season']) is int:
                        item['seasons'].add(match['season'])
                    if match['included']:
                        item['matches'] += 1
                        item['counts'][match['value']] += 1
                        item['latest'].append(match['value'])
                        item['first_day'] = item['first_day'] or match['local_day']
                        item['last_day'] = match['local_day']
                        for name in item['goals']:
                            item['goals'][name] += match[name]
                    elif match['excluded']:
                        item['excluded'] += 1
                    else:
                        item['not_finished'] += 1
            revision, store_uuid = int(meta['revision']), meta['store_uuid']
    except fixture_store.FixtureStoreError:
        raise TrendsError('La lecture des tendances des clubs est momentanément indisponible.') from None
    rows = [_finish(item) for item in groups.values()]
    rows.sort(key=lambda item: (item['country'].casefold(), item['competition'].casefold(), item['club'].casefold(), item['team_id']))
    totals.update(profiles=len(rows), clubs=len({item['team_id'] for item in rows}))
    return dict(version=VERSION, rows=rows, totals=dict(totals), exclusion_reasons=dict(sorted(reasons.items())),
                start=start.isoformat(), end=end.isoformat(), recent=recent, role=role,
                timezone=str(base.ZONE), as_of=current.isoformat(), revision=revision,
                store_uuid=store_uuid, unassigned_matches=unassigned)


def details(team, league, start, end, *, seasons=None, role='all', now=None, path=None, limit=50, offset=0):
    """Read only one league and stream exact validated club IDs before paging.

    IDs are checked in Python so malformed or zero-padded raw IDs cannot create
    SQL coercion collisions or a mismatch between counts and displayed evidence.
    """
    role, team = _role(role), base._identity(team)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 500:
        raise TrendsError('La pagination des rencontres est invalide.')
    where, args, current = base._bounds(start, end, seasons, now, league)
    rows, total = [], 0
    try:
        with fixture_store._connection(base._database(path)) as connection:
            meta = fixture_store._meta(connection)
            query = ('SELECT ' + base._COLUMNS + ', ' + _IDS + ' FROM fixtures WHERE ' + where
                     + ' ORDER BY kickoff_utc DESC, CAST(match_id AS INTEGER) DESC, match_id DESC')
            for row in connection.execute(query, args):
                for match in _appearances(_record(row), role):
                    if match['team_id'] == team:
                        if offset <= total < offset + limit:
                            rows.append(match)
                        total += 1
            return dict(rows=rows, total=total, offset=offset, limit=limit, role=role,
                        timezone=str(base.ZONE), as_of=current.isoformat(),
                        revision=int(meta['revision']), store_uuid=meta['store_uuid'])
    except fixture_store.FixtureStoreError:
        raise TrendsError('La lecture des rencontres du club est momentanément indisponible.') from None
