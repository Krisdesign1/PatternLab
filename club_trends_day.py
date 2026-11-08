"""Small read-only day calendar joined to descriptive club profiles by API IDs.

The day query includes scheduled, live, finished and administrative states. It
loads no events or statistics and never requests an API refresh, migrates storage
or treats historical frequencies as probabilities for the fixture.
"""
from collections import Counter
from copy import deepcopy
from datetime import datetime, time, timedelta, timezone
import json

import calendar_data
import competition_trends as base
import fixture_store
from halftime_domain import CLASS_ORDER

VERSION = 'club-trends-day-v1'
ZONE = base.ZONE
TrendsError = base.TrendsError

# Reuse the calendar's domain projection without copying large fixture events,
# statistics, lineups or player payloads into Python for a day-only display.
_COMPACT = """json_object(
    'fixture', json_object('id',json_extract(raw_json,'$.fixture.id'),
                          'date',json_extract(raw_json,'$.fixture.date'),
                          'status',json_extract(raw_json,'$.fixture.status')),
    'league', json_extract(raw_json,'$.league'),
    'teams', json_extract(raw_json,'$.teams'),
    'goals', json_extract(raw_json,'$.goals'),
    'score', json_extract(raw_json,'$.score'),
    'provider', json_extract(raw_json,'$.provider'),
    '_patternlab_import', json_extract(raw_json,'$._patternlab_import')) AS compact"""


def _id(value):
    try:
        return base._identity(value)
    except TrendsError:
        return None


def _record(saved):
    raw = json.loads(saved['compact'])
    row = calendar_data.from_raw(raw)
    league = _id(raw.get('league', {}).get('id'))
    teams = raw.get('teams') or {}
    home = _id((teams.get('home') or {}).get('id'))
    away = _id((teams.get('away') or {}).get('id'))
    # calendar_data is the display boundary; ID joins are stricter and accept
    # ASCII numeric API identifiers only, never a matching name or Unicode ID.
    row.update(league=league, home_id=home, away_id=away)
    confirmed = bool(row['teams_confirmed'] and league and home and away and home != away)
    row.update(teams_confirmed=confirmed,
               selectable=bool(row['selectable'] and confirmed), import_issue=saved['issue'])
    if not confirmed and row['final_verdict'] in CLASS_ORDER:
        row.update(final_verdict='À vérifier', final_verdict_reason='Identité des clubs ou de compétition non confirmée')
    elif saved['issue'] and row['final_verdict'] in CLASS_ORDER:
        row.update(final_verdict='À vérifier', final_verdict_reason=saved['issue'])
    stamp = base._stamp(row['timestamp']).astimezone(ZONE)
    row.update(kickoff_local=stamp.isoformat(), local_day=stamp.date().isoformat(),
               season=raw.get('league', {}).get('season'))
    return row


def load(day, *, path=None, now=None):
    """Read only fixtures inside the full selected Kinshasa day, even in future.

    ``now`` timestamps the consultation; it must not cut off upcoming fixtures.
    Revision, day-refresh marker and fixtures share one SQLite read snapshot.
    """
    day, current = base._day(day), base._now(now)
    try:
        lower = datetime.combine(day, time.min, ZONE).astimezone(timezone.utc)
        upper = datetime.combine(day + timedelta(days=1), time.min, ZONE).astimezone(timezone.utc)
    except (OverflowError, ValueError):
        raise TrendsError('La journée choisie est hors de la plage du calendrier.') from None
    rows, unreadable = [], 0
    try:
        with fixture_store._connection(base._database(path)) as connection:
            meta = fixture_store._meta(connection)
            query = ('SELECT match_id, issue, '+_COMPACT+' FROM fixtures '
                     'WHERE kickoff_utc >= ? AND kickoff_utc < ? '
                     'ORDER BY kickoff_utc, CAST(match_id AS INTEGER), match_id')
            for saved in connection.execute(query, (base._utc_text(lower), base._utc_text(upper))):
                try:
                    row = _record(saved)
                    # Defend the day view if a damaged SQLite projection ever
                    # disagrees with its provider timestamp.
                    if row['local_day'] != day.isoformat():
                        raise ValueError('Date incohérente')
                    rows.append(row)
                except (KeyError, TypeError, ValueError, AttributeError):
                    unreadable += 1
            update = connection.execute('SELECT updated_at FROM calendar_updates WHERE day=? AND timezone=?',
                                        (day.isoformat(), str(ZONE))).fetchone()
            revision, uuid, updated_at = int(meta['revision']), meta['store_uuid'], meta.get('updated_at')
    except fixture_store.FixtureStoreError:
        raise TrendsError('La lecture des matchs de la journée est momentanément indisponible.') from None
    groups = Counter(row['status_group'] for row in rows)
    return dict(version=VERSION, rows=rows, day=day.isoformat(), timezone=str(ZONE),
                as_of=current.isoformat(), revision=revision, store_uuid=uuid,
                calendar_updated_at=update['updated_at'] if update else None, updated_at=updated_at,
                totals=dict(matches=len(rows), unreadable=unreadable,
                            **{name: groups[name] for name in ('upcoming','live','finished','other')}))


def enrich(fixtures, profiles):
    """Join every fixture to its exact (club ID, competition ID) profile.

    A missing club/profile stays visible with ``None`` and a false history flag.
    The selected profile role remains explicit; role filters are not silently
    changed to a home/away role or joined to another competition's history.
    """
    indexed = {}
    for profile in profiles:
        if not isinstance(profile, dict):
            raise TrendsError('Un profil de club est invalide.')
        team, league = _id(profile.get('team_id')), _id(profile.get('league'))
        if team is None or league is None:
            continue
        key = (team, league)
        if key in indexed:
            raise TrendsError('Plusieurs profils existent pour le même club et la même compétition.')
        indexed[key] = profile
    result = []
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            raise TrendsError('Une rencontre de la journée est invalide.')
        row = deepcopy(fixture)
        league = _id(row.get('league'))
        for side in ('home','away'):
            team = _id(row.get(side+'_id'))
            profile = indexed.get((team,league)) if team and league else None
            row[side+'_profile'] = deepcopy(profile) if profile is not None else None
            row[side+'_has_history'] = bool(profile and profile.get('matches',0) > 0)
        result.append(row)
    return result


def filter_profiles(profiles, fixtures):
    """Restrict historical profiles to exact club/league IDs in this calendar.

    Both sides and every recorded fixture status contribute to membership.
    The profile's historical role, counts and probabilities are never changed.
    Names never supply an identity or borrow another competition's history.
    """
    keys = set()
    for fixture in fixtures:
        league = _id(fixture.get('league'))
        if league is None:
            continue
        for side in ('home', 'away'):
            team = _id(fixture.get(side+'_id'))
            if team is not None:
                keys.add((team, league))
    rows = [row for row in profiles if (_id(row.get('team_id')), _id(row.get('league'))) in keys]
    matched = {(_id(row.get('team_id')), _id(row.get('league'))) for row in rows}
    return dict(rows=rows, participating_profiles=len(keys), missing_profiles=len(keys-matched))
