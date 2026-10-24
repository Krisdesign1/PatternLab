"""Small local execution ledger; it never reads fixtures or contacts an API.

The dot-prefixed SQLite file deliberately stays outside Cloud publications.
Rows describe observed worker attempts, not server uptime or predictive skill.
"""
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
import time
from uuid import uuid4


VERSION = 1
FILENAME = '.automation_health.sqlite3'
ORIGINS = {'direct', 'manual', 'service'}
STATUSES = {'running', 'completed', 'paused', 'busy', 'error', 'interrupted'}
COUNTERS = ('requests', 'created', 'already_recorded', 'history_imports',
            'history_received', 'groups_considered', 'settled', 'pending')
REASONS = {
    'busy': 'Un autre cycle utilisait les données.',
    'day_changed': 'La journée locale a changé pendant le passage.',
    'provider_quota': 'Quota communiqué par API-Football atteint.',
    'provider_pause': 'Temporisation de la connexion API.',
    'time_window': 'Temps disponible écoulé ; reprise au prochain passage.',
    'data_error': 'Erreur de données, de connexion ou de stockage ; voir le dernier rapport.',
    'interrupted': 'Exécution interrompue avant sa fin normale.',
    'warning': 'Le rapport contient des points à vérifier.',
}
MONITORING_WARNING = 'Le journal local des cycles n’a pas pu être actualisé. Le traitement des données reste indépendant.'


class HealthError(ValueError):
    pass


def path(root):
    return Path(root) / FILENAME


def _stamp(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            raise HealthError('Date du journal des cycles invalide.') from None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise HealthError('Date du journal des cycles invalide.')
    return value.astimezone(timezone.utc)


def _connection(root):
    target = path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target, timeout=1)
    try:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA synchronous=NORMAL')
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, VERSION):
            raise HealthError('Version du journal local des cycles incompatible.')
        connection.execute('''CREATE TABLE IF NOT EXISTS cycles (
            id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
            duration_seconds REAL, status TEXT NOT NULL, origin TEXT NOT NULL,
            interval_minutes INTEGER NOT NULL, reasons TEXT NOT NULL,
            counters TEXT NOT NULL)''')
        connection.execute('CREATE INDEX IF NOT EXISTS cycles_started ON cycles(started_at)')
        connection.execute(f'PRAGMA user_version={VERSION}')
        return connection
    except BaseException:
        connection.close()
        raise


def _reason_codes(report, interrupted=False):
    # Persist only controlled labels, never arbitrary HTTP/exception text that
    # could contain a credential or a provider response.
    if interrupted:
        return ['interrupted']
    status = report.get('status')
    if status == 'busy':
        return ['busy']
    issues = report.get('issues') or []
    text = ' '.join(str(issue) for issue in issues).casefold()
    result = []
    for code, needles in (
        ('day_changed', ('journée de kinshasa a changé',)),
        ('provider_quota', ('quota',)),
        ('provider_pause', ('temporairement en pause', 'temporisation')),
        ('time_window', ('avant l’arrêt de l’hébergeur', 'durée disponible')),
    ):
        if any(needle in text for needle in needles):
            result.append(code)
    if status == 'error':
        result.append('data_error')
    elif issues and not result:
        result.append('warning')
    return result


@contextmanager
def monitor(root, report, *, interval_minutes, clock, origin='direct', enabled=True):
    """Record an attempt without changing its calls, result or failure behavior.

    Diagnostics are best effort: a ledger failure is surfaced in the report,
    but must not abort forecasts or hide the original worker exception.
    """
    if not enabled:
        yield
        return
    if origin not in ORIGINS:
        raise HealthError('Origine du cycle invalide.')
    run_id = uuid4().hex
    started = time.perf_counter()
    recorded = False
    try:
        with closing(_connection(root)) as connection, connection:
            connection.execute('INSERT INTO cycles VALUES (?,?,?,?,?,?,?,?,?)',
                (run_id, _stamp(report['started_at']).isoformat(), None, None,
                 'running', origin, int(interval_minutes), '[]', '{}'))
        recorded = True
    except (OSError, sqlite3.Error, HealthError, ValueError, TypeError):
        report['monitoring_warning'] = MONITORING_WARNING
    interrupted = False
    error = False
    try:
        yield
    except BaseException as exception:
        error = True
        interrupted = not isinstance(exception, Exception)
        raise
    finally:
        duration = max(0.0, time.perf_counter() - started)
        report['duration_seconds'] = round(duration, 3)
        if recorded:
            try:
                finished = _stamp(clock()).isoformat()
                status = 'interrupted' if interrupted else ('error' if error else report.get('status'))
                if status not in STATUSES - {'running'} or not math.isfinite(duration):
                    raise HealthError('État du cycle invalide.')
                counters = {key: report[key] for key in COUNTERS
                            if type(report.get(key)) is int and report[key] >= 0}
                reasons = _reason_codes(dict(report, status=status), interrupted)
                with closing(_connection(root)) as connection, connection:
                    connection.execute('UPDATE cycles SET finished_at=?,duration_seconds=?,status=?,reasons=?,counters=? WHERE id=?',
                        (finished, duration, status, json.dumps(reasons), json.dumps(counters), run_id))
            except (OSError, sqlite3.Error, HealthError, ValueError, TypeError):
                report['monitoring_warning'] = MONITORING_WARNING


def load(root, *, now=None, days=7, limit=300):
    """Read a bounded window without creating a database on a page render."""
    now = _stamp(now or datetime.now(timezone.utc))
    if type(days) is not int or not 1 <= days <= 365 or type(limit) is not int or not 1 <= limit <= 1000:
        raise HealthError('Fenêtre du journal des cycles invalide.')
    if not path(root).exists():
        return dict(rows=[], total=0, first_started_at=None, truncated=False)
    try:
        uri = path(root).resolve().as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as connection:
            if connection.execute('PRAGMA user_version').fetchone()[0] != VERSION:
                raise HealthError('Version du journal local des cycles incompatible.')
            total, first = connection.execute('SELECT count(*),min(started_at) FROM cycles').fetchone()
            values = connection.execute('''SELECT id,started_at,finished_at,duration_seconds,
                status,origin,interval_minutes,reasons,counters FROM cycles
                WHERE started_at>=? AND started_at<=?
                ORDER BY started_at DESC,id DESC LIMIT ?''',
                ((now-timedelta(days=days)).isoformat(), now.isoformat(), limit+1)).fetchall()
        rows = []
        for value in values[:limit]:
            identifier, started, finished, duration, status, origin, interval, reasons, counters = value
            _stamp(started)
            if finished is not None:
                _stamp(finished)
            if (status not in STATUSES or origin not in ORIGINS or type(interval) is not int or interval < 1
                    or (duration is not None and (not math.isfinite(duration) or duration < 0))):
                raise HealthError('Le journal local contient un état invalide.')
            reasons, counters = json.loads(reasons), json.loads(counters)
            if (not isinstance(reasons, list) or any(code not in REASONS for code in reasons)
                    or not isinstance(counters, dict)
                    or any(key not in COUNTERS or type(count) is not int or count < 0 for key, count in counters.items())):
                raise HealthError('Le journal local contient un état invalide.')
            rows.append(dict(id=identifier, started_at=started, finished_at=finished,
                             duration_seconds=duration, status=status, origin=origin,
                             interval_minutes=interval, reasons=reasons, counters=counters))
        return dict(rows=rows, total=total, first_started_at=first, truncated=len(values)>limit)
    except (OSError, sqlite3.Error, ValueError, TypeError):
        raise HealthError('Le journal local des cycles est indisponible ou invalide.') from None


def summarize(view, *, now=None, schedule_expected=False):
    """Describe observed gaps, without treating a stopped Mac as a model error."""
    now = _stamp(now or datetime.now(timezone.utc))
    rows = view['rows']
    counts = {status: sum(row['status'] == status for row in rows) for status in STATUSES}
    completed = [row['duration_seconds'] for row in rows if row['duration_seconds'] is not None]
    service = sorted((row for row in rows if row['origin'] == 'service'), key=lambda row: row['started_at'])
    gaps = []
    for previous, following in zip(service, service[1:]):
        expected = _stamp(previous['started_at']) + timedelta(minutes=previous['interval_minutes'])
        if previous['finished_at']:
            expected = max(expected, _stamp(previous['finished_at']))
        observed = _stamp(following['started_at'])
        if observed > expected + timedelta(minutes=previous['interval_minutes']/2):
            gaps.append(dict(start=expected.isoformat(), end=observed.isoformat(),
                             minutes=(observed-expected).total_seconds()/60, ongoing=False))
    if schedule_expected and service:
        previous = service[-1]
        expected = _stamp(previous['started_at']) + timedelta(minutes=previous['interval_minutes'])
        if previous['finished_at']:
            expected = max(expected, _stamp(previous['finished_at']))
        if now > expected + timedelta(minutes=previous['interval_minutes']/2):
            gaps.append(dict(start=expected.isoformat(), end=now.isoformat(),
                             minutes=(now-expected).total_seconds()/60, ongoing=True))
    return dict(counts=counts, gaps=gaps, longest_duration=max(completed, default=None),
                service_observations=len(service), observation_started=view['first_started_at'])
