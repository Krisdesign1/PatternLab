"""Read-only reception of registered capture windows; never reconstruct proofs.

Only compact confirmation metadata is inspected. Provider histories, CAS feature
documents, outcomes, models and worker mutations are outside this diagnostic.
"""
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, time, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import causal_confirmation as confirmation
import causal_confirmation_store as store
from fixture_temporal import utc_instant
import prematch_feature_snapshot as snapshots


VERSION = 'scheduler-deadlines-v1'
LIMITATIONS = [
    'Les fenêtres décrivent les règles déjà inscrites, sans modifier ni rattraper une décision passée.',
    'Un recensement absent laisse le nombre de cibles inconnu ; zéro capture ne signifie pas zéro match.',
    'Les accusés locaux attestent un enregistrement après COMMIT, pas une publication du fournisseur à T.',
    'La fin de lecture du recensement précède son COMMIT et n’atteste pas son heure de publication ; '
    'l’accusé de fermeture atteste seulement la fermeture, y compris pour un recensement vide.',
    'La lecture contrôle les métadonnées compactes ; elle ne reconstruit pas les variables ou leurs documents de sources.',
    'Ce diagnostic ne démontre ni disponibilité continue, ni qualité prédictive, ni cause d’une interruption.',
    'La compatibilité compare les fichiers sur disque et les versions installées ; elle ne certifie pas le code déjà chargé dans un processus.',
]
SAFE_KINDS = {
    'registration_ack', 'calendar_prepared', 'calendar_preparation_unavailable',
    'window_closed', 'window_closed_ack', 'window_missed', 'registration_after_decision',
    'preparation_unavailable', 'feature_unavailable', 'provider_unavailable', 'capture_unavailable',
}


class DeadlineError(ValueError):
    """Sanitized operational failure; never include stored freeform reasons."""


def _hash(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                            separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _implementation():
    root = Path(__file__).resolve().parent
    return {name: sha256((root/name).read_bytes()).hexdigest() for name in
            ('scheduler_deadlines.py', 'causal_confirmation.py', 'causal_confirmation_store.py',
             'prematch_feature_snapshot.py')}


def _fixture_time(value):
    value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _read(path, now):
    """One immutable read transaction, including each receipt's matching row."""
    evidence = []
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=2)) as con:
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA query_only=ON')
        con.execute('BEGIN')
        required = {'protocols', 'anchors', 'attempts', 'forecasts', 'receipts'}
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required <= tables:
            raise DeadlineError('Le schéma du suivi des échéances est incomplet.')
        protocols = {}
        for row in con.execute('SELECT * FROM protocols ORDER BY id'):
            value = confirmation._validate_protocol(store._decode(row, expected_id=row['id']))
            evidence.append(['protocol', row['id'], row['digest']])
            if utc_instant(value['registered_at']) <= now:
                protocols[value['id']] = value
        anchors, attempts, forecasts = {}, defaultdict(list), defaultdict(list)
        for row in con.execute('SELECT * FROM anchors ORDER BY protocol_id,day'):
            value = store._decode(row)
            if value.get('protocol_id') != row['protocol_id'] or value.get('day') != row['day']:
                raise ValueError()
            evidence.append(['anchor', row['protocol_id'], row['day'], row['digest']])
            if row['protocol_id'] in protocols and utc_instant(value['read_finished_at']) <= now:
                anchors[(row['protocol_id'], row['day'])] = value
        for row in con.execute('SELECT * FROM attempts ORDER BY protocol_id,id'):
            value = store._decode(row, expected_id=row['id'])
            if value.get('protocol_id') != row['protocol_id']:
                raise ValueError()
            evidence.append(['attempt', row['id'], row['digest']])
            if row['protocol_id'] in protocols and utc_instant(value['observed_at_utc']) <= now:
                attempts[row['protocol_id']].append(value)
        for row in con.execute('SELECT * FROM forecasts ORDER BY protocol_id,match_id'):
            value = store._decode(row, sealed=False)
            for identity in (row['id'],row['context_id'],value['snapshot']['id']):
                store._hash(identity)
            fixture = value['fixture']
            if (value['id'] != row['id'] or value['protocol_id'] != row['protocol_id']
                    or fixture['match_id'] != row['match_id'] or fixture != value['snapshot']['fixture']
                    or any(name in value['snapshot'] for name in ('history_rows', 'source_refs'))):
                raise ValueError()
            transaction = utc_instant(row['transaction_at_utc'])
            if transaction < utc_instant(value['snapshot']['decision_at']):
                raise ValueError()
            evidence.append(['forecast', row['id'], row['digest'], row['transaction_at_utc']])
            ackrow = con.execute('SELECT * FROM receipts WHERE forecast_id=?', (row['id'],)).fetchone()
            ack = store._decode(ackrow) if ackrow else None
            if ack:
                store._validate_ack(ack, value, row['transaction_at_utc'])
                if ackrow['forecast_id'] != value['id']:
                    raise ValueError()
                evidence.append(['receipt', row['id'], ackrow['digest']])
                if utc_instant(ack['observed_at_utc']) > now:
                    ack = None
            if row['protocol_id'] in protocols and transaction <= now:
                forecasts[row['protocol_id']].append(dict(payload=value, ack=ack, transaction_at=transaction,
                                                         context_id=row['context_id']))
        # Orphaned compact records are not silently omitted from a success report.
        for table in ('anchors', 'attempts', 'forecasts'):
            if con.execute(f'SELECT 1 FROM {table} t LEFT JOIN protocols p ON p.id=t.protocol_id '
                           'WHERE p.id IS NULL LIMIT 1').fetchone():
                raise ValueError()
        if con.execute('SELECT 1 FROM receipts r LEFT JOIN forecasts f ON f.id=r.forecast_id '
                       'WHERE f.id IS NULL LIMIT 1').fetchone():
            raise ValueError()
    return protocols, anchors, attempts, forecasts, evidence


def _day(protocol, day, now, anchors, attempts, forecasts):
    pid = protocol['id']
    decision = datetime.combine(day, time(protocol['decision_hour_utc']), timezone.utc)
    deadline = decision+timedelta(minutes=protocol['capture_grace_minutes'])
    warmup = decision-timedelta(minutes=protocol['calendar_warmup_minutes'])
    stamp = day.isoformat()
    anchor = anchors.get((pid, stamp))
    events = [v for v in attempts if v.get('day') == stamp]
    rows = [v for v in forecasts if utc_instant(v['payload']['planned_decision_at']).date() == day]
    targets = None
    census_timely = True
    issues = []
    if anchor:
        if (utc_instant(anchor['decision_at']) != decision
                or not decision <= utc_instant(anchor['read_started_at']) <= utc_instant(anchor['read_finished_at'])):
            raise ValueError()
        census_timely = utc_instant(anchor['read_finished_at']) < deadline
        if not census_timely:
            issues.append('census_timing_unverified')
        targets = {}
        for target in anchor['targets']:
            mid = target['match_id']
            if mid in targets or target['fixture']['match_id'] != mid:
                raise ValueError()
            fixture = target['fixture']
            canonical = snapshots._fixture(fixture,decision)[0]
            if canonical['match_id'] != mid:
                raise ValueError()
            if not (_fixture_time(fixture['timestamp']).date() == day and _fixture_time(fixture['timestamp']) > decision):
                raise ValueError()
            targets[mid] = target
    ids, actual_times, admissible = set(), [], 0
    for row in rows:
        payload, ack = row['payload'], row['ack']
        mid = payload['fixture']['match_id']
        if mid in ids or targets is None or mid not in targets:
            raise ValueError()
        ids.add(mid)
        target = targets[mid]
        canonical = snapshots._fixture(target['fixture'], decision)[0]
        if (payload['fixture'] != canonical
                or payload.get('version') != confirmation.VERSION
                or payload.get('candidate_id') != protocol['candidate_id']
                or target['target_ref'] != payload['snapshot']['target_ref']
                or target['target_raw'] != payload['snapshot']['target_observation']
                or row['context_id'] != anchor['contexts'][payload['fixture']['league']]['context_id']
                or payload['source_read']['start'] != anchor['read_started_at']
                or payload['source_read']['end'] != anchor['read_finished_at']
                or utc_instant(payload['planned_decision_at']) != decision
                or utc_instant(payload['snapshot']['decision_at']) != decision
                or not decision <= utc_instant(payload['prepared_at']) <= row['transaction_at']
                or row['transaction_at'] < utc_instant(protocol['registered_at'])):
            raise ValueError()
        if not (decision <= utc_instant(payload['source_read']['start'])
                <= utc_instant(payload['source_read']['end'])
                <= utc_instant(payload['confirmation']['requested_at'])
                <= utc_instant(payload['confirmation']['received_at'])
                <= utc_instant(payload['prepared_at']) < deadline):
            raise ValueError()
        if ack and ack['time_quality'] == 'local_commit_ack':
            acknowledged = utc_instant(ack['observed_at_utc'])
            actual_times.append(acknowledged)
            if ack['admissible'] and acknowledged < deadline:
                admissible += 1
    registered = any(v['kind'] == 'registration_ack'
        and v.get('time_quality') == 'local_registration_ack' and not protocol['synthetic_clock']
        and utc_instant(protocol['registered_at']) <= utc_instant(v['observed_at_utc']) < decision
        for v in attempts)
    if not registered:
        issues.append('registration_timing_unverified')
        admissible = 0
    if len(ids) != admissible:
        issues.append('capture_timing_unverified')
    closures = [v for v in events if v['kind'] in ('window_closed', 'window_missed')]
    if len(closures) > 1:
        raise ValueError()
    closure = closures[0] if closures else None
    if closure is None and any(v['kind'] == 'window_closed_ack' for v in events):
        raise ValueError()
    closed_at, closed_ack = None, None
    if closure:
        closed_at = utc_instant(closure['observed_at_utc'])
        if closed_at < deadline or (closure['kind'] == 'window_missed') != (anchor is None):
            raise ValueError()
        if closure['kind'] == 'window_closed':
            missing_ids = closure.get('missing_match_ids')
            if (not isinstance(missing_ids, list) or any(not isinstance(v, str) for v in missing_ids)
                    or len(set(missing_ids)) != len(missing_ids)
                    or set(missing_ids) != set(targets)-ids):
                raise ValueError()
        acks = [v for v in events if v['kind'] == 'window_closed_ack']
        if len(acks) > 1:
            raise ValueError()
        if acks:
            ack = acks[0]
            if ack.get('closure_id') != closure['id'] or utc_instant(ack['observed_at_utc']) < closed_at:
                raise ValueError()
            if ack.get('time_quality') == 'local_closure_commit_ack':
                closed_ack = utc_instant(ack['observed_at_utc'])
    if now < warmup:
        state = 'upcoming'
    elif now < decision:
        state = 'warmup'
    elif now < deadline:
        state = 'open'
    elif anchor is None:
        state = 'missed'
        issues.append('unknown_census')
    elif not closed_ack or not registered or not census_timely or admissible != len(ids):
        state = 'closed_unverified'
    else:
        state = 'closed'
    if now >= deadline and closed_ack is None:
        issues.append('closure_timing_unverified')
    missing = len(targets)-len(ids) if targets is not None else None
    if missing:
        issues.append('known_targets_not_captured')
    calendar_times = [utc_instant(v['observed_at_utc']) for v in events if v['kind'] == 'calendar_prepared']
    warmup_times = [v for v in calendar_times if warmup <= v < decision]
    if now >= decision and not warmup_times:
        issues.append('warmup_not_observed_in_window')
    iso = lambda v: v.isoformat() if v is not None else None
    return dict(protocol_id=pid, day=stamp, warmup_at=iso(warmup), decision_at=iso(decision),
        deadline_at=iso(deadline), state=state, target_count=len(targets) if targets is not None else None,
        capture_count=len(ids), timing_admissible_count=admissible, missing_known_targets=missing,
        first_capture_ack_at=iso(min(actual_times)) if actual_times else None,
        last_capture_ack_at=iso(max(actual_times)) if actual_times else None,
        closure_at=iso(closed_at), closure_ack_at=iso(closed_ack),
        closure_delay_seconds=(closed_at-deadline).total_seconds() if closed_at else None,
        registration_ready=registered, warmup_observed_at=iso(min(warmup_times)) if warmup_times else None,
        event_counts=dict(sorted(Counter(v['kind'] if v['kind'] in SAFE_KINDS else 'other' for v in events).items())),
        issues=issues)


def load(root=None, now=None):
    """Read saved window evidence only, within one SQLite snapshot.

    ``now`` supports deterministic diagnostics. It filters recorded timestamps,
    not a historical SQLite visibility reconstruction, and never writes proofs.
    """
    try:
        current = utc_instant(now or datetime.now(timezone.utc))
        path = store.database_path(root).resolve()
        implementation = _implementation()
        # Capture the inputs to the same compatibility rule as the core once,
        # rather than mixing disk/version observations between protocols.
        current_implementation = confirmation.implementation()
        current_runtime = confirmation._runtime()
        compatibility = dict(implementation=current_implementation, runtime=current_runtime)
        base = dict(version=VERSION, as_of=current.isoformat(), implementation=implementation,
                    current_implementation=current_implementation, current_runtime=current_runtime,
                    limitations=LIMITATIONS.copy())
        if not path.exists():
            return dict(base, source_sha256=_hash(dict(implementation=implementation,
                        compatibility=compatibility, evidence=[])),
                        source_states={'confirmation': 'missing'}, protocols=[], days=[], counts={})
        if store.database_path(root).is_symlink():
            raise ValueError()
        protocols, anchors, events, forecasts, evidence = _read(path, current)
        days, summaries = [], []
        for pid, protocol in protocols.items():
            expected = confirmation._dates(protocol)
            allowed = {d.isoformat() for d in expected}
            if any(k[0] == pid and k[1] not in allowed for k in anchors):
                raise ValueError()
            if any(v.get('day') is not None and v['day'] not in allowed for v in events[pid]):
                raise ValueError()
            if any(utc_instant(v['payload']['planned_decision_at']).date().isoformat() not in allowed for v in forecasts[pid]):
                raise ValueError()
            values = [_day(protocol, d, current, anchors, events[pid], forecasts[pid]) for d in expected]
            days.extend(values)
            summaries.append(dict(id=pid, candidate_id=protocol['candidate_id'], registered_at=protocol['registered_at'],
                start=protocol['confirmation_start'], end=protocol['confirmation_end'],
                implementation_compatible=(protocol['implementation'] == current_implementation
                    and ('followup' not in protocol or protocol.get('runtime') == current_runtime)),
                synthetic_clock=protocol['synthetic_clock'], registered_days=len(values),
                counts=dict(sorted(Counter(v['state'] for v in values).items()))))
        return dict(base, source_sha256=_hash(dict(implementation=implementation,
                    compatibility=compatibility,evidence=evidence)),
                    source_states={'confirmation': 'available'}, protocols=summaries, days=days,
                    counts=dict(sorted(Counter(v['state'] for v in days).items())))
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        raise DeadlineError('Le suivi des échéances est indisponible ou incohérent ; aucune donnée n’a été modifiée.') from None
