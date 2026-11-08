"""Durable, append-only context versions, separate from the fixture projection.

Only the new pure validators are imported. No API, serving or source-database
module is used. Writers explicitly initialize this destination, then manage a
transaction around append_observation and update_checkpoint. Neither function
commits. Omitting a block creates no component version; explicit null/empty,
invalid or quarantined observations mask older versions without deleting them.
"""
from datetime import datetime, timezone
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import context_event_quality as event_quality
import prematch_context_quality as prematch_quality


VERSION = 'context-observation-store-v1'
KINDS = ('events', 'lineups', 'injuries')
PROVENANCE = ('local_import_recorded', 'recovered_at_first_tracked_import')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode()).hexdigest()


def _instant(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
        if not isinstance(parsed, datetime) or parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc).isoformat(timespec='microseconds')
    except (ValueError, TypeError, OverflowError):
        return None


def _identifier(value):
    if (type(value) not in (int, str) or not str(value).isascii()
            or not str(value).isdigit() or int(value) <= 0):
        return None
    return str(int(value))


def _nonempty(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name + ' must be a nonempty string.')
    return value


def _metadata(raw):
    raw = raw if isinstance(raw, dict) else {}
    fixture = raw.get('fixture'); fixture = fixture if isinstance(fixture, dict) else {}
    league = raw.get('league'); league = league if isinstance(league, dict) else {}
    teams = raw.get('teams'); teams = teams if isinstance(teams, dict) else {}
    home = teams.get('home'); home = home if isinstance(home, dict) else {}
    away = teams.get('away'); away = away if isinstance(away, dict) else {}
    status = fixture.get('status'); status = status if isinstance(status, dict) else {}
    short = status.get('short')
    return dict(fixture_id=_identifier(fixture.get('id')), league_id=_identifier(league.get('id')),
                home_id=_identifier(home.get('id')), away_id=_identifier(away.get('id')),
                kickoff_utc=_instant(fixture.get('date')),
                status=short.strip().upper() if isinstance(short, str) else None)


def connect(path, *, readonly=False):
    """Open an existing archive. Read-only access never creates a file."""
    target = Path(path).resolve(strict=True)
    connection = sqlite3.connect(target.as_uri() + ('?mode=ro' if readonly else '?mode=rw'),
                                 uri=True, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    if readonly:
        connection.execute('PRAGMA query_only=ON')
    try:
        row = connection.execute("SELECT value FROM context_metadata WHERE key='schema_version'").fetchone()
        if row is None or row[0] != VERSION:
            raise ValueError('Unsupported context archive schema.')
    except BaseException:
        connection.close()
        raise
    return connection


def initialize(path):
    """Explicitly create a destination archive; never modify a source store."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target.resolve().as_uri() + '?mode=rwc', uri=True, timeout=15)
    try:
        existing = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if existing and 'context_metadata' not in existing:
            raise ValueError('Destination is an existing non-context database.')
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA journal_mode=WAL')
        with connection:
            connection.execute('CREATE TABLE IF NOT EXISTS context_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            prior = connection.execute("SELECT value FROM context_metadata WHERE key='schema_version'").fetchone()
            if prior is not None and prior[0] != VERSION:
                raise ValueError('Unsupported context archive schema.')
            connection.execute("INSERT OR IGNORE INTO context_metadata VALUES('schema_version',?)", (VERSION,))
            connection.execute('''CREATE TABLE IF NOT EXISTS context_receipts(
                id INTEGER PRIMARY KEY,source_uuid TEXT NOT NULL,sequence INTEGER NOT NULL,
                receipt_variant TEXT NOT NULL,record_id TEXT NOT NULL,fixture_id TEXT NOT NULL,
                observed_at TEXT,provenance TEXT NOT NULL,receipt_sha256 TEXT NOT NULL,
                raw_sha256 TEXT NOT NULL,metadata_json TEXT NOT NULL,validation_error TEXT,
                UNIQUE(source_uuid,sequence,receipt_variant))''')
            receipt_columns = {row[1] for row in connection.execute('PRAGMA table_info(context_receipts)')}
            if 'canonical_receipt_json' not in receipt_columns:
                # Additive schema only: old immutable rows remain untouched.
                connection.execute('ALTER TABLE context_receipts ADD COLUMN canonical_receipt_json TEXT')
            connection.execute('''CREATE TABLE IF NOT EXISTS context_observations(
                id INTEGER PRIMARY KEY,receipt_id INTEGER NOT NULL REFERENCES context_receipts(id),
                source_uuid TEXT NOT NULL,sequence INTEGER NOT NULL,receipt_variant TEXT NOT NULL,
                fixture_id TEXT NOT NULL,kind TEXT NOT NULL,observed_at TEXT,provenance TEXT NOT NULL,
                state TEXT NOT NULL,availability TEXT NOT NULL,payload_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL,quality_json TEXT NOT NULL,quality_sha256 TEXT NOT NULL,
                observation_sha256 TEXT NOT NULL,UNIQUE(source_uuid,sequence,receipt_variant,kind))''')
            connection.execute('''CREATE INDEX IF NOT EXISTS context_fixture_time
                ON context_observations(fixture_id,kind,observed_at,sequence)''')
            connection.execute('''CREATE TABLE IF NOT EXISTS context_checkpoints(
                source_uuid TEXT PRIMARY KEY,sequence INTEGER NOT NULL,last_digest TEXT NOT NULL)''')
            for table in ('context_receipts', 'context_observations'):
                for action in ('UPDATE', 'DELETE'):
                    connection.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()}
                        BEFORE {action} ON {table} BEGIN
                        SELECT RAISE(ABORT,'Context observations are append-only'); END''')
    finally:
        connection.close()


@lru_cache(maxsize=2)
def _validator_identity(kind):
    module = event_quality if kind == 'events' else prematch_quality
    return dict(version=getattr(module, 'VERSION', 'prematch-context-quality-v1'),
                code_sha256=sha256(Path(module.__file__).read_bytes()).hexdigest())


def _quality(raw, kind, observed, global_error):
    if kind == 'events':
        report = event_quality.audit_events(raw)
        structural = report['goal_compatible']
    elif kind == 'lineups':
        report = prematch_quality.audit_lineups(raw, observed_at=observed)
        structural = report['complete_starting_elevens']
    else:
        report = prematch_quality.audit_injuries(raw, observed_at=observed)
        timing_issues = set(report['timing']['issues'])
        temporal_issues = set(report['prematch_issues'])
        structural = (report['availability'] == 'present'
            and not set(report['issues']) - timing_issues - temporal_issues
            and 'injury_fixture_binding_unknown' not in report['unknown'])
    return dict(validator=_validator_identity(kind), report=report,
                structural_eligible=bool(structural and global_error is None),
                qualification_scope=('goal_score_compatibility_only' if kind == 'events'
                    else 'starting_xi_structure_only' if kind == 'lineups' else 'injury_row_structure_only'),
                validation_error=global_error, model_training_ready=False,
                provider_publication_time_proven=False)


def _receipt_fields(row):
    return {key: row[key] for key in ('source_uuid', 'sequence', 'receipt_variant',
        'record_id', 'fixture_id', 'observed_at', 'provenance', 'raw_sha256', 'validation_error')} | {
        'metadata': json.loads(row['metadata_json'])}


def _legacy_supplied_times(observed):
    """Bounded formats used by the original UTC sync; never invent an offset."""
    if observed is None:
        return (None,)
    normalized = _instant(observed)
    if normalized != observed:
        return ()
    automatic = datetime.fromisoformat(observed).isoformat()
    return tuple(dict.fromkeys((normalized, automatic, normalized.replace('+00:00', 'Z'),
                               automatic.replace('+00:00', 'Z'))))


def _receipt_valid(row, *, legacy_supplied_time=None):
    """Verify receipt indexes against its persisted or inferable canonical body.

    Legacy receipts did not retain the supplied time string. Only bounded,
    equivalent UTC formats are inferred for reads. Exact supplied text may be
    checked during a replay, where the caller still has that original value.
    Non-inferable legacy representations fail closed; old rows are not changed.
    """
    try:
        fields = _receipt_fields(row)
        body = row['canonical_receipt_json'] if 'canonical_receipt_json' in row.keys() else None
        if body is not None:
            decoded = json.loads(body)
            expected = dict(fields, supplied_observed_at=fields['observed_at'])
            return decoded == expected and canonical(decoded) == body and digest(decoded) == row['receipt_sha256']
        candidates = list(_legacy_supplied_times(fields['observed_at']))
        if legacy_supplied_time is not None and _instant(legacy_supplied_time) == fields['observed_at']:
            candidates.append(legacy_supplied_time)
        return any(digest(dict(fields, supplied_observed_at=value)) == row['receipt_sha256'] for value in candidates)
    except (ValueError, TypeError, KeyError, OverflowError):
        return False


def append_observation(connection, *, source_uuid, sequence, record_id, observed_at,
                       raw, validation_error=None, provenance='local_import_recorded',
                       fixture_id=None, receipt_variant='received'):
    """Append one receipt and its explicitly present blocks; never commit.

    ``fixture_id`` binds an invalid payload to its source index when available.
    An unknown/naive observation time is retained as an invalidation with NULL
    time; it is never replaced by a kickoff, current time or recovered old time.
    Recovered baselines use receipt_variant='recovered_baseline' and their own
    provenance; their availability starts at the first tracked receipt.
    Invalidating receipts create all three component versions, even on omission.
    Exact replays return inserted=False; a changed receipt at the same key raises.
    """
    source_uuid = _nonempty(source_uuid, 'source_uuid')
    record_id = _nonempty(record_id, 'record_id')
    if type(sequence) is not int or sequence < 1:
        raise ValueError('sequence must be a positive integer.')
    if provenance not in PROVENANCE:
        raise ValueError('Unknown context provenance.')
    if receipt_variant not in ('received', 'recovered_baseline'):
        raise ValueError('Unknown receipt variant.')
    if (receipt_variant == 'recovered_baseline') != (provenance == 'recovered_at_first_tracked_import'):
        raise ValueError('Recovered provenance requires the recovered-baseline variant.')
    if validation_error is not None:
        validation_error = _nonempty(validation_error, 'validation_error')
    metadata = _metadata(raw)
    bound_id = _identifier(fixture_id) if fixture_id is not None else metadata['fixture_id']
    if bound_id is None:
        raise ValueError('A valid source-bound fixture_id is required for archival invalidation.')
    observed = _instant(observed_at)
    errors = [validation_error] if validation_error else []
    if observed is None:
        errors.append('observation_time_unknown_or_naive')
    if (metadata['fixture_id'] != bound_id or metadata['league_id'] is None
            or metadata['home_id'] is None or metadata['away_id'] is None
            or metadata['home_id'] == metadata['away_id'] or metadata['kickoff_utc'] is None
            or metadata['status'] is None):
        errors.append('fixture_identity_or_metadata_invalid')
    if not isinstance(raw, dict) or raw.get('provider') not in (None, 'api-football', 'API-Football'):
        errors.append('foreign_or_malformed_provider_fixture')
    annotation = raw.get('_patternlab_import') if isinstance(raw, dict) else None
    if isinstance(annotation, dict) and annotation.get('quarantined') is True:
        errors.append('fixture_quarantined')
    elif annotation is not None and (not isinstance(annotation, dict)
            or 'quarantined' in annotation and type(annotation['quarantined']) is not bool):
        errors.append('fixture_quality_annotation_malformed')
    global_error = ';'.join(sorted(set(errors))) if errors else None
    # A canonical input is required for reproducible immutable evidence. Invalid
    # provider structure can be JSON, but non-JSON Python values cannot be archived.
    supplied_time = observed_at.isoformat() if isinstance(observed_at, datetime) else observed_at
    receipt = dict(source_uuid=source_uuid, sequence=sequence, receipt_variant=receipt_variant,
                   record_id=record_id, fixture_id=bound_id, observed_at=observed,
                   supplied_observed_at=observed,
                   provenance=provenance, raw_sha256=digest(raw), metadata=metadata,
                   validation_error=global_error)
    receipt_hash = digest(receipt)
    old = connection.execute('''SELECT * FROM context_receipts
        WHERE source_uuid=? AND sequence=? AND receipt_variant=?''',
        (source_uuid, sequence, receipt_variant)).fetchone()
    if old is not None:
        if (_receipt_fields(old) != {key: value for key, value in receipt.items() if key != 'supplied_observed_at'}
                or not _receipt_valid(old, legacy_supplied_time=supplied_time)):
            raise ValueError('Immutable context receipt collision.')
        return dict(inserted=False, observations_inserted=0, fixture_id=bound_id,
                    validation_error=global_error)
    cursor = connection.execute('''INSERT INTO context_receipts
        (source_uuid,sequence,receipt_variant,record_id,fixture_id,observed_at,provenance,
         receipt_sha256,raw_sha256,metadata_json,validation_error,canonical_receipt_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
        (source_uuid, sequence, receipt_variant, record_id, bound_id, observed, provenance,
         receipt_hash, receipt['raw_sha256'], canonical(metadata), global_error, canonical(receipt)))
    inserted = 0
    for kind in KINDS:
        present = isinstance(raw, dict) and kind in raw
        if not present and global_error is None:
            continue
        value = raw.get(kind) if isinstance(raw, dict) else None
        availability = ('absent' if not present else 'null' if value is None
                        else 'malformed' if not isinstance(value, list)
                        else 'present' if value else 'empty')
        quality = _quality(raw, kind, observed, global_error)
        state = ('invalid' if global_error is not None or availability == 'malformed'
                 or availability == 'present' and not quality['structural_eligible'] else availability)
        observation = dict(receipt_sha256=receipt_hash, fixture_id=bound_id, kind=kind,
                           state=state, availability=availability, payload=value,
                           metadata=metadata, quality=quality)
        connection.execute('''INSERT INTO context_observations
            (receipt_id,source_uuid,sequence,receipt_variant,fixture_id,kind,observed_at,
             provenance,state,availability,payload_json,metadata_json,quality_json,
             quality_sha256,observation_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (cursor.lastrowid, source_uuid, sequence, receipt_variant, bound_id, kind,
             observed, provenance, state, availability, canonical(value), canonical(metadata),
             canonical(quality), digest(quality), digest(observation)))
        inserted += 1
    return dict(inserted=True, observations_inserted=inserted, fixture_id=bound_id,
                validation_error=global_error)


def update_checkpoint(connection, source_uuid, sequence, last_digest):
    """Advance an opaque sync high-water mark within the caller's transaction."""
    source_uuid = _nonempty(source_uuid, 'source_uuid')
    last_digest = _nonempty(last_digest, 'last_digest')
    if type(sequence) is not int or sequence < 0:
        raise ValueError('checkpoint sequence must be nonnegative.')
    previous = get_checkpoint(connection, source_uuid)
    if previous and (sequence < previous['sequence'] or sequence == previous['sequence'] and last_digest != previous['last_digest']):
        raise ValueError('Checkpoint cannot move backward or change at the same sequence.')
    connection.execute('''INSERT INTO context_checkpoints VALUES(?,?,?)
        ON CONFLICT(source_uuid) DO UPDATE SET sequence=excluded.sequence,last_digest=excluded.last_digest''',
        (source_uuid, sequence, last_digest))


def get_checkpoint(connection, source_uuid):
    row = connection.execute('SELECT source_uuid,sequence,last_digest FROM context_checkpoints WHERE source_uuid=?',
                             (source_uuid,)).fetchone()
    return dict(row) if row is not None else None


def latest_observation(connection, *, fixture_id, kind, before, expected_identity=None,
                       include_recovered=False):
    """Return the last component version strictly before an aware cutoff.

    No older value is resurrected after null, empty or invalidation. Unknown
    observation time fails closed for the fixture, because it cannot safely be
    ordered relative to the cutoff. Recovered baselines are excluded by default.
    ``expected_identity`` optionally gates fixture_id/league_id/home_id/away_id/
    kickoff_utc; this is an explicit caller scope, never today's inferred team.
    This query returns stored evidence, not a model-ready training example.
    ``usable`` means only the reported qualification_scope. In particular,
    goal-score compatibility is not event-process eligibility, and XI structure
    is not a verified prematch decision-time observation. Inspect the immutable
    validator report and separately apply decision, identity and coverage gates.
    """
    fixture_id = _identifier(fixture_id)
    cutoff = _instant(before)
    if fixture_id is None or kind not in KINDS or cutoff is None:
        raise ValueError('A fixture ID, known component kind and aware cutoff are required.')
    result = dict(fixture_id=fixture_id, kind=kind, before=cutoff, state='unknown', known=False,
                  usable=False, observation=None, reason='no_observation_before_cutoff',
                  provider_publication_time_proven=False, model_training_ready=False,
                  qualification_scope=None)
    provenance_filter = '' if include_recovered else " AND provenance='local_import_recorded'"
    unknown = connection.execute('''SELECT 1 FROM context_observations
        WHERE fixture_id=? AND observed_at IS NULL''' + provenance_filter + ' LIMIT 1', (fixture_id,)).fetchone()
    if unknown:
        result.update(state='invalid', reason='unknown_observation_time_for_fixture')
        return result
    row = connection.execute('''SELECT * FROM context_observations
        WHERE fixture_id=? AND kind=? AND observed_at<?''' + provenance_filter + '''
        ORDER BY observed_at DESC,sequence DESC,
            CASE receipt_variant WHEN 'received' THEN 1 ELSE 0 END DESC,id DESC LIMIT 1''',
        (fixture_id, kind, cutoff)).fetchone()
    if row is None:
        return result
    observation = dict(row)
    receipt = connection.execute('SELECT * FROM context_receipts WHERE id=?', (row['receipt_id'],)).fetchone()
    try:
        indexes_match = receipt is not None and all(row[key] == receipt[key] for key in (
            'source_uuid', 'sequence', 'receipt_variant', 'fixture_id', 'observed_at', 'provenance', 'metadata_json'))
        for field in ('payload_json', 'metadata_json', 'quality_json'):
            observation[field[:-5]] = json.loads(observation.pop(field))
        metadata, quality = observation['metadata'], observation['quality']
        integrity_payload = dict(receipt_sha256=receipt['receipt_sha256'] if receipt is not None else None,
            fixture_id=observation['fixture_id'], kind=observation['kind'], state=observation['state'],
            availability=observation['availability'], payload=observation['payload'], metadata=metadata, quality=quality)
        integrity_ok = (indexes_match and _receipt_valid(receipt)
            and digest(quality) == observation['quality_sha256']
            and digest(integrity_payload) == observation['observation_sha256'])
    except (ValueError, TypeError, KeyError, OverflowError):
        integrity_ok = False
    result.update(state=observation['state'], known=True, observation=observation,
                  reason='latest_component_observation')
    if not integrity_ok:
        result.update(state='invalid', reason='immutable_observation_integrity_mismatch')
        return result
    result['qualification_scope'] = quality['qualification_scope']
    if expected_identity is not None:
        if not isinstance(expected_identity, dict) or any(key not in metadata or metadata[key] != value
                for key, value in expected_identity.items()):
            result.update(state='invalid', reason='caller_identity_scope_mismatch')
            return result
    result['usable'] = bool(observation['state'] == 'present' and quality['structural_eligible']
                            and observation['provenance'] == 'local_import_recorded')
    return result


def latest(path, **kwargs):
    """Read-only convenience query. A missing archive is never created."""
    connection = connect(path, readonly=True)
    try:
        return latest_observation(connection, **kwargs)
    finally:
        connection.close()
