"""Indexed, local API-Football storage with an explicit JSON migration.

The historical JSON is never modified by migration. An atomic rename publishes
a fully checked database; its active marker is the sole opt-in. Raw provider
payloads and their validated regulation-time projections are kept together.
Every write uses a short SQLite transaction. WAL readers keep a consistent
snapshot while another process imports a correction.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from itertools import islice
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from uuid import uuid4
import fixture_import_policy as import_policy


SCHEMA_VERSION = 1
PROJECTION_VERSION = 'api-football-regulation-v1'
FINISHED = ('FT', 'AET', 'PEN')


class FixtureStoreError(ValueError):
    """A safe local database error, without connection details."""


def database_path(path):
    path = Path(path)
    return path if path.suffix in ('.sqlite', '.sqlite3', '.db') else path.with_suffix('.sqlite3')


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


@contextmanager
def _connection(path, *, write=False, create=False):
    connection = None
    try:
        path = Path(path).resolve()
        if create:
            connection = sqlite3.connect(path, timeout=30, isolation_level=None)
        else:
            mode = 'rw' if write else 'ro'
            connection = sqlite3.connect(path.as_uri() + '?mode=' + mode, uri=True,
                                         timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA busy_timeout=30000')
        if create:
            # Set the persistent journal mode before publication. The last
            # temporary connection closes/checkpoints before atomic rename.
            connection.execute('PRAGMA journal_mode=WAL')
        if write:
            connection.execute('PRAGMA synchronous=FULL')
        else:
            connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        yield connection
        connection.commit()
    except (sqlite3.Error, OSError, json.JSONDecodeError):
        if connection is not None:
            connection.rollback()
        raise FixtureStoreError('La base locale API-Football est indisponible ou invalide.') from None
    except BaseException:
        if connection is not None:
            connection.rollback()
        raise
    finally:
        if connection is not None:
            connection.close()


def _meta(connection):
    meta = dict(connection.execute('SELECT key, value FROM metadata'))
    if (meta.get('schema_version') != str(SCHEMA_VERSION) or meta.get('active') != '1'
            or meta.get('projection_version') != PROJECTION_VERSION
            or not meta.get('revision', '').isdigit() or int(meta['revision']) < 1
            or len(meta.get('store_uuid', '')) != 32
            or any(value not in '0123456789abcdef' for value in meta.get('store_uuid', ''))):
        raise FixtureStoreError('La version de la base locale API-Football est incompatible.')
    return meta


def active_path(path):
    """Resolve an explicitly migrated store; do not create anything on reads."""
    target = database_path(path)
    if not target.exists():
        return None
    with _connection(target) as connection:
        _meta(connection)
    return target


def _schema(connection):
    # executescript commits implicitly: individual statements keep activation
    # inside the migration transaction, even if projection fails halfway.
    statements = (
        'CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)',
        '''CREATE TABLE fixtures (
            ordinal INTEGER PRIMARY KEY AUTOINCREMENT, match_id TEXT NOT NULL UNIQUE,
            kickoff_utc TEXT, league TEXT, season INTEGER, status TEXT,
            raw_json TEXT NOT NULL, normalized_json TEXT, issue TEXT,
            expected INTEGER NOT NULL DEFAULT 0 CHECK(expected IN (0,1)))''',
        'CREATE INDEX fixture_kickoff ON fixtures(kickoff_utc, match_id)',
        'CREATE INDEX fixture_league_kickoff ON fixtures(league, kickoff_utc, match_id)',
        'CREATE INDEX fixture_status_kickoff ON fixtures(status, kickoff_utc)',
        'CREATE INDEX fixture_normalized_league ON fixtures(league, kickoff_utc) WHERE normalized_json IS NOT NULL',
        '''CREATE TABLE season_imports (
            league TEXT NOT NULL, season INTEGER NOT NULL, manifest_json TEXT NOT NULL,
            PRIMARY KEY(league, season))''',
        '''CREATE TABLE calendar_updates (
            day TEXT NOT NULL, timezone TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(day, timezone))''',
    )
    for statement in statements:
        connection.execute(statement)
    _import_schema(connection)


def _import_schema(connection):
    """Additive, lazy-on-write schema; never backfill or alter reads."""
    connection.execute('''CREATE TABLE IF NOT EXISTS fixture_import_revisions (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, match_id TEXT NOT NULL,
        observed_at_utc TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL UNIQUE)''')
    connection.execute('CREATE INDEX IF NOT EXISTS fixture_import_match ON fixture_import_revisions(match_id,sequence)')
    for action in ('UPDATE', 'DELETE'):
        connection.execute(f'''CREATE TRIGGER IF NOT EXISTS fixture_import_no_{action.lower()}
            BEFORE {action} ON fixture_import_revisions
            BEGIN SELECT RAISE(ABORT, 'Import observations are append-only'); END''')


def _insert_observation(connection, record):
    import_policy.validate_record(record)
    connection.execute('''INSERT INTO fixture_import_revisions(match_id,observed_at_utc,payload,digest)
                          VALUES (?,?,?,?)''',
                       (record['match_id'], record['observed_at_utc'], _json(record), record['id']))


def _observations(connection, ids=None):
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'fixture_import_revisions' not in tables:
        return []
    rows = []
    if ids is None:
        rows = connection.execute('SELECT sequence,payload,digest FROM fixture_import_revisions ORDER BY sequence').fetchall()
    else:
        identities = list(dict.fromkeys(map(str, ids)))
        for offset in range(0, len(identities), 500):
            chunk = identities[offset:offset+500]
            rows.extend(connection.execute('SELECT sequence,payload,digest FROM fixture_import_revisions '
                'WHERE match_id IN (' + ','.join('?' for _ in chunk) + ') ORDER BY sequence', chunk).fetchall())
        rows.sort(key=lambda row: row['sequence'])
    result, latest = [], {}
    for row in rows:
        record = import_policy.validate_record(json.loads(row['payload']))
        if record['id'] != row['digest'] or record['previous_observation'] != latest.get(record['match_id']):
            raise FixtureStoreError('La chronologie des imports locaux est incohérente.')
        latest[record['match_id']] = record['id']
        result.append(record)
    return result


def _projection(raw):
    import api_football as api
    mid = api._numeric_id(raw['fixture']['id'], 'du match')
    event = raw.get('fixture', {})
    status = api._status_short(raw)
    stamp = None
    try:
        value = datetime.fromisoformat(event['date'].replace('Z', '+00:00'))
        if value.tzinfo is not None:
            stamp = value.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds')
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    league, season = None, None
    try:
        league = api._numeric_id(raw['league']['id'], 'de compétition')
        season = raw['league'].get('season')
        if type(season) is not int:
            season = None
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    normalized, issue = None, None
    expected = int(status is None or status in FINISHED)
    conflict = import_policy.quality_error(raw)
    if conflict:
        expected, issue = 1, conflict
    elif status is None:
        issue = 'Statut du match absent ou invalide'
    elif status in FINISHED:
        try:
            normalized = _json(api.normalize_fixture(raw))
        except ValueError as error:
            issue = str(error)
    return (mid, stamp, league, season, status, _json(raw), normalized, issue, expected)


_UPSERT = '''INSERT INTO fixtures
    (match_id,kickoff_utc,league,season,status,raw_json,normalized_json,issue,expected)
    VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(match_id) DO UPDATE SET
    kickoff_utc=excluded.kickoff_utc, league=excluded.league, season=excluded.season,
    status=excluded.status, raw_json=excluded.raw_json,
    normalized_json=excluded.normalized_json, issue=excluded.issue, expected=excluded.expected'''


def _set_meta(connection, values):
    connection.executemany('INSERT OR REPLACE INTO metadata(key,value) VALUES (?,?)',
                           [(key, str(value)) for key, value in values.items()])


def _touch(connection, meta):
    _set_meta(connection, dict(revision=int(meta['revision']) + 1,
                              updated_at=datetime.now(timezone.utc).isoformat(timespec='seconds')))


def migrate_json(path):
    """Activate an indexed copy once, with source hash and integrity verification.

    The existing JSON writer lock also covers the authority switch, so an import
    that waited for migration must recheck the active database before writing.
    A failed migration leaves only an unreferenced temporary file, never an
    active partial database. Re-running against an active store is a no-op.
    """
    import api_football as api
    import api_match_workspace as calendar
    path = Path(path).resolve()
    target = database_path(path)
    if path == target:
        raise FixtureStoreError('La migration attend un fichier source JSON.')
    with api._store_write_lock(path):
        if active_path(path) is not None:
            return dict(statistics(target), already_active=True, database=str(target))
        payload = api._load_store(path)
        if not path.exists():
            raise FixtureStoreError('Le fichier JSON source est absent ; aucune migration effectuée.')
        content_hash = sha256(path.read_bytes()).hexdigest()
        # Full validation and projection happen before publishing the authority.
        projections = [_projection(raw) for raw in payload['fixtures']]
        raw_by_id = {api._numeric_id(raw['fixture']['id'], 'du match'): raw for raw in payload['fixtures']}
        markers = api._valid_season_imports(payload, raw_by_id)
        updates = calendar._calendar_updates(path)
        handle = tempfile.NamedTemporaryFile(prefix='.fixtures-migration-', suffix='.sqlite3',
                                             dir=target.parent, delete=False)
        temporary = Path(handle.name)
        handle.close()
        try:
            with _connection(temporary, write=True, create=True) as connection:
                _schema(connection)
                connection.executemany(_UPSERT, projections)
                latest = {}
                for record in payload.get('import_revisions', []):
                    import_policy.validate_record(record)
                    if record['previous_observation'] != latest.get(record['match_id']):
                        raise FixtureStoreError('La chronologie des imports locaux est incohérente.')
                    _insert_observation(connection, record)
                    latest[record['match_id']] = record['id']
                connection.executemany('INSERT INTO season_imports VALUES (?,?,?)',
                    [(marker['competition_code'], marker['season'], _json(marker)) for marker in markers])
                connection.executemany('INSERT INTO calendar_updates VALUES (?,?,?)',
                    [(day, zone, stamp) for (day, zone), stamp in updates.items()])
                _set_meta(connection, dict(schema_version=SCHEMA_VERSION,
                    projection_version=PROJECTION_VERSION, active=1, revision=1,
                    store_uuid=uuid4().hex, source_sha256=content_hash, source_path=str(path),
                    source_size=path.stat().st_size,
                    migrated_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
                    updated_at=payload.get('updated_at') or ''))
                if connection.execute('SELECT count(*) FROM fixtures').fetchone()[0] != len(projections):
                    raise FixtureStoreError('Le nombre de matchs migrés ne correspond pas au JSON source.')
                if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise FixtureStoreError('La vérification de la base migrée a échoué.')
            with temporary.open('rb') as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary.exists():
                temporary.unlink()
        return dict(statistics(target), already_active=False, database=str(target), source_sha256=content_hash)


def _filters(*, start=None, end=None, leagues=None, statuses=None, ids=None):
    clauses, values = [], []
    for operator, value in (('>=', start), ('<', end)):
        if value is not None:
            if isinstance(value, datetime):
                if value.tzinfo is None:
                    raise ValueError('Une borne temporelle doit préciser son fuseau horaire.')
                value = value.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds')
            clauses.append('kickoff_utc ' + operator + ' ?')
            values.append(value)
    for column, selected in (('league', leagues), ('status', statuses), ('match_id', ids)):
        if selected is not None:
            selected = tuple(str(value) for value in selected)
            if not selected:
                clauses.append('0')
            else:
                clauses.append(column + ' IN (' + ','.join('?' for _ in selected) + ')')
                values.extend(selected)
    return clauses, values


def _selected_rows(connection, columns, filters):
    """Stream bounded ID queries within the caller's single read transaction.

    Large journals can contain more IDs than SQLite's SQL-variable limit.
    Chunks avoid that limit, keep a stable MVCC snapshot and do not construct
    another full ID array. Repeated IDs across chunks still select one record.
    """
    filters = dict(filters)
    identities = filters.pop('ids', None)
    if identities is None:
        chunks = (None,)
    else:
        iterator = iter(identities)
        chunks = iter(lambda: list(islice(iterator, 500)), [])
    seen = set()
    for chunk in chunks:
        clauses, values = _filters(**filters, ids=chunk)
        query = 'SELECT ordinal,' + columns + ' FROM fixtures'
        query += ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        query += ' ORDER BY ordinal'
        for row in connection.execute(query, values):
            ordinal = row['ordinal']
            if ordinal not in seen:
                if identities is not None:
                    seen.add(ordinal)
                yield row


def raw_fixtures(path, **filters):
    with _connection(path) as connection:
        _meta(connection)
        rows = {row['ordinal']: json.loads(row['raw_json'])
                for row in _selected_rows(connection, 'raw_json', filters)}
        return [rows[ordinal] for ordinal in sorted(rows)]


def matches(path, **filters):
    with _connection(path) as connection:
        _meta(connection)
        rows, issues, expected = [], {}, 0
        for row in _selected_rows(connection, 'match_id, normalized_json, issue, expected', filters):
            expected += row['expected']
            if row['normalized_json'] is not None:
                rows.append(json.loads(row['normalized_json']))
            if row['issue'] is not None:
                issues[row['ordinal']] = dict(match_id=row['match_id'], reason=row['issue'])
        rows.sort(key=lambda row: (row['timestamp'], int(row['match_id'])))
        issues = [issues[ordinal] for ordinal in sorted(issues)]
        return rows, dict(expected=expected, matched=len(rows), issues=issues)


def statistics(path):
    with _connection(path) as connection:
        meta = _meta(connection)
        return _statistics(connection, meta)


def _statistics(connection, meta):
    # Both counts use small covering indexes. Counting the large JSON column
    # would force a full payload-table scan even on the calendar-only screen.
    stored = connection.execute('SELECT count(*) FROM fixtures').fetchone()[0]
    history = dict(connection.execute('SELECT league, count(*) FROM fixtures'
                    ' WHERE normalized_json IS NOT NULL GROUP BY league'))
    return dict(stored_count=stored, usable_count=sum(history.values()), history_counts=history,
                updated_at=meta.get('updated_at') or None, revision=int(meta['revision']),
                store_uuid=meta['store_uuid'])


def calendar_snapshot(path, *, start, end):
    """Day payloads, counts and refresh times from one committed generation."""
    clauses, values = _filters(start=start, end=end)
    where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
    with _connection(path) as connection:
        meta = _meta(connection)  # First SELECT fixes the WAL read snapshot.
        rows = connection.execute('SELECT raw_json, normalized_json FROM fixtures'
                                  + where + ' ORDER BY ordinal', values).fetchall()
        return dict(matches=[json.loads(row['raw_json']) for row in rows],
                    usable=[json.loads(row['normalized_json']) for row in rows
                            if row['normalized_json'] is not None],
                    stats=_statistics(connection, meta),
                    calendar_updates={(row[0], row[1]): row[2] for row in connection.execute(
                        'SELECT day, timezone, updated_at FROM calendar_updates')})


def metadata(path):
    with _connection(path) as connection:
        return dict(_meta(connection))


def signature(path):
    meta = metadata(path)
    # A WAL commit increments revision in the same transaction as the data.
    # store_uuid also changes if a different migrated database replaces this one.
    return str(Path(path)), int(meta['revision']), meta['store_uuid']


def season_imports(path):
    with _connection(path) as connection:
        _meta(connection)
        return [json.loads(row[0]) for row in connection.execute(
            'SELECT manifest_json FROM season_imports ORDER BY rowid')]


def calendar_updates(path):
    with _connection(path) as connection:
        _meta(connection)
        return {(row[0], row[1]): row[2] for row in connection.execute(
            'SELECT day, timezone, updated_at FROM calendar_updates')}


def mark_calendar_refresh(path, day, timezone_name, updated_at):
    # Metadata is authoritative too: do not let an invalid timestamp turn a
    # successful import into apparently fresh calendar data.
    from datetime import date
    from zoneinfo import ZoneInfo
    try:
        day = date.fromisoformat(day).isoformat()
        ZoneInfo(timezone_name)
        stamp = datetime.fromisoformat(updated_at.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            raise ValueError()
        updated_at = stamp.astimezone(timezone.utc).isoformat(timespec='seconds')
    except (ValueError, TypeError, AttributeError, KeyError):
        raise FixtureStoreError('La date d’actualisation du calendrier est invalide.') from None
    with _connection(path, write=True) as connection:
        meta = _meta(connection)
        connection.execute('''INSERT INTO calendar_updates VALUES (?,?,?)
            ON CONFLICT(day,timezone) DO UPDATE SET updated_at=excluded.updated_at
            WHERE excluded.updated_at > calendar_updates.updated_at''', (day, timezone_name, updated_at))
        _touch(connection, meta)


def upsert(path, fixtures, *, season_imports=None):
    import api_football as api
    # Validate the whole incoming batch before beginning any write.
    incoming, received = {}, []
    try:
        for raw in fixtures:
            incoming[api._numeric_id(raw['fixture']['id'], 'du match')] = raw
            _projection(raw)  # Validate IDs and JSON before any transaction.
            received.append(raw)
    except (KeyError, TypeError, ValueError):
        raise api.ApiFootballError('Un match API-Football à enregistrer a un identifiant invalide.') from None
    now = datetime.now(timezone.utc).isoformat(timespec='microseconds')
    markers = []
    if season_imports is not None:
        try:
            if not isinstance(season_imports, list):
                raise ValueError()
            for entry in season_imports:
                marker = api._validate_season_import(entry, incoming, imported_at=now)
                expected = {mid for mid, raw in incoming.items()
                    if api._season_identity(raw) == (marker['competition_code'], marker['season'])}
                if set(marker['match_ids']) != expected:
                    raise ValueError()
                markers.append(marker)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise api.ApiFootballError('Le manifeste de saison API-Football est invalide ou ne correspond pas au lot reçu.') from None
    with _connection(path, write=True) as connection:
        # Take the import-recorded timestamp only after the writer lock. It
        # still does not attest provider publication or transaction commit.
        now = datetime.now(timezone.utc).isoformat(timespec='microseconds')
        markers = [dict(marker,imported_at=now) for marker in markers]
        meta = _meta(connection)
        _import_schema(connection)
        previous = {row['match_id']: json.loads(row['raw_json']) for row in
                    _selected_rows(connection, 'match_id,raw_json', dict(ids=incoming))}
        latest = {record['match_id']: record for record in _observations(connection, ids=incoming)}
        projections = []
        for raw in received:
            mid = api._numeric_id(raw['fixture']['id'], 'du match')
            old = previous.get(mid)
            try:
                selected, decision, reason = import_policy.decide(old, raw, api.normalize_fixture, latest=latest.get(mid))
                record = import_policy.observation(old, raw, selected, decision, reason, now, latest.get(mid))
            except (ValueError, TypeError, KeyError):
                raise api.ApiFootballError('Le relevé reçu ou son historique de qualité est invalide.') from None
            if record is not None:
                _insert_observation(connection, record)
                latest[mid] = record
            projections.append(_projection(selected))
            previous[mid] = selected
        connection.executemany(_UPSERT, projections)
        # Retain only manifests whose members still belong to that season.
        for saved in connection.execute('SELECT league, season, manifest_json FROM season_imports').fetchall():
            marker = json.loads(saved['manifest_json'])
            ids = marker['match_ids']
            valid_count = 0
            # Bound parameter counts for old SQLite builds as well as big seasons.
            for offset in range(0, len(ids), 500):
                chunk = ids[offset:offset+500]
                valid_count += connection.execute('SELECT count(*) FROM fixtures WHERE league=? AND season=?'
                    + " AND (issue IS NULL OR issue NOT LIKE 'Quarantaine import : %')"
                    + ' AND match_id IN (' + ','.join('?' for _ in chunk) + ')',
                    [saved['league'], saved['season']] + chunk).fetchone()[0]
            if valid_count != len(ids):
                connection.execute('DELETE FROM season_imports WHERE league=? AND season=?',
                                   (saved['league'], saved['season']))
        for marker in markers:
            # Validate against the retained final version, not a stale live state.
            ids = marker['match_ids']
            actual = {}
            for offset in range(0, len(ids), 500):
                chunk = ids[offset:offset+500]
                actual.update((row['match_id'], json.loads(row['raw_json'])) for row in connection.execute(
                    'SELECT match_id, raw_json FROM fixtures WHERE match_id IN ('
                    + ','.join('?' for _ in chunk) + ')', chunk))
            try:
                api._validate_season_import(marker, actual)
            except (KeyError, TypeError, ValueError, AttributeError):
                raise api.ApiFootballError('Le manifeste de saison API-Football est invalide ou ne correspond pas au lot reçu.') from None
            connection.execute('''INSERT INTO season_imports VALUES (?,?,?)
                ON CONFLICT(league,season) DO UPDATE SET manifest_json=excluded.manifest_json''',
                (marker['competition_code'], marker['season'], _json(marker)))
        _touch(connection, meta)
        return connection.execute('SELECT count(*) FROM fixtures').fetchone()[0]


def export_snapshot(path):
    """One consistent full export for an explicit audit/backup, never page loads."""
    with _connection(path) as connection:
        meta = _meta(connection)
        fixtures = [json.loads(row[0]) for row in connection.execute('SELECT raw_json FROM fixtures ORDER BY ordinal')]
        markers = [json.loads(row[0]) for row in connection.execute('SELECT manifest_json FROM season_imports ORDER BY rowid')]
        return dict(version=1, fixtures=fixtures, season_imports=markers, updated_at=meta.get('updated_at') or None,
                    import_revisions=_observations(connection))


def import_observations(path, *, ids=None):
    """Read recorded receipt/recovery events; never create or backfill history."""
    with _connection(path) as connection:
        _meta(connection)
        return _observations(connection, ids=ids)


def import_quarantines(path):
    with _connection(path) as connection:
        _meta(connection)
        return [json.loads(row[0]) for row in connection.execute(
            "SELECT raw_json FROM fixtures WHERE issue LIKE 'Quarantaine import : %' ORDER BY ordinal")]


def review_import(path, match_id, *, choice, expected_observation, reason):
    import api_football as api
    with _connection(path, write=True) as connection:
        meta = _meta(connection)
        row = connection.execute('SELECT raw_json FROM fixtures WHERE match_id=?', (match_id,)).fetchone()
        events = _observations(connection, ids=[match_id])
        selected, event = import_policy.review_selection(json.loads(row[0]) if row else {},
            events[-1] if events else None, choice=choice, expected_observation=expected_observation,
            reason=reason, normalize=api.normalize_fixture,
            observed_at=datetime.now(timezone.utc).isoformat(timespec='microseconds'))
        connection.execute(_UPSERT, _projection(selected))
        _insert_observation(connection, event)
        _touch(connection, meta)
        return event
