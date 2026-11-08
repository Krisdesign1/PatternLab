"""Read-only observations of local scheduler passages and completed work.

One SQLite snapshot supplies this report. No API, fixture, model, log or secret
is read. Gaps describe recorded operations, not their cause or server uptime.
"""
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import sqlite3
from zoneinfo import ZoneInfo

import automation_health as health


VERSION = 'scheduler-operations-v1'
EVENT_LIMIT = 100000
CYCLE_LIMIT = 10000
PHASES = ('calendar', 'results', 'forecasts', 'reconcile')
ZONE = ZoneInfo('Africa/Kinshasa')
DIVERGENCE_SECONDS = 120
LIMITATIONS = [
    'Un signal récent décrit la présence observée du processus, pas la fraîcheur des données.',
    'Un écart entre temps civil et durée mesurée peut provenir d’une suspension ou d’une horloge ; sa cause reste inconnue.',
    'Une reprise désigne ici une nouvelle opération après un intervalle sans opération conservée dans le même cycle.',
    'Le seuil de deux intervalles est un repère de surveillance, pas un engagement de délai ni une preuve de panne.',
    'Les opérations ne prouvent pas un fonctionnement continu ; aucune prévision manquée n’est reconstruite.',
    'Les intervalles observés restent limités aux événements présents dans la fenêtre lue.',
]


class OperationError(ValueError):
    pass


def _stamp(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            raise OperationError('Date invalide.') from None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise OperationError('Date invalide.')
    return value.astimezone(timezone.utc)


def _identity(value):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{32}', value) is None:
        raise OperationError('Identifiant invalide.')
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _implementation():
    return sha256(Path(__file__).read_bytes()).hexdigest()


def _cycle(raw, now):
    identity, start, end, duration, status, origin, interval, reasons, seen, progress, phase = raw
    identity = _identity(identity)
    start = _stamp(start)
    end = _stamp(end) if end is not None else None
    seen = _stamp(seen) if seen is not None else None
    progress = _stamp(progress) if progress is not None else None
    reasons = json.loads(reasons)
    if (status not in health.STATUSES or origin not in health.ORIGINS
            or type(interval) is not int or not 1 <= interval <= 1440
            or phase is not None and phase not in health.PHASES
            or not isinstance(reasons, list) or any(reason not in health.REASONS for reason in reasons)):
        raise OperationError('Contenu invalide.')
    issues = []
    if start > now:
        issues.append('future_cycle_start')
    if end is not None and end > now:
        issues.append('future_cycle_end')
    if end is not None and end < start:
        issues.append('end_before_start')
    if status == 'running' and end is not None:
        issues.append('running_with_finish')
    if status != 'running' and end is None:
        issues.append('finished_without_end')
    for name, instant in (('signal', seen), ('progress', progress)):
        if instant is not None:
            if instant > now:
                issues.append('future_' + name)
            if instant < start:
                issues.append(name + '_before_start')
            if end is not None and instant > end:
                issues.append(name + '_after_finish')
    if seen is not None and progress is not None and progress > seen:
        issues.append('progress_after_signal')
    if duration is not None and (type(duration) not in (int, float)
                                 or not math.isfinite(duration) or duration < 0):
        duration = None
        issues.append('invalid_measured_duration')
    if status == 'running' and duration is not None:
        issues.append('running_with_measured_duration')
    wall = ((end or now) - start).total_seconds()
    wall = wall if wall >= 0 else None
    difference = wall - duration if wall is not None and duration is not None else None
    if difference is not None and difference >= DIVERGENCE_SECONDS:
        issues.append('wall_measured_divergence')
    if difference is not None and difference <= -DIVERGENCE_SECONDS:
        issues.append('measured_exceeds_wall')
    age = (now - seen).total_seconds() if seen is not None else None
    signal = ('invalid_clock' if any(code not in ('wall_measured_divergence',) for code in issues)
              else 'recent' if age is not None and 0 <= age <= 3 * health.HEARTBEAT_SECONDS
              else 'stale' if age is not None else 'unavailable') if status == 'running' else 'unavailable'
    return dict(id=identity, started_at=start.isoformat(), finished_at=end.isoformat() if end else None,
                status=status, origin=origin, phase=phase, interval_minutes=interval, reasons=reasons,
                last_seen_at=seen.isoformat() if seen else None,
                last_progress_at=progress.isoformat() if progress else None,
                wall_duration_seconds=wall, measured_duration_seconds=duration,
                wall_minus_measured_seconds=difference, signal_state=signal, issues=issues)


def _event(raw, now):
    identity, observed, cycle_started, day, phase = raw
    identity = _identity(identity)
    observed, cycle_started = _stamp(observed), _stamp(cycle_started)
    if (not isinstance(day, str) or date.fromisoformat(day).isoformat() != day or phase not in PHASES):
        raise OperationError('Opération invalide.')
    issues = []
    if observed > now:
        issues.append('future_event')
    if observed < cycle_started:
        issues.append('event_before_cycle')
    if observed.astimezone(ZONE).date().isoformat() != day:
        issues.append('event_day_mismatch')
    return dict(id=identity, observed_at=observed.isoformat(), cycle_started_at=cycle_started.isoformat(),
                day=day, phase=phase, issues=issues)


def _snapshot(path, start):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)) as con:
        con.execute('PRAGMA query_only=ON')
        con.execute('BEGIN')
        if con.execute('PRAGMA user_version').fetchone()[0] != health.VERSION:
            raise OperationError('Schéma invalide.')
        columns = {row[1] for row in con.execute('PRAGMA table_info(cycles)')}
        optional = ','.join(name if name in columns else 'NULL' for name in ('last_seen_at', 'last_progress_at', 'phase'))
        required = {'id', 'started_at', 'finished_at', 'duration_seconds', 'status', 'origin', 'interval_minutes', 'reasons'}
        if not required <= columns:
            raise OperationError('Schéma invalide.')
        values = con.execute('''SELECT id,started_at,finished_at,duration_seconds,status,origin,
            interval_minutes,reasons,''' + optional + ''' FROM cycles
            WHERE started_at>=? OR finished_at>=? OR status='running'
            ORDER BY started_at DESC,id DESC LIMIT ?''', (start.isoformat(), start.isoformat(), CYCLE_LIMIT + 1)).fetchall()
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        events = []
        refresh_available = 'refresh_events' in tables
        if refresh_available:
            events = con.execute('''SELECT id,observed_at,cycle_started_at,day,phase FROM refresh_events
                WHERE observed_at>=? ORDER BY observed_at DESC,id DESC LIMIT ?''',
                (start.isoformat(), EVENT_LIMIT + 1)).fetchall()
        return values, events, refresh_available


def load(root, *, now=None, days=7, interval_minutes=15):
    """Load bounded observations in one query-only transaction; never repair.

    ``now`` is a report cutoff, never an execution or capture clock. Bad stores
    return a controlled unavailable state, without exception or provider text.
    """
    if type(days) is not int or not 1 <= days <= 365:
        raise OperationError('La fenêtre doit compter de 1 à 365 jours.')
    if type(interval_minutes) is not int or not 1 <= interval_minutes <= 1440:
        raise OperationError('L’intervalle doit compter de 1 à 1 440 minutes.')
    now = _stamp(now or datetime.now(timezone.utc))
    start = now - timedelta(days=days)
    threshold = interval_minutes * 120
    implementation = _implementation()
    base = dict(version=VERSION, as_of=now.isoformat(), window_start=start.isoformat(), days=days,
                interval_minutes=interval_minutes, stale_threshold_seconds=threshold,
                state='missing', truncated=False, total_cycles=0, cycles=[], resumptions=[], issues=[],
                refresh_events_available=False, observed_events=0, implementation_sha256=implementation,
                latest_operations={phase:dict(state='missing', observed_at=None, day=None, age_seconds=None)
                                   for phase in PHASES}, limitations=LIMITATIONS.copy())
    path = health.path(root)
    source = dict(version=VERSION, implementation_sha256=implementation, cycles=[], events=[], refresh_available=False)
    try:
        if path.is_symlink():
            raise OperationError('Chemin invalide.')
        if not path.exists():
            base['source_sha256'] = sha256(_json(source).encode()).hexdigest()
            return base
        raw_cycles, raw_events, refresh_available = _snapshot(path.resolve(), start)
        truncated = len(raw_cycles) > CYCLE_LIMIT or len(raw_events) > EVENT_LIMIT
        raw_cycles, raw_events = raw_cycles[:CYCLE_LIMIT], raw_events[:EVENT_LIMIT]
        cycles = [_cycle(row, now) for row in raw_cycles]
        events = [_event(row, now) for row in raw_events]
        if len({row['id'] for row in cycles}) != len(cycles) or len({row['id'] for row in events}) != len(events):
            raise OperationError('Identifiants répétés.')
        by_start = defaultdict(list)
        for row in cycles:
            by_start[row['started_at']].append(row)
        for event in events:
            matches = by_start[event['cycle_started_at']]
            if len(matches) != 1:
                event['issues'].append('cycle_identity_unavailable')
            elif matches[0]['finished_at'] and _stamp(event['observed_at']) > _stamp(matches[0]['finished_at']):
                event['issues'].append('event_after_cycle_end')
        # Fingerprint the controlled source, excluding age/state/other fields
        # that change merely because the user reads it a few seconds later.
        source_cycles = []
        for row, raw in zip(cycles, raw_cycles):
            value = {key:row[key] for key in ('id', 'started_at', 'finished_at', 'status', 'origin', 'phase',
                'interval_minutes', 'reasons', 'last_seen_at', 'last_progress_at', 'measured_duration_seconds')}
            # Invalid measurements are displayed as absent, but changing one
            # invalid scalar must still change the source fingerprint. Never
            # export a malformed scalar's freeform representation.
            value['duration_record_sha256'] = sha256((type(raw[3]).__name__ + ':' + repr(raw[3])).encode()).hexdigest()
            source_cycles.append(value)
        source.update(cycles=source_cycles, events=[{key:row[key] for key in ('id', 'observed_at', 'cycle_started_at', 'day', 'phase')}
            for row in events], refresh_available=refresh_available)
        for phase in PHASES:
            selected = [row for row in events if row['phase'] == phase]
            if selected:
                row = max(selected, key=lambda item:(item['observed_at'], item['id']))
                age = (now - _stamp(row['observed_at'])).total_seconds()
                base['latest_operations'][phase] = dict(
                    state='invalid_clock' if row['issues'] else 'recent' if age <= threshold else 'stale',
                    observed_at=row['observed_at'], day=row['day'], age_seconds=age,
                    issues=row['issues'])
        prior = {}
        calendar = None
        resumptions = []
        for row in sorted(events, key=lambda item:(item['observed_at'], item['id'])):
            if row['issues']:
                continue
            current = _stamp(row['observed_at'])
            previous = prior.get(row['cycle_started_at'])
            if previous is not None:
                gap = (current - _stamp(previous['observed_at'])).total_seconds()
                if gap >= threshold:
                    age = (current - _stamp(calendar['observed_at'])).total_seconds() if calendar else None
                    resumptions.append(dict(cycle_id=by_start[row['cycle_started_at']][0]['id'],
                        previous_at=previous['observed_at'], resumed_at=row['observed_at'], gap_seconds=gap,
                        resumed_phase=row['phase'], resumed_day=row['day'],
                        calendar_at=calendar['observed_at'] if calendar else None, calendar_age_seconds=age,
                        calendar_state='missing' if age is None else 'stale' if age > threshold else 'recent'))
            prior[row['cycle_started_at']] = row
            # The calendar completion at the resumed event itself must not
            # conceal how old the PREVIOUS calendar was when work resumed.
            if row['phase'] == 'calendar':
                calendar = row
        base.update(state='available', cycles=cycles, total_cycles=len(cycles), resumptions=resumptions,
                    truncated=truncated, observed_events=len(events), refresh_events_available=refresh_available,
                    issues=sorted({code for row in cycles + events for code in row['issues']}))
        base['source_sha256'] = sha256(_json(source).encode()).hexdigest()
        return base
    except (OperationError, OSError, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError):
        base.update(state='unavailable_or_invalid', issues=['store_unavailable_or_invalid'])
        base['source_sha256'] = None
        return base
