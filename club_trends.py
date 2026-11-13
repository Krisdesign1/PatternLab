"""Descriptive club/competition profiles, streamed from the read-only fixture store.

The outcome compares *both teams' total goals* between halves. It is not a
forecast, a club-only scoring outcome, or a replay of pre-match availability.
Identity and result validation share the competition trend import boundary.
"""
from collections import Counter, deque
from datetime import datetime, time, timedelta
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


def _record(row, context=None):
    context = context or base._RecordContext()
    record = base._record(row, _context=context)
    # A failed current import must never be rescued by a stale projection.
    if row['issue']:
        record.update(included=False, excluded=True, value=None, reason=row['issue'])
    try:
        if row['has_projection'] and isinstance(context.payload, dict):
            home, away = context.payload.get('home_id'), context.payload.get('away_id')
        else:
            home, away = json.loads(row['club_ids'])
        home, away = context.identity(home), context.identity(away)
        if home == away:
            raise TrendsError('Les deux clubs possèdent le même identifiant.')
        record.update(home_id=home, away_id=away)
    except (TypeError, ValueError):
        record.update(home_id=None, away_id=None, included=False, excluded=True,
                      value=None, reason='Identifiants des clubs non confirmés')
    try:
        record['league'] = context.identity(record['league'])
    except TrendsError:
        record.update(league=None, included=False, excluded=True, value=None,
                      reason='Identifiant de compétition non confirmé')
    return record


def _appearances(record, role, outcome=None):
    if not record['league'] or not record['home_id'] or not record['away_id']:
        return
    # The shared validation boundary already verified both score strings.
    ht, ft = ((outcome.halftime, outcome.fulltime) if outcome is not None else
              (parse_score(record['ht']), parse_score(record['ft']))) if record['included'] else (None, None)
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


def _bounds(start, end, seasons, now, league=None, before_day=None):
    where, args, current = base._bounds(start, end, seasons, now, league)
    if before_day is not None:
        before_day = base._day(before_day)
        cutoff = datetime.combine(before_day, time.min, base.ZONE)
        where += ' AND kickoff_utc < ?'
        args.append(base._utc_text(cutoff))
    return where, args, current


def _aggregate():
    return dict(groups={}, role_history={}, role_counts={}, reasons=Counter(),
                totals=Counter(observed=0, matches=0, excluded=0, not_finished=0), unassigned=0)


def _copy_aggregate(state):
    """Fork the common history without copying immutable fixture identities.

    The two reports finish their counters and windows independently. Only the
    mutable containers must be copied: window entries are immutable tuples of
    strings and all other profile metadata is scalar.
    """
    result = dict(state)
    result['groups'] = {key: dict(item, counts=item['counts'].copy(),
        latest=item['latest'].copy(), goals=item['goals'].copy(), seasons=item['seasons'].copy())
        for key, item in state['groups'].items()}
    result['role_history'] = {key: {side: window.copy() for side, window in windows.items()}
                              for key, windows in state['role_history'].items()}
    result['role_counts'] = {key: {side: counts.copy() for side, counts in sides.items()}
                             for key, sides in state['role_counts'].items()}
    result['totals'] = state['totals'].copy()
    result['reasons'] = state['reasons'].copy()
    return result


def _add_record(state, record, appearances, role, recent):
    groups, role_history, role_counts = (state[name] for name in ('groups', 'role_history', 'role_counts'))
    totals, reasons = state['totals'], state['reasons']
    totals['observed'] += 1
    if record['included']:
        totals['matches'] += 1
    elif record['excluded']:
        totals['excluded'] += 1
        reasons[record['reason']] += 1
    else:
        totals['not_finished'] += 1
    if not appearances:
        state['unassigned'] += 1
    for match in appearances:
        key = (match['team_id'], match['league'])
        if match['included']:
            if key not in role_history:
                role_history[key] = {side: deque(maxlen=recent) for side in ('home', 'away')}
                role_counts[key] = {side: Counter({value: 0 for value in CLASS_ORDER})
                                    for side in ('home', 'away')}
            windows = role_history[key]
            windows[match['role']].append((match['value'], match['kickoff_local'], match['match_id']))
            role_counts[key][match['role']][match['value']] += 1
        item = groups.get(key)
        if item is None:
            item = groups[key] = dict(team_id=key[0], league=key[1],
                profile_id=':'.join(key), club=match['club'], competition=match['competition'],
                country=match['country'], role=role, matches=0, observed=0, excluded=0, not_finished=0,
                counts=Counter({value: 0 for value in CLASS_ORDER}), latest=deque(maxlen=recent),
                goals=Counter({name: 0 for name in ('goals_for_h1', 'goals_against_h1', 'goals_for_h2', 'goals_against_h2')}),
                seasons=set(), first_day=None, last_day=None)
        # Invalid metadata must not rename an established valid profile.
        if match['included'] or key not in role_counts:
            item.update(club=match['club'], competition=match['competition'], country=match['country'])
        # Opposite-role frequencies remain available without mixing main counts.
        if role != 'all' and match['role'] != role:
            continue
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


def _report(state, start, end, recent, role, current, revision, store_uuid, before_day):
    rows = [_finish(item) for item in state['groups'].values()]
    for item in rows:
        key = (item['team_id'], item['league'])
        windows = state['role_history'].get(key, {})
        counts = state['role_counts'].get(key, {})
        for side in ('home', 'away'):
            window = windows.get(side, ())
            item['recent_' + side + '_results'] = [entry[0] for entry in window]
            item['recent_' + side + '_dates'] = [entry[1] for entry in window]
            item['recent_' + side + '_ids'] = [entry[2] for entry in window]
            item[side + '_counts'] = {value: counts.get(side, {}).get(value, 0) for value in CLASS_ORDER}
            item[side + '_matches'] = sum(item[side + '_counts'].values())
            item[side + '_percentages'] = base._percent(item[side + '_counts'], item[side + '_matches'])
    rows.sort(key=lambda item: (item['country'].casefold(), item['competition'].casefold(), item['club'].casefold(), item['team_id']))
    state['totals'].update(profiles=len(rows), clubs=len({row['team_id'] for row in rows}))
    return dict(version=VERSION, rows=rows, totals=dict(state['totals']),
                exclusion_reasons=dict(sorted(state['reasons'].items())),
                start=start.isoformat(), end=end.isoformat(), recent=recent, role=role,
                timezone=str(base.ZONE), as_of=current.isoformat(), revision=revision,
                store_uuid=store_uuid, unassigned_matches=state['unassigned'],
                before_day=before_day.isoformat() if before_day is not None else None)


def build(start, end, *, seasons=None, recent=10, role='all', now=None, path=None, before_day=None):
    """One profile per (API team ID, league ID), without blending competitions.

    Each fixture contributes once to totals and once to each applicable club.
    Memory is bounded by profile count × recent window, never raw payload size.
    Home/away counts and percentages use every eligible result in each role,
    independently of the main role filter and the recent window length.
    Optional before_day excludes that Kinshasa day and every following kickoff
    before computing counts and recent windows; it never rewrites the store.
    """
    role = _role(role)
    if type(recent) is not int or not 1 <= recent <= 100:
        raise TrendsError('La fenêtre récente doit contenir de 1 à 100 matchs.')
    where, args, current = _bounds(start, end, seasons, now, before_day=before_day)
    state = _aggregate()
    context = base._RecordContext()
    try:
        with fixture_store._connection(base._database(path)) as connection:
            meta = fixture_store._meta(connection)
            query = 'SELECT ' + base._COLUMNS + ', ' + _IDS + ' FROM fixtures WHERE ' + where + base._ORDER
            for row in connection.execute(query, args):
                record = _record(row, context)
                _add_record(state, record, list(_appearances(record, 'all', context.outcome)), role, recent)
            revision, store_uuid = int(meta['revision']), meta['store_uuid']
    except fixture_store.FixtureStoreError:
        raise TrendsError('La lecture des tendances des clubs est momentanément indisponible.') from None
    return _report(state, start, end, recent, role, current, revision, store_uuid, before_day)


def build_comparison(start, end, *, comparison_start, comparison_end=None, seasons=None,
                     recent=10, role='all', now=None, path=None):
    """Compute before/after periods from one validated, read-only SQL snapshot.

    Before excludes comparison_start and all later Kinshasa days. After includes
    comparison_end but no later day. Both retain the requested historical
    start/end, seasons and consultation-time bounds. A multi-day comparison uses
    shared cutoffs before its first day and after its last day; it is not a
    per-match replay of which corrections were available before kickoff.
    """
    comparison_start = base._day(comparison_start)
    comparison_end = base._day(comparison_end if comparison_end is not None else comparison_start)
    if comparison_start > comparison_end:
        raise TrendsError('La première journée de comparaison doit précéder ou être égale à la dernière.')
    try:
        after_day = comparison_end + timedelta(days=1)
    except OverflowError:
        raise TrendsError('La dernière journée de comparaison est invalide.') from None
    role = _role(role)
    if type(recent) is not int or not 1 <= recent <= 100:
        raise TrendsError('La fenêtre récente doit contenir de 1 à 100 matchs.')
    where, args, current = _bounds(start, end, seasons, now, before_day=after_day)
    before, after = _aggregate(), None
    context = base._RecordContext()
    boundary = comparison_start.isoformat()
    try:
        with fixture_store._connection(base._database(path)) as connection:
            meta = fixture_store._meta(connection)
            query = 'SELECT ' + base._COLUMNS + ', ' + _IDS + ' FROM fixtures WHERE ' + where + base._ORDER
            for row in connection.execute(query, args):
                record = _record(row, context)
                appearances = list(_appearances(record, 'all', context.outcome))
                if record['local_day'] < boundary:
                    _add_record(before, record, appearances, role, recent)
                else:
                    # The SQL order makes the history before the comparison a
                    # common prefix. Aggregate it once, then fork its bounded
                    # state before applying only the additional days to After.
                    if after is None:
                        after = _copy_aggregate(before)
                    _add_record(after, record, appearances, role, recent)
            revision, store_uuid = int(meta['revision']), meta['store_uuid']
    except fixture_store.FixtureStoreError:
        raise TrendsError('La comparaison des tendances des clubs est momentanément indisponible.') from None
    if after is None:
        after = _copy_aggregate(before)
    return dict(before=_report(before, start, end, recent, role, current, revision, store_uuid, comparison_start),
                after=_report(after, start, end, recent, role, current, revision, store_uuid, after_day),
                comparison_start=comparison_start.isoformat(), comparison_end=comparison_end.isoformat(),
                before_cutoff_utc=base._utc_text(datetime.combine(comparison_start, time.min, base.ZONE)),
                after_cutoff_utc=base._utc_text(datetime.combine(after_day, time.min, base.ZONE)),
                revision=revision, store_uuid=store_uuid, as_of=current.isoformat(), timezone=str(base.ZONE))


def details(team, league, start, end, *, seasons=None, role='all', now=None, path=None, limit=50, offset=0,
            before_day=None):
    """Read only one league and stream exact validated club IDs before paging.

    IDs are checked in Python so malformed or zero-padded raw IDs cannot create
    SQL coercion collisions or a mismatch between counts and displayed evidence.
    """
    role, team = _role(role), base._identity(team)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 500:
        raise TrendsError('La pagination des rencontres est invalide.')
    where, args, current = _bounds(start, end, seasons, now, league, before_day)
    rows, total = [], 0
    context = base._RecordContext()
    try:
        with fixture_store._connection(base._database(path)) as connection:
            meta = fixture_store._meta(connection)
            query = ('SELECT ' + base._COLUMNS + ', ' + _IDS + ' FROM fixtures WHERE ' + where
                     + ' ORDER BY kickoff_utc DESC, CAST(match_id AS INTEGER) DESC, match_id DESC')
            for row in connection.execute(query, args):
                record = _record(row, context)
                for match in _appearances(record, role, context.outcome):
                    if match['team_id'] == team:
                        if offset <= total < offset + limit:
                            rows.append(match)
                        total += 1
            return dict(rows=rows, total=total, offset=offset, limit=limit, role=role,
                        timezone=str(base.ZONE), as_of=current.isoformat(),
                        before_day=before_day.isoformat() if before_day is not None else None,
                        revision=int(meta['revision']), store_uuid=meta['store_uuid'])
    except fixture_store.FixtureStoreError:
        raise TrendsError('La lecture des rencontres du club est momentanément indisponible.') from None
