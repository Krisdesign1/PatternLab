"""Bounded, read-only operating evidence for the Linux deployment.

Only fixed systemd units are queried. Backup receipts describe a past control;
they never turn a timer on and never prove that the current files can restore.
No application, credential or prediction module is imported here.
"""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from time import monotonic


BACKUP_STATUS_PATH = Path('/var/lib/patternlab-ops/backup-status.json')
MAX_RECEIPT_BYTES = 32768
MAX_STATUS_BYTES = 32768
REPORT_MAX_AGE_SECONDS = 7200
_STATES = {'verified', 'deferred', 'error', 'unconfigured', 'running'}
_REASONS = {'none', 'worker_busy', 'backup_busy', 'insufficient_space',
            'snapshot_changed', 'verification_failed', 'permission_denied', 'unavailable'}
_SYSTEMD_STATES = {'active', 'activating', 'inactive', 'deactivating', 'failed',
                   'reloading', 'maintenance', 'refreshing'}
_UNIT_FILE_STATES = {'enabled', 'enabled-runtime', 'disabled', 'static', 'indirect',
                     'masked', 'masked-runtime', 'linked', 'linked-runtime',
                     'alias', 'generated', 'transient', 'bad', 'not-found'}
_RESULTS = {'success', 'resources', 'timeout', 'exit-code', 'signal', 'core-dump',
            'watchdog', 'start-limit-hit', 'oom-kill', 'exec-condition', 'protocol'}
_PROPERTIES = ('LoadState', 'ActiveState', 'SubState', 'UnitFileState', 'Result',
               'ExecMainStartTimestamp', 'ExecMainExitTimestamp', 'LastTriggerUSec',
               'NextElapseUSecRealtime', 'NextElapseUSecMonotonic', 'TimersMonotonic')


def _now(value):
    value = datetime.now(timezone.utc) if value is None else value
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('Une heure UTC explicite est nécessaire.')
    return value.astimezone(timezone.utc)


def _stamp(value, *, now=None, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError()
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or (now is not None and parsed > now + timedelta(minutes=5)):
        raise ValueError()
    return parsed.astimezone(timezone.utc).isoformat()


def _system_stamp(value):
    if not value or value in {'n/a', '0', 'infinity'}:
        return None
    try:
        return datetime.strptime(value, '%a %Y-%m-%d %H:%M:%S UTC').replace(
            tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def _duration(value):
    """Read systemd's C-locale, human durations or integer microseconds."""
    if not value or value == 'infinity':
        return None
    if value.isdigit():
        return int(value) / 1000000
    units = {'d': 86400, 'h': 3600, 'min': 60, 's': 1, 'ms': .001, 'us': .000001}
    matches = list(re.finditer(r'(\d+(?:\.\d+)?)(min|ms|us|d|h|s)', value))
    if not matches or re.sub(r'\s+', '', ''.join(m.group(0) for m in matches)) != re.sub(r'\s+', '', value):
        return None
    return sum(float(match.group(1)) * units[match.group(2)] for match in matches)


def _query(unit, runner):
    response = runner(['/usr/bin/systemctl', 'show', unit, '--no-pager',
                       '--property=' + ','.join(_PROPERTIES)],
                      capture_output=True, text=True, timeout=2, check=False,
                      env={'LC_ALL': 'C', 'TZ': 'UTC', 'PATH': '/usr/bin:/bin'})
    if response.returncode != 0 or not isinstance(response.stdout, str) or len(response.stdout) > MAX_STATUS_BYTES:
        raise ValueError()
    result = {}
    for line in response.stdout.splitlines():
        name, separator, value = line.partition('=')
        if separator and name in _PROPERTIES:
            if name == 'TimersMonotonic' and name in result:
                result[name] += '; ' + value
                continue
            if name in result:
                raise ValueError()
            result[name] = value
    if result.get('LoadState') not in {'loaded', 'not-found', 'masked', 'error', 'bad-setting', 'merged', 'stub'}:
        raise ValueError()
    if result['LoadState'] == 'loaded' and result.get('ActiveState') not in _SYSTEMD_STATES:
        raise ValueError()
    return result


def _unit_view(properties):
    state = properties.get('ActiveState')
    file_state = properties.get('UnitFileState')
    substate = properties.get('SubState', '')
    return dict(load_state=properties['LoadState'],
                active_state=state if state in _SYSTEMD_STATES else 'unknown',
                sub_state=substate if re.fullmatch(r'[a-z-]{1,32}', substate) else 'unknown',
                unit_file_state=file_state if file_state in _UNIT_FILE_STATES else 'unknown',
                result=properties.get('Result') if properties.get('Result') in _RESULTS else None,
                started_at=_system_stamp(properties.get('ExecMainStartTimestamp')),
                finished_at=_system_stamp(properties.get('ExecMainExitTimestamp')))


def _systemd_status(timer_name, worker_name, *, platform=None, runner=None, now=None):
    current = _now(now)
    result = dict(backend='systemd', supported=(sys.platform if platform is None else platform) == 'linux',
                  available=False, installed=False, enabled=None, running=False,
                  process_running=False, interval_seconds=None, observed_at=current.isoformat())
    if not result['supported']:
        result.update(backend='unsupported', error='Ce contrôle systemd est réservé au serveur Linux.')
        return result
    try:
        timer = _query(timer_name, runner or subprocess.run)
        worker = _query(worker_name, runner or subprocess.run)
        file_state = timer.get('UnitFileState')
        enabled = (file_state in {'enabled', 'enabled-runtime'}
                   if file_state in _UNIT_FILE_STATES else None)
        result.update(available=True, installed=timer['LoadState'] == 'loaded',
                      enabled=enabled,
                      running=timer.get('ActiveState') == 'active',
                      process_running=worker.get('ActiveState') in {'active', 'activating', 'reloading'},
                      timer=_unit_view(timer), worker=_unit_view(worker))
        result['timer']['last_trigger_at'] = _system_stamp(timer.get('LastTriggerUSec'))
        result['timer']['next_elapse_at'] = _system_stamp(timer.get('NextElapseUSecRealtime'))
        # A monotonic-only timer has no NextElapseUSecRealtime. Convert its
        # actual monotonic target, not a configured or presumed interval.
        target = _duration(timer.get('NextElapseUSecMonotonic'))
        if target and result['running'] and not result['process_running']:
            delay = target - monotonic()
            if 0 <= delay <= 366 * 86400:
                monotonic_at = (current + timedelta(seconds=delay)).isoformat()
                realtime_at = result['timer']['next_elapse_at']
                result['timer']['next_elapse_at'] = min(realtime_at, monotonic_at) if realtime_at else monotonic_at
        for name in ('OnUnitInactiveUSec', 'OnActiveUSec', 'OnCalendar'):
            match = re.search(r'\b' + name + r'=([^;{}]+)', timer.get('TimersMonotonic', ''))
            if match:
                duration = _duration(match.group(1).strip())
                if duration is not None and name == 'OnUnitInactiveUSec':
                    result['interval_seconds'] = duration
        return result
    except (OSError, ValueError, subprocess.SubprocessError):
        result['error'] = 'État systemd indisponible ; aucun fonctionnement automatique n’est supposé.'
        return result


def scheduler_status(*, platform=None, runner=None, now=None):
    """Observe only the fixed data worker and its timer; never starts a unit."""
    return _systemd_status('patternlab-worker.timer', 'patternlab-worker.service',
                           platform=platform, runner=runner, now=now)


def _keys(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ValueError()
    return value


def _count(value):
    if type(value) is not int or not 0 <= value <= 10**16:
        raise ValueError()
    return value


def _receipt(path, *, owner):
    # Reject links in the entire route. The production directory and file are
    # root-owned and not writable by the web user. O_NOFOLLOW closes the final
    # file-link race; fstat validates the exact open file before any content.
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise ValueError()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        parent = path.parent.stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RECEIPT_BYTES or info.st_nlink != 1
                or info.st_mode & 0o022 or parent.st_mode & 0o022
                or (owner is not None and (info.st_uid != owner or parent.st_uid != owner))):
            raise ValueError()
        data = stream.read(MAX_RECEIPT_BYTES + 1)
    if len(data) > MAX_RECEIPT_BYTES:
        raise ValueError()
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique)


def _backup_receipt(value, current):
    value = _keys(value, {'version', 'observed_at', 'state', 'last_backup', 'schedule', 'capacity', 'last_attempt'},
                  {'version', 'observed_at', 'state'})
    if value['version'] != 'backup-status-v1' or value['state'] not in _STATES:
        raise ValueError()
    result = dict(version=value['version'], observed_at=_stamp(value['observed_at'], now=current), state=value['state'])
    if value.get('last_backup') is not None:
        backup = _keys(value['last_backup'], {'created_at', 'verified_at', 'files_verified', 'databases_restored',
                                            'payload_bytes', 'point_name'},
                       {'created_at', 'verified_at', 'files_verified', 'databases_restored', 'payload_bytes', 'point_name'})
        name = backup['point_name']
        if not isinstance(name, str) or not re.fullmatch(
                r'(?:first-aws-\d{8}|daily-\d{8}T\d{6}Z-[a-f0-9]{8}\.tar\.gz)', name):
            raise ValueError()
        result['last_backup'] = dict(created_at=_stamp(backup['created_at'], now=current),
                                   verified_at=_stamp(backup['verified_at'], now=current), point_name=name,
                                   **{key: _count(backup[key]) for key in ('files_verified', 'databases_restored', 'payload_bytes')})
        if datetime.fromisoformat(result['last_backup']['verified_at']) < datetime.fromisoformat(result['last_backup']['created_at']):
            raise ValueError()
    if value['state'] == 'verified' and not result.get('last_backup'):
        raise ValueError()
    if value.get('schedule') is not None:
        schedule = _keys(value['schedule'], {'interval_hours', 'retention_count'}, {'interval_hours', 'retention_count'})
        interval, retention = _count(schedule['interval_hours']), _count(schedule['retention_count'])
        if not 1 <= interval <= 8760 or not 1 <= retention <= 365:
            raise ValueError()
        result['schedule'] = dict(interval_hours=interval, retention_count=retention)
    if value.get('capacity') is not None:
        capacity = _keys(value['capacity'], {'free_bytes'}, {'free_bytes'})
        result['capacity'] = dict(free_bytes=_count(capacity['free_bytes']))
    if value.get('last_attempt') is not None:
        attempt = _keys(value['last_attempt'], {'started_at', 'finished_at', 'state', 'reason_code'},
                        {'started_at', 'finished_at', 'state', 'reason_code'})
        if attempt['state'] not in _STATES or attempt['reason_code'] not in _REASONS:
            raise ValueError()
        result['last_attempt'] = dict(started_at=_stamp(attempt['started_at'], now=current),
                                    finished_at=_stamp(attempt['finished_at'], now=current, nullable=True),
                                    state=attempt['state'], reason_code=attempt['reason_code'])
        if (result['last_attempt']['finished_at'] is not None
                and datetime.fromisoformat(result['last_attempt']['finished_at']) < datetime.fromisoformat(result['last_attempt']['started_at'])):
            raise ValueError()
    observed = datetime.fromisoformat(result['observed_at'])
    result['observation_age_seconds'] = max(0, (current - observed).total_seconds())
    result['observation_stale'] = result['observation_age_seconds'] > REPORT_MAX_AGE_SECONDS
    backup = result.get('last_backup')
    result['backup_age_seconds'] = ((current - datetime.fromisoformat(backup['verified_at'])).total_seconds()
                                    if backup else None)
    result['backup_overdue'] = (result['backup_age_seconds'] is not None
                               and result['backup_age_seconds'] > result.get('schedule', {}).get('interval_hours', 24) * 5400)
    return result


def backup_status(path=None, *, now=None, platform=None, runner=None, expected_owner=None):
    """Read a safe receipt and independently observe the actual backup timer.

    Explicit test paths may specify an owner. The production default always
    requires root ownership; a receipt supplied by the web account is refused.
    """
    current = _now(now)
    target = BACKUP_STATUS_PATH if path is None else Path(path)
    owner = 0 if path is None else expected_owner
    result = dict(available=False, state='unconfigured', last_backup=None,
                  observation_stale=True, backup_overdue=False)
    try:
        result.update(_backup_receipt(_receipt(target, owner=owner), current), available=True)
    except FileNotFoundError:
        result['error'] = 'Aucun compte rendu de sauvegarde serveur disponible.'
    except (OSError, ValueError, TypeError, OverflowError):
        result.update(state='invalid', error='Le compte rendu de sauvegarde ne peut pas être validé.')
    result['scheduler'] = _systemd_status('patternlab-backup.timer', 'patternlab-backup.service',
                                         platform=platform, runner=runner, now=current)
    return result
