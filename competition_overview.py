"""Causal, local competition summaries for one Kinshasa calendar day."""
from collections import defaultdict
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import api_football as api
import competition_readiness as readiness
from coupon_sizes import validate_size


ZONE = 'Africa/Kinshasa'
SORTS = ('matches', 'history', 'name')


def _day(value):
    if isinstance(value, datetime):
        raise ValueError('Choisissez une journée sans heure.')
    return value if isinstance(value, date) else date.fromisoformat(value)


def _now(value):
    value = value or datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('L’heure actuelle doit préciser son fuseau.')
    return value.astimezone(timezone.utc)


def _stamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _identity(value):
    return api._numeric_id(value, 'de compétition')


def build(snapshot, rows, day, *, size=4, now=None):
    """Summarize without a model, API, file access, or today's training outcomes.

    History is grouped once, then each league validates only its own rows. UTC
    cutoffs use the earliest fixture of that league on the selected local day,
    bounded by the current UTC day even when browsing a future calendar day.
    """
    day, size, current = _day(day), validate_size(size), _now(now)
    zone = ZoneInfo(snapshot.get('timezone') or ZONE)
    if snapshot.get('day') and _day(snapshot['day']) != day:
        return []
    if snapshot.get('source_label') not in (None, 'API-Football'):
        raise ValueError('Seules les compétitions API-Football sont disponibles.')
    competitions = {}
    for code, item in (snapshot.get('competitions') or {}).items():
        try:
            league = _identity(code)
            if not isinstance(item, dict):
                continue
            competitions[league] = item
        except ValueError:
            continue
    histories = defaultdict(list)
    for row in rows:
        if row.get('provider') != 'api-football':
            continue
        league = str(row.get('league'))
        if league in competitions:
            histories[league].append(row)
    events, seen = defaultdict(list), set()
    has_raw = isinstance(snapshot.get('matches'), list)
    for item in snapshot.get('matches' if has_raw else 'fixtures', []):
        try:
            if has_raw:
                league = _identity(item['league']['id'])
                stamp = _stamp(item['fixture']['date'])
                mid = str(item['fixture']['id'])
            else:
                if item.get('provider', 'api-football') != 'api-football':
                    continue
                league, stamp, mid = _identity(item['league']), _stamp(item['timestamp']), str(item['match_id'])
            if league not in competitions or stamp.astimezone(zone).date() != day or (league, mid) in seen:
                continue
            seen.add((league, mid))
            events[league].append(stamp)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    future, finished, selected_ids = defaultdict(int), defaultdict(int), set()
    for fixture in snapshot.get('fixtures', []):
        try:
            if fixture.get('provider', 'api-football') != 'api-football':
                continue
            league, stamp = _identity(fixture['league']), _stamp(fixture['timestamp'])
            identity = league, str(fixture['match_id'])
            if league not in competitions or stamp.astimezone(zone).date() != day or identity in selected_ids:
                continue
            selected_ids.add(identity)
            if fixture.get('is_upcoming') and stamp > current:
                future[league] += 1
            if fixture.get('is_historical'):
                finished[league] += 1
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    output = []
    for league, item in competitions.items():
        first = min(events[league]) if events[league] else datetime.combine(day, datetime.min.time(), zone).astimezone(timezone.utc)
        before = min(first.date(), current.date()).isoformat()
        error = None
        try:
            assessment = readiness.assess_history(histories[league], league, size, before)
        except ValueError as problem:
            error = str(problem)
            assessment = dict(eligible=False, history_matches=None, history_groups=None,
                              missing_matches=None, missing_groups=None, reason=error)
        historical = day < current.astimezone(zone).date()
        available = finished[league] if historical else future[league]
        label = ('Simulation possible' if historical else 'Assez d’affiches à venir') if available >= size else 'Pas assez d’affiches'
        output.append(dict(league=league, competition=item.get('name') or item.get('competition') or league,
            country=item.get('country') or 'International', day=day.isoformat(), matches=len(events[league]),
            upcoming=future[league], historical=finished[league], available=available, size=size, before=before,
            history_matches=assessment['history_matches'], history_groups=assessment['history_groups'],
            history_ready=assessment['eligible'], missing_matches=assessment['missing_matches'],
            preparation=('À vérifier' if error else 'Prêt' if assessment['eligible'] else 'À compléter'),
            preparation_reason=assessment['reason'], fixture_ready=available >= size,
            fixture_state=f'{label} ({available}/{size})', historical_day=historical, history_issue=error))
    return select(output)


def select(items, *, country=None, query='', sort='matches'):
    """Pure country/search/sort controls; no positional identities are retained."""
    if sort not in SORTS:
        raise ValueError('Tri de compétition inconnu.')
    search = query.strip().casefold()
    values = [item for item in items if (country is None or item['country'] == country)
              and (not search or search in f"{item['league']} {item['competition']} {item['country']}".casefold())]
    alpha = lambda item:(item['competition'].casefold(), item['country'].casefold(), int(item['league']))
    key = (lambda item:(-item['matches'], *alpha(item))) if sort == 'matches' else (
        (lambda item:(-(item['history_matches'] if item['history_matches'] is not None else -1), *alpha(item)))
        if sort == 'history' else alpha)
    return sorted(values, key=key)
