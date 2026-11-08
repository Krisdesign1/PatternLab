"""Versioned, bounded-memory reads for the Linux collector.

The canonical store, import decisions, normalization and model implementations
remain unchanged. SQLite reads keep one MVCC transaction and decode one raw
record at a time in the original ordinal order. JSON remains a compatibility
fallback; it is not claimed to be a streaming format.
"""
from collections import Counter, defaultdict
from datetime import date
from heapq import merge
import json

import api_football as api
import api_match_workspace as workspace
import fixture_store


VERSION = 'worker-fixture-io-v1'


def _iter_connection(connection, filters):
    if filters.get('ids') is not None:
        identities = tuple(dict.fromkeys(map(str, filters['ids'])))
        streams = [fixture_store._selected_rows(connection, 'raw_json',
            dict(filters, ids=identities[offset:offset+500]))
            for offset in range(0,len(identities),500)]
        selected_rows = merge(*streams,key=lambda row:row['ordinal'])
    else:
        selected_rows = fixture_store._selected_rows(connection, 'raw_json', filters)
    for row in selected_rows:
        yield json.loads(row['raw_json'])


def iter_fixtures(path, **filters):
    """Yield canonical raw records with the same filters as load_fixtures.

    The generator must be exhausted or closed by the caller; no transaction
    survives normal exhaustion, an exception, or an explicit close().
    """
    database = api.active_store(path)
    if database is None:
        yield from api.load_fixtures(path, **filters)
        return
    try:
        with fixture_store._connection(database) as connection:
            fixture_store._meta(connection)
            # Canonical raw_fixtures sorts all selected ordinals after its ID
            # chunks. Merge chunk cursors instead: keep that global order while
            # retaining at most one undecoded raw payload per chunk.
            yield from _iter_connection(connection, filters)
    except fixture_store.FixtureStoreError as error:
        raise api.ApiFootballError(str(error)) from None


def inventory(path, now, *, keep_leagues=None, include_history=True):
    """Match the worker inventory without retaining the full raw population.

    Metadata keeps the original last-ordinal-wins rule across every league.
    Finished records are all normalized so global error counters stay exact;
    only requested leagues' historical normalized records are kept in memory.
    """
    return _inventory_values(iter_fixtures(path), now, keep_leagues=keep_leagues,
                             include_history=include_history)


def _inventory_values(values, now, *, keep_leagues=None, include_history=True):
    selected = None if keep_leagues is None else {str(value) for value in keep_leagues}
    info, rows, issues = {}, defaultdict(list), Counter()
    for item in values:
        try:
            league = api._numeric_id(item['league']['id'], 'de compétition')
            info[league] = dict(league=league, competition=item['league'].get('name') or league,
                                country=item['league'].get('country') or 'International')
        except (KeyError, TypeError, ValueError):
            continue
        if api._status_short(item) in api.FINISHED_STATUSES:
            try:
                row = api.normalize_fixture(item)
                if (include_history and (selected is None or league in selected)
                        and row['timestamp'][:10] < now.date().isoformat()):
                    rows[league].append(row)
            except (ValueError, TypeError):
                issues[league] += 1
    for values in rows.values():
        values.sort(key=lambda row: (row['timestamp'], int(row['match_id'])))
    return info, rows, issues


def forecast_fixtures(path, day, sizes, plans, *, timezone_name=workspace.DEFAULT_TIMEZONE):
    """Keep today's fixtures plus every ID referenced by today's saved plans.

    A previously anchored match may have moved out of today's calendar. It is
    still needed to preserve the original group's ordering and rejection, even
    though it must not become a new fixture on the selected day.
    """
    if type(day) is not date:
        raise ValueError('A calendar day is required.')
    values = workspace.local_day_matches(day, timezone_name, path=path)
    retained = {str(value.get('fixture', {}).get('id')) for value in values}
    prefix = day.isoformat()+'/'
    endings = {'/'+str(size) for size in sizes}
    identities = {str(mid) for key, blocks in plans.items()
                  if key.startswith(prefix) and any(key.endswith(ending) for ending in endings)
                  for block in blocks for mid in block}
    missing = identities-retained
    if missing:
        values.extend(iter_fixtures(path, ids=sorted(missing, key=int)))
    return values


def forecast_inventory(path, day, sizes, plans, now, *, timezone_name=workspace.DEFAULT_TIMEZONE):
    """Read calendar, anchored IDs and scoped history from one MVCC snapshot."""
    start, end = workspace._bounds(day, timezone_name)
    def read(stream):
        values = workspace._day_rows(stream(start=start, end=end), day, timezone_name)
        retained = {str(value.get('fixture', {}).get('id')) for value in values}
        prefix, endings = day.isoformat()+'/', {'/'+str(size) for size in sizes}
        missing = {str(mid) for key, blocks in plans.items()
                   if key.startswith(prefix) and any(key.endswith(ending) for ending in endings)
                   for block in blocks for mid in block}-retained
        if missing:
            values.extend(stream(ids=sorted(missing, key=int)))
        day_info = _inventory_values(workspace._day_rows(values, day, timezone_name), now)[0]
        info, rows, issues = _inventory_values(stream(), now, keep_leagues=set(day_info))
        return values, day_info, info, rows, issues
    database = api.active_store(path)
    if database is None:
        # One captured JSON population, preserving legacy read coherence.
        raw = api.load_fixtures(path)
        return read(lambda **filters: iter(api._select_fixtures(raw, **filters)))
    try:
        with fixture_store._connection(database) as connection:
            fixture_store._meta(connection)
            return read(lambda **filters: _iter_connection(connection, filters))
    except fixture_store.FixtureStoreError as error:
        raise api.ApiFootballError(str(error)) from None
