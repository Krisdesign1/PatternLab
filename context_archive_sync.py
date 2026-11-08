"""Incrementally preserve existing local fixture-context receipts in a sidecar.

The source is always opened read-only. Local receipt times and recovered
baselines remain explicitly qualified; hashes prove internal consistency only.
Each bounded batch commits observations, per-fixture chains and its checkpoint
together. No API, model, provider client or production module is imported.
"""
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import argparse
import fcntl
import json
import os
import sqlite3

import context_observation_store as archive


VERSION = 'context-archive-sync-v1'
RECORD_VERSION = 'fixture-import-quality-v1'


class SyncError(ValueError):
    """The source identity, checkpoint or append-only contract changed."""


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _digest(value):
    return sha256(_canonical(value).encode()).hexdigest()


def _identifier(value):
    if (type(value) not in (str, int) or not str(value).isascii()
            or not str(value).isdecimal() or int(value) <= 0):
        return None
    return str(int(value))


def _instant(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
        if not isinstance(result, datetime) or result.tzinfo is None or result.utcoffset() is None:
            return None
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


@contextmanager
def _source(path):
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=15)
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        yield connection
    finally:
        connection.rollback()
        connection.close()


@contextmanager
def _lock(destination):
    lock_path = Path(str(destination) + '.sync.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncError('Another context archive sync holds the destination lock.') from None
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _source_header(connection):
    metadata = dict(connection.execute('SELECT key,value FROM metadata'))
    source_uuid = metadata.get('store_uuid', '')
    if (len(source_uuid) != 32 or any(c not in '0123456789abcdef' for c in source_uuid)):
        raise SyncError('A valid explicit source store_uuid is required.')
    try:
        revision = int(metadata['revision'])
        if revision < 0:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise SyncError('Invalid source revision.') from None
    highwater = connection.execute('SELECT COALESCE(MAX(sequence),0) FROM fixture_import_revisions').fetchone()[0]
    if type(highwater) is not int or highwater < 0:
        raise SyncError('Invalid source highwater.')
    return dict(source_uuid=source_uuid, revision=revision, highwater=highwater)


def _schema(connection):
    # These tables belong solely to the new archive, never to the source.
    connection.execute('''CREATE TABLE IF NOT EXISTS sync_source_binding(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), source_uuid TEXT NOT NULL,
        last_source_revision INTEGER NOT NULL, last_seen_highwater INTEGER NOT NULL,
        processed_records INTEGER NOT NULL)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS sync_fixture_chains(
        source_uuid TEXT NOT NULL, fixture_id TEXT NOT NULL, last_record_id TEXT,
        last_observed_at TEXT, last_sequence INTEGER NOT NULL, chain_valid INTEGER NOT NULL,
        PRIMARY KEY(source_uuid,fixture_id))''')
    connection.execute('''CREATE TABLE IF NOT EXISTS sync_receipt_audit(
        source_uuid TEXT NOT NULL, sequence INTEGER NOT NULL, reason TEXT,
        supplied_observed_at TEXT, supplied_indexed_at TEXT,
        PRIMARY KEY(source_uuid,sequence))''')


def _binding(connection):
    row = connection.execute('''SELECT source_uuid,last_source_revision,last_seen_highwater,
        processed_records FROM sync_source_binding WHERE singleton=1''').fetchone()
    if row is None:
        return None
    return dict(zip(('source_uuid', 'revision', 'highwater', 'processed'), row))


def _check_source(connection, header, binding, checkpoint, *, check_prefix=True):
    if binding is not None:
        if header['source_uuid'] != binding['source_uuid']:
            raise SyncError('Source database identity changed; use a separate destination.')
        if header['revision'] < binding['revision'] or header['highwater'] < binding['highwater']:
            raise SyncError('Source revision or journal highwater rolled back.')
    sequence = checkpoint['sequence'] if checkpoint else 0
    if header['highwater'] < sequence:
        raise SyncError('Source journal rolled back behind the checkpoint.')
    if checkpoint:
        anchor = connection.execute('''SELECT sequence,match_id,observed_at_utc,payload,digest
            FROM fixture_import_revisions WHERE sequence=?''', (sequence,)).fetchone()
        if anchor is None or _digest(list(anchor)) != checkpoint['last_digest']:
            raise SyncError('Source checkpoint row or indexes changed.')
    if check_prefix:
        count = connection.execute('SELECT COUNT(*) FROM fixture_import_revisions WHERE sequence<=?',
                                   (sequence,)).fetchone()[0]
        if binding and count != binding['processed']:
            raise SyncError('Source journal prefix was deleted or replaced.')


def _record(row, previous):
    """Return the original received payload and an explicit invalidation reason."""
    sequence, indexed_mid, indexed_time, payload, indexed_digest = row
    reasons, record = [], None
    try:
        def reject_constant(_):
            raise ValueError('Non-finite JSON')
        record = json.loads(payload, parse_constant=reject_constant)
        if not isinstance(record, dict):
            raise ValueError('Non-object record')
    except (ValueError, TypeError, UnicodeError):
        record = {}
        reasons.append('record_not_canonical_object')
    identity = _identifier(indexed_mid)
    if identity is None:
        # There is no safe fixture to invalidate when the source index is lost.
        raise SyncError('Source match index is invalid; no fixture identity can be preserved.')
    if record.get('version') != RECORD_VERSION:
        reasons.append('record_version_invalid')
    record_id = record.get('id')
    if not isinstance(record_id, str) or not record_id:
        reasons.append('record_id_invalid')
    if indexed_digest != record_id:
        reasons.append('indexed_digest_mismatch')
    try:
        if _digest({k: v for k, v in record.items() if k != 'id'}) != record_id:
            reasons.append('record_checksum_invalid')
    except (ValueError, TypeError, OverflowError):
        reasons.append('record_checksum_invalid')
    if _identifier(record.get('match_id')) != identity:
        reasons.append('indexed_fixture_mismatch')
    observed = _instant(record.get('observed_at_utc'))
    indexed = _instant(indexed_time)
    if observed is None or indexed is None:
        reasons.append('observation_time_unknown_or_naive')
        observed = None
    elif observed != indexed:
        reasons.append('indexed_time_mismatch')
        observed = None
    if record.get('time_kind') != 'local_import_recorded':
        reasons.append('observation_time_kind_invalid')
    if 'previous_observation' not in record:
        reasons.append('previous_observation_missing')
    if previous is None:
        if record.get('previous_observation') is not None:
            reasons.append('chain_missing_predecessor')
    else:
        prior_id, prior_time, prior_sequence, valid = previous
        if not valid:
            reasons.append('chain_predecessor_invalid')
        if record.get('previous_observation') != prior_id:
            reasons.append('chain_predecessor_mismatch')
        if sequence <= prior_sequence:
            reasons.append('chain_sequence_not_increasing')
        if observed is not None and _instant(prior_time) is not None and observed < _instant(prior_time):
            reasons.append('chain_time_regression')
            # It cannot be safely placed before an older, already received
            # version. Keep the original text in the audit, never invent a time.
            observed = None
    raw = record.get('received_raw')
    if not isinstance(raw, dict):
        reasons.append('received_payload_not_object')
        raw = {}
    elif _digest(raw) != record.get('received_raw_sha256'):
        reasons.append('received_checksum_invalid')
    raw_fixture = raw.get('fixture')
    if not isinstance(raw_fixture, dict) or _identifier(raw_fixture.get('id')) != identity:
        reasons.append('received_fixture_identity_mismatch')
    baseline = None
    if 'baseline_raw' in record or record.get('baseline_equals_received') is True:
        baseline = record.get('baseline_raw', raw)
        if (previous is not None or record.get('baseline_time_kind') != 'recovered_at_first_tracked_import'
                or not isinstance(baseline, dict) or _digest(baseline) != record.get('baseline_raw_sha256')):
            reasons.append('recovered_baseline_invalid')
        elif (not isinstance(baseline.get('fixture'), dict)
                or _identifier(baseline['fixture'].get('id')) != identity):
            reasons.append('recovered_baseline_identity_mismatch')
    chain_valid = not reasons
    # Valid integrity/chain does not make a rejected or quarantined context usable.
    if record.get('decision') not in ('received', 'quarantined', 'retained_final',
            'status_correction', 'reviewed', 'review_retained'):
        reasons.append('invalid_import_decision')
    if record.get('decision') == 'quarantined':
        reasons.append('quarantined_observation')
    annotation = record.get('selected_quality')
    if annotation is not None:
        if (not isinstance(annotation, dict) or annotation.get('version') != RECORD_VERSION
                or annotation.get('quarantined') is not True
                or annotation.get('previous_raw_sha256') != record.get('selected_raw_sha256')
                or annotation.get('received_raw_sha256') != record.get('received_raw_sha256')):
            reasons.append('malformed_selected_quality')
        reasons.append('quarantined_observation')
    if record.get('selected_raw_sha256') != record.get('received_raw_sha256'):
        reasons.append('received_version_not_retained')
    return dict(fixture_id=identity, record_id=record_id if isinstance(record_id, str) and record_id else str(indexed_digest),
                raw=raw, observed_at=observed.isoformat() if observed else None,
                baseline=baseline, chain_valid=chain_valid,
                supplied_observed_at=record.get('observed_at_utc'), supplied_indexed_at=indexed_time,
                reason=';'.join(sorted(set(reasons))) if reasons else None)


def _summary(connection, header, target, start, processed, batches, errors, *, totals=None):
    checkpoint = archive.get_checkpoint(connection, header['source_uuid'])
    binding = _binding(connection)
    if totals is None:
        total_errors = Counter()
        for reason, count in connection.execute(
                'SELECT reason,COUNT(*) FROM sync_receipt_audit WHERE reason IS NOT NULL GROUP BY reason'):
            for code in reason.split(';'):
                total_errors[code] += count
        totals = dict(total_error_counts=dict(total_errors),
            total_receipts=connection.execute('SELECT COUNT(*) FROM context_receipts').fetchone()[0],
            total_observations=connection.execute('SELECT COUNT(*) FROM context_observations').fetchone()[0])
    return dict(version=VERSION, source_uuid=header['source_uuid'],
        initial_source_highwater=target, checkpoint_start=start,
        checkpoint=checkpoint, processed_records=processed, batches=batches,
        total_processed_records=binding['processed'] if binding else 0,
        total_receipts=totals['total_receipts'], total_observations=totals['total_observations'],
        error_counts=dict(errors),
        total_error_counts=totals['total_error_counts'],
        complete=(checkpoint['sequence'] if checkpoint else 0) >= target,
        source_database_writes=0, api_calls=0, model_changes=0)


def sync_archive(source_database, destination_database, *, batch_size=500, max_batches=None, progress=None):
    """Catch up to the initial source highwater, optionally yielding between runs.

    ``batch_size`` bounds memory (at most 1000 raw revisions), never the amount
    collected. ``max_batches`` is an explicit scheduling budget; the checkpoint
    resumes the remaining records. ``progress(summary)`` runs after commit only.
    Unknown-time invalidations remain undated; no current time is substituted.
    """
    if type(batch_size) is not int or not 1 <= batch_size <= 1000:
        raise ValueError('batch_size must be an integer from 1 to 1000.')
    if max_batches is not None and (type(max_batches) is not int or max_batches < 0):
        raise ValueError('max_batches must be a nonnegative integer or None.')
    source_path = Path(source_database).resolve(strict=True)
    destination_arg = Path(destination_database).absolute()
    destination = destination_arg.resolve()
    if (destination_arg.is_symlink() or source_path == destination
            or destination.exists() and os.path.samefile(source_path, destination)):
        raise SyncError('Source and destination must be different databases.')
    with _lock(destination):
        with _source(source_path) as source:
            header = _source_header(source)
        target = header['highwater']
        archive.initialize(destination)
        connection = archive.connect(destination)
        try:
            with connection:
                _schema(connection)
            checkpoint = archive.get_checkpoint(connection, header['source_uuid'])
            start = checkpoint['sequence'] if checkpoint else 0
            processed, batches, errors = 0, 0, Counter()
            initial = _summary(connection, header, target, start, 0, 0, errors)
            totals = {name: initial[name] for name in ('total_receipts', 'total_observations', 'total_error_counts')}
            while True:
                binding = _binding(connection)
                checkpoint = archive.get_checkpoint(connection, header['source_uuid'])
                after = checkpoint['sequence'] if checkpoint else 0
                with _source(source_path) as source:
                    current = _source_header(source)
                    stopping = after >= target or (max_batches is not None and batches >= max_batches)
                    # Check the entire processed prefix at each invocation's
                    # boundaries; per-batch anchors still detect checkpoint
                    # changes. This avoids re-scanning a growing journal N times.
                    _check_source(source, current, binding, checkpoint,
                                  check_prefix=batches == 0 or stopping)
                    if current['source_uuid'] != header['source_uuid']:
                        raise SyncError('Source database identity changed during sync.')
                    if stopping:
                        break
                    rows = source.execute('''SELECT sequence,match_id,observed_at_utc,payload,digest
                        FROM fixture_import_revisions WHERE sequence>? AND sequence<=?
                        ORDER BY sequence LIMIT ?''', (after, target, batch_size)).fetchall()
                if not rows:
                    raise SyncError('Source journal contains a missing checkpoint range.')
                batch_errors = Counter()
                added_receipts = added_observations = 0
                with connection:
                    for row in rows:
                        sequence, mid = row[:2]
                        previous = connection.execute('''SELECT last_record_id,last_observed_at,
                            last_sequence,chain_valid FROM sync_fixture_chains
                            WHERE source_uuid=? AND fixture_id=?''',
                            (header['source_uuid'], _identifier(mid) or str(mid))).fetchone()
                        value = _record(row, previous)
                        kwargs = dict(source_uuid=header['source_uuid'], sequence=sequence,
                            record_id=value['record_id'], observed_at=value['observed_at'],
                            fixture_id=value['fixture_id'], validation_error=value['reason'])
                        stored_errors = set(value['reason'].split(';')) if value['reason'] else set()
                        if value['baseline'] is not None:
                            baseline_receipt = archive.append_observation(connection, raw=value['baseline'],
                                provenance='recovered_at_first_tracked_import',
                                receipt_variant='recovered_baseline', **kwargs)
                            if baseline_receipt['validation_error']:
                                stored_errors.update(baseline_receipt['validation_error'].split(';'))
                            added_receipts += int(baseline_receipt['inserted'])
                            added_observations += baseline_receipt['observations_inserted']
                        receipt = archive.append_observation(connection, raw=value['raw'], **kwargs)
                        added_receipts += int(receipt['inserted'])
                        added_observations += receipt['observations_inserted']
                        if receipt['validation_error']:
                            stored_errors.update(receipt['validation_error'].split(';'))
                        connection.execute('''INSERT INTO sync_fixture_chains VALUES(?,?,?,?,?,?)
                            ON CONFLICT(source_uuid,fixture_id) DO UPDATE SET
                            last_record_id=excluded.last_record_id,last_observed_at=excluded.last_observed_at,
                            last_sequence=excluded.last_sequence,chain_valid=excluded.chain_valid''',
                            (header['source_uuid'], value['fixture_id'], value['record_id'],
                             value['observed_at'], sequence, int(value['chain_valid'])))
                        connection.execute('INSERT INTO sync_receipt_audit VALUES(?,?,?,?,?)',
                                           (header['source_uuid'], sequence,
                                            ';'.join(sorted(stored_errors)) if stored_errors else None,
                                            _canonical(value['supplied_observed_at']) if value['observed_at'] is None else None,
                                            _canonical(value['supplied_indexed_at']) if value['observed_at'] is None else None))
                        batch_errors.update(stored_errors)
                    count = (binding['processed'] if binding else 0) + len(rows)
                    connection.execute('''INSERT INTO sync_source_binding VALUES(1,?,?,?,?)
                        ON CONFLICT(singleton) DO UPDATE SET last_source_revision=excluded.last_source_revision,
                        last_seen_highwater=excluded.last_seen_highwater,processed_records=excluded.processed_records''',
                        (header['source_uuid'], current['revision'], current['highwater'], count))
                    archive.update_checkpoint(connection, header['source_uuid'], rows[-1][0], _digest(list(rows[-1])))
                processed += len(rows)
                batches += 1
                errors.update(batch_errors)
                totals['total_receipts'] += added_receipts
                totals['total_observations'] += added_observations
                totals['total_error_counts'] = dict(Counter(totals['total_error_counts']) + batch_errors)
                if progress is not None:
                    # Exact committed counters, without rescanning hundreds of
                    # thousands of immutable receipts for every progress tick.
                    progress(_summary(connection, header, target, start, processed, batches, errors, totals=totals))
            return _summary(connection, header, target, start, processed, batches, errors)
        finally:
            connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source_database', type=Path)
    parser.add_argument('destination_database', type=Path)
    parser.add_argument('--batch-size', type=int, default=500)
    parser.add_argument('--max-batches', type=int)
    args = parser.parse_args()
    print(json.dumps(sync_archive(args.source_database, args.destination_database,
        batch_size=args.batch_size, max_batches=args.max_batches), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
