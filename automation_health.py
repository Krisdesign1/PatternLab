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
from threading import Event, Lock, Thread
from uuid import uuid4


VERSION = 1
FILENAME = '.automation_health.sqlite3'
ORIGINS = {'direct', 'manual', 'service'}
STATUSES = {'running', 'completed', 'paused', 'busy', 'error', 'interrupted'}
COUNTERS = ('requests', 'created', 'already_recorded', 'history_imports',
            'history_received', 'groups_considered', 'settled', 'pending', 'day_refreshes')
PHASES = {'starting':'Démarrage', 'calendar':'Calendrier', 'results':'Résultats',
          'forecasts':'Prévisions', 'history':'Historique', 'reconcile':'Rapprochement local', 'finished':'Fin du passage'}
HEARTBEAT_SECONDS = 30
REASONS = {
    'busy': 'Un autre cycle utilisait les données.',
    'day_changed': 'La journée locale a changé pendant le passage.',
    'provider_quota': 'Quota communiqué par API-Football atteint.',
    'provider_pause': 'Temporisation de la connexion API.',
    'time_window': 'Temps disponible écoulé ; reprise au prochain passage.',
    'data_error': 'Erreur de données, de connexion ou de stockage ; voir le dernier rapport.',
    'interrupted': 'Exécution interrompue avant sa fin normale.',
    'maintenance': 'Arrêt du service confirmé pour appliquer une mise à jour locale.',
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
        connection.execute('''CREATE TABLE IF NOT EXISTS refresh_events (
            id TEXT PRIMARY KEY, observed_at TEXT NOT NULL, cycle_started_at TEXT NOT NULL,
            day TEXT NOT NULL, phase TEXT NOT NULL, counters TEXT NOT NULL)''')
        connection.execute('CREATE INDEX IF NOT EXISTS refresh_events_observed ON refresh_events(observed_at)')
        # Additive migration remains compatible with a v1 worker finishing its
        # already running cycle. Reads never change a schema.
        columns = {row[1] for row in connection.execute('PRAGMA table_info(cycles)')}
        for name in ('last_seen_at','last_progress_at','phase'):
            if name not in columns:
                connection.execute(f'ALTER TABLE cycles ADD COLUMN {name} TEXT')
        connection.execute(f'PRAGMA user_version={VERSION}')
        return connection
    except BaseException:
        connection.close()
        raise


def record_refresh(root, *, phase, when, cycle_started_at, day, counters):
    """Append evidence of a completed operation, never infer uptime from it.

    The observation ledger is best effort and deliberately contains no provider
    payload, error text, credential, model output or arbitrary counter key.
    """
    try:
        from datetime import date
        if phase not in ('calendar','results','forecasts','reconcile') or date.fromisoformat(day).isoformat()!=day:
            raise HealthError('Observation locale invalide.')
        observed, started = _stamp(when), _stamp(cycle_started_at)
        if observed < started:
            raise HealthError('Observation antérieure au cycle.')
        safe = {key:counters[key] for key in COUNTERS
                if type(counters.get(key)) is int and counters[key]>=0}
        with closing(_connection(root)) as connection, connection:
            connection.execute('INSERT INTO refresh_events VALUES (?,?,?,?,?,?)',
                (uuid4().hex,observed.isoformat(),started.isoformat(),day,phase,json.dumps(safe,sort_keys=True)))
        return True
    except (OSError,sqlite3.Error,HealthError,ValueError,TypeError,AttributeError):
        return False


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


class _Pulse:
    def __init__(self, root, run_id, report, clock, enabled):
        self.root, self.run_id, self.report, self.clock = root, run_id, report, clock
        self.enabled = enabled
        self.phase = 'starting'
        self.previous = None
        self.lock = Lock()
        self.write_lock = Lock()

    def progress(self, phase):
        if phase not in PHASES:
            raise HealthError('Phase du cycle invalide.')
        with self.lock:
            self.phase = phase
        self.beat()

    def beat(self):
        if not self.enabled:
            return
        with self.write_lock:
            self._write_beat()

    def _write_beat(self):
        try:
            with self.lock:
                phase = self.phase
            counters = {key:self.report[key] for key in COUNTERS
                        if type(self.report.get(key)) is int and self.report[key] >= 0}
            signature = (phase, json.dumps(counters,sort_keys=True))
            now = _stamp(self.clock()).isoformat()
            changed = signature != self.previous
            with closing(_connection(self.root)) as connection, connection:
                connection.execute('''UPDATE cycles SET last_seen_at=?,phase=?,counters=?,
                    last_progress_at=CASE WHEN ? THEN ? ELSE last_progress_at END
                    WHERE id=? AND status='running' ''',
                    (now,phase,signature[1],changed,now,self.run_id))
            self.previous = signature
        except (OSError,sqlite3.Error,HealthError,ValueError,TypeError,RuntimeError):
            self.report['monitoring_warning'] = MONITORING_WARNING

    def watch(self, stopped):
        while not stopped.wait(HEARTBEAT_SECONDS):
            self.beat()


@contextmanager
def monitor(root, report, *, interval_minutes, clock, origin='direct', enabled=True):
    """Record an attempt without changing its calls, result or failure behavior.

    Diagnostics are best effort: a ledger failure is surfaced in the report,
    but must not abort forecasts or hide the original worker exception.
    """
    if not enabled:
        yield _Pulse(root, None, report, clock, False)
        return
    if origin not in ORIGINS:
        raise HealthError('Origine du cycle invalide.')
    run_id = uuid4().hex
    started = time.perf_counter()
    recorded = False
    try:
        with closing(_connection(root)) as connection, connection:
            connection.execute('''INSERT INTO cycles
                (id,started_at,finished_at,duration_seconds,status,origin,interval_minutes,reasons,counters)
                VALUES (?,?,?,?,?,?,?,?,?)''',
                (run_id, _stamp(report['started_at']).isoformat(), None, None,
                 'running', origin, int(interval_minutes), '[]', '{}'))
        recorded = True
    except (OSError, sqlite3.Error, HealthError, ValueError, TypeError):
        report['monitoring_warning'] = MONITORING_WARNING
    pulse = _Pulse(root,run_id,report,clock,recorded)
    stopped = Event()
    heartbeat = None
    if recorded:
        pulse.beat()
        heartbeat = Thread(target=pulse.watch,args=(stopped,),name='patternlab-cycle-heartbeat',daemon=True)
        try:
            heartbeat.start()
        except RuntimeError:
            heartbeat = None
            report['monitoring_warning'] = MONITORING_WARNING
    interrupted = False
    error = False
    try:
        yield pulse
    except BaseException as exception:
        error = True
        interrupted = not isinstance(exception, Exception)
        raise
    finally:
        stopped.set()
        if heartbeat is not None:
            heartbeat.join(timeout=2)
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
                    connection.execute('''UPDATE cycles SET finished_at=?,duration_seconds=?,status=?,reasons=?,counters=?,
                        last_seen_at=?,phase='finished' WHERE id=? AND status='running' ''',
                        (finished, duration, status, json.dumps(reasons), json.dumps(counters), finished, run_id))
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
            columns = {row[1] for row in connection.execute('PRAGMA table_info(cycles)')}
            extras = ','.join(name if name in columns else 'NULL' for name in ('last_seen_at','last_progress_at','phase'))
            total, first = connection.execute('SELECT count(*),min(started_at) FROM cycles').fetchone()
            values = connection.execute('''SELECT id,started_at,finished_at,duration_seconds,
                status,origin,interval_minutes,reasons,counters,''' + extras + ''' FROM cycles
                WHERE started_at>=? AND started_at<=?
                ORDER BY started_at DESC,id DESC LIMIT ?''',
                ((now-timedelta(days=days)).isoformat(), now.isoformat(), limit+1)).fetchall()
        rows = []
        for value in values[:limit]:
            identifier, started, finished, duration, status, origin, interval, reasons, counters, last_seen, progress, phase = value
            _stamp(started)
            if finished is not None:
                _stamp(finished)
            for stamp in (last_seen,progress):
                if stamp is not None:
                    _stamp(stamp)
            if (status not in STATUSES or origin not in ORIGINS or type(interval) is not int or interval < 1
                    or phase is not None and phase not in PHASES
                    or (duration is not None and (not math.isfinite(duration) or duration < 0))):
                raise HealthError('Le journal local contient un état invalide.')
            reasons, counters = json.loads(reasons), json.loads(counters)
            if (not isinstance(reasons, list) or any(code not in REASONS for code in reasons)
                    or not isinstance(counters, dict)
                    or any(key not in COUNTERS or type(count) is not int or count < 0 for key, count in counters.items())):
                raise HealthError('Le journal local contient un état invalide.')
            rows.append(dict(id=identifier, started_at=started, finished_at=finished,
                             duration_seconds=duration, status=status, origin=origin,
                             interval_minutes=interval, reasons=reasons, counters=counters,
                             last_seen_at=last_seen,last_progress_at=progress,phase=phase,
                             elapsed_seconds=max(0, (now-_stamp(started)).total_seconds()) if status=='running' else duration,
                             signal_state=('recent' if (now-_stamp(last_seen)).total_seconds() <= 3*HEARTBEAT_SECONDS
                                           else 'stale') if status=='running' and last_seen else 'unavailable'))
        return dict(rows=rows, total=total, first_started_at=first, truncated=len(values)>limit)
    except (OSError, sqlite3.Error, ValueError, TypeError):
        raise HealthError('Le journal local des cycles est indisponible ou invalide.') from None


def confirm_service_interruption(root, ids, *, confirmed_at):
    """Record an operator-confirmed stopped service; never infer it from age.

    The caller must first verify the scheduler/process has actually stopped.
    No elapsed wall time is invented as a monotonic measured duration. Already
    finished attempts are never overwritten. No forecasts or API state change.
    """
    ids = list(ids)
    if not ids or any(not isinstance(value,str) or len(value)!=32 or
                      any(character not in '0123456789abcdef' for character in value) for value in ids):
        raise HealthError('Identifiants des passages à interrompre invalides.')
    stamp = _stamp(confirmed_at).isoformat()
    if not path(root).exists():
        return 0
    changed = 0
    try:
        with closing(_connection(root)) as connection, connection:
            for identity in ids:
                cursor = connection.execute('''UPDATE cycles SET status='interrupted',finished_at=?,
                    reasons=?,phase='finished' WHERE id=? AND status='running' AND origin='service' AND started_at<=?''',
                    (stamp,json.dumps(['interrupted','maintenance']),identity,stamp))
                changed += cursor.rowcount
        return changed
    except (OSError,sqlite3.Error):
        raise HealthError('L’arrêt confirmé ne peut pas être enregistré dans le journal local.') from None


def summarize(view, *, now=None, schedule_expected=False):
    """Describe observed gaps, without treating a stopped Mac as a model error."""
    now = _stamp(now or datetime.now(timezone.utc))
    rows = view['rows']
    counts = {status: sum(row['status'] == status for row in rows) for status in STATUSES}
    completed = [row['duration_seconds'] for row in rows if row['duration_seconds'] is not None]
    service = sorted((row for row in rows if row['origin'] == 'service'), key=lambda row: row['started_at'])
    gaps = []
    for previous, following in zip(service, service[1:]):
        if previous['status'] == 'running':
            continue  # No known end: do not invent an idle scheduling gap.
        expected = _stamp(previous['started_at']) + timedelta(minutes=previous['interval_minutes'])
        if previous['finished_at']:
            expected = max(expected, _stamp(previous['finished_at']))
        observed = _stamp(following['started_at'])
        if observed > expected + timedelta(minutes=previous['interval_minutes']/2):
            gaps.append(dict(start=expected.isoformat(), end=observed.isoformat(),
                             minutes=(observed-expected).total_seconds()/60, ongoing=False))
    if schedule_expected and service and service[-1]['status'] != 'running':
        previous = service[-1]
        expected = _stamp(previous['started_at']) + timedelta(minutes=previous['interval_minutes'])
        if previous['finished_at']:
            expected = max(expected, _stamp(previous['finished_at']))
        if now > expected + timedelta(minutes=previous['interval_minutes']/2):
            gaps.append(dict(start=expected.isoformat(), end=now.isoformat(),
                             minutes=(now-expected).total_seconds()/60, ongoing=True))
    return dict(counts=counts, gaps=gaps, longest_duration=max(completed, default=None),
                service_observations=len(service), observation_started=view['first_started_at'])
