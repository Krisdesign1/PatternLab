#!/usr/bin/env python3
"""Private verified data backups, with bounded staging and safe retention.

No service is stopped and no live application database is restored. A point
is consistent per SQLite database, not atomic across the whole application.
Credentials and server configuration are deliberately outside this data scope.
The unchanged local_recovery policy selects the files. Only this command's own
new temporary staging tree is consumed while compressing it.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
from datetime import datetime, timezone
import fcntl
import gzip
import grp
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
import local_recovery as recovery

BACKUP_ROOT = Path('/var/lib/patternlab-backups')
STATUS_PATH = Path('/var/lib/patternlab-ops/backup-status.json')
VERSION = 'backup-status-v1'
ARCHIVE_NAME = re.compile(r'daily-\d{8}T\d{6}Z-[a-f0-9]{8}\.tar\.gz\Z')
RESERVE_BYTES = 1024 ** 3
RETENTION = 2
INTERVAL_HOURS = 24


def stamp():
    return datetime.now(timezone.utc).isoformat()


def _safe_directory(path, *, mode):
    """Root-managed directories must not be replaceable by the web user."""
    path = Path(path)
    if not path.is_absolute() or any(parent.is_symlink() for parent in (path, *path.parents)):
        raise PermissionError('unsafe_directory')
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    info = path.stat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o022):
        raise PermissionError('unsafe_directory')


def _atomic_json(path, value, *, mode=0o600, group=None):
    if path.is_symlink():
        raise PermissionError('unsafe_report')
    descriptor, temporary = tempfile.mkstemp(prefix='.backup-report-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), mode)
            if group is not None:
                os.fchown(stream.fileno(), -1, group)
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        Path(temporary).unlink(missing_ok=True)


def worker_idle(*, runner=subprocess.run):
    try:
        result = runner(['systemctl', 'show', 'patternlab-worker.service',
                         '--property=ActiveState,SubState,MainPID,ControlPID'],
                        capture_output=True, text=True, timeout=5, check=False)
        fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        return (result.returncode == 0 and fields.get('ActiveState') in {'inactive', 'failed'}
                and fields.get('MainPID') == '0' and fields.get('ControlPID') == '0')
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False


def capacity_plan(root, destination, *, reserve=RESERVE_BYTES):
    """Include WAL growth and one extra largest file during SQLite QA / tar add."""
    paths = recovery.source_files(root)
    sizes = []
    for relative in paths:
        path = root / relative
        size = path.stat().st_size
        if path.suffix.casefold() in recovery.DATABASE_SUFFIXES:
            wal = Path(str(path) + '-wal')
            if wal.is_file():
                size += wal.stat().st_size
        sizes.append(size)
    total, largest = sum(sizes), max(sizes, default=0)
    # gzip can expand incompressible input; include tar headers/padding and
    # deflate block overhead before starting any new point or retention.
    overhead = max(1024 * 1024, total // 1000 + len(paths) * 4096)
    # One snapshot file at a time is retained while archiving. The archive
    # grows separately; each compressed write checks the remaining reserve.
    # Do not require a second complete 3GB dataset just to refresh a backup.
    # Snapshot and archive-restore phases are sequential, never two full
    # largest-file copies at once. Compressed growth is checked separately
    # on every write, and each later restore rechecks its complete size.
    required = largest + overhead + reserve
    free = shutil.disk_usage(destination).free
    return dict(source_bytes=total, largest_file_bytes=largest, required_free_bytes=required,
                free_bytes=free, sufficient=free >= required, files=len(paths))


class CapacityError(OSError):
    pass


class _ReservedWriter:
    def __init__(self, stream, destination, reserve):
        self.stream, self.destination, self.reserve = stream, destination, reserve

    def write(self, block):
        if shutil.disk_usage(self.destination).free < self.reserve + len(block):
            raise CapacityError('insufficient_space')
        return self.stream.write(block)

    def flush(self):
        return self.stream.flush()


def _stream_snapshot(root, archive, work, *, reserve):
    """Stage one consistent file, compress it, then release that staging space.

    SQLite uses the existing online backup and schema/integrity checks. An
    independent pass subsequently restores every archived database and checks
    all archive hashes. No source file or previous backup is removed.
    """
    manifest = dict(version=recovery.VERSION, state='complete', started_at=stamp(),
                    atomic_across_databases=False, **recovery.SECURITY_SCOPE, files=[])
    scratch = work / 'snapshot-file'
    with archive.open('xb') as raw:
        os.fchmod(raw.fileno(), 0o600)
        guarded = _ReservedWriter(raw, archive.parent, reserve)
        with tarfile.open(fileobj=guarded, mode='w|gz', compresslevel=6) as output:
            for relative in recovery.source_files(root):
                source = root / relative
                # Never follow a newly replaced link between inventory/copy.
                if any(path.is_symlink() for path in (source, *source.parents) if path != root):
                    raise ValueError('snapshot_changed')
                estimated = source.stat().st_size
                wal = Path(str(source) + '-wal')
                if source.suffix.casefold() in recovery.DATABASE_SUFFIXES and wal.is_file():
                    estimated += wal.stat().st_size
                if shutil.disk_usage(archive.parent).free < estimated + reserve:
                    raise CapacityError('insufficient_space')
                item = dict(path=relative.as_posix())
                if source.suffix.casefold() in recovery.DATABASE_SUFFIXES:
                    recovery._backup_database(source, scratch)
                    item.update(kind='sqlite', database=recovery._database_summary(scratch))
                else:
                    before = recovery.fingerprint(source)
                    shutil.copy2(source, scratch)
                    if before != recovery.fingerprint(scratch) or before != recovery.fingerprint(source):
                        raise ValueError('snapshot_changed')
                    item['kind'] = 'file'
                item.update(sha256=recovery.fingerprint(scratch), bytes=scratch.stat().st_size)
                output.add(scratch, arcname='files/' + item['path'], recursive=False)
                manifest['files'].append(item)
                for suffix in ('', '-wal', '-shm'):
                    Path(str(scratch) + suffix).unlink(missing_ok=True)
            manifest['completed_at'] = stamp()
            payload = json.dumps(manifest, ensure_ascii=False, allow_nan=False).encode()
            member = tarfile.TarInfo('manifest.json')
            member.size, member.mode = len(payload), 0o600
            output.addfile(member, io.BytesIO(payload))
        raw.flush()
        os.fsync(raw.fileno())
    return manifest


def verify_archive(archive, manifest, scratch, *, reserve=RESERVE_BYTES):
    """Read every compressed member; restore each archived SQLite into isolation."""
    items = recovery._manifest_items(manifest)
    expected = {'files/' + item['path']: item for item in items}
    seen = set()
    databases = 0
    with gzip.open(archive, 'rb') as decompressed, tarfile.open(fileobj=decompressed, mode='r|') as source:
        for member in source:
            if not member.isfile() or member.name in seen:
                raise ValueError('verification_failed')
            seen.add(member.name)
            data = source.extractfile(member)
            if data is None:
                raise ValueError('verification_failed')
            if member.name == 'manifest.json':
                # The locally created manifest is bounded by its actual size.
                if member.size > 32 * 1024 * 1024:
                    raise ValueError('verification_failed')
                if json.loads(data.read()) != manifest:
                    raise ValueError('verification_failed')
                continue
            item = expected.get(member.name)
            if item is None or member.size != item['bytes']:
                raise ValueError('verification_failed')
            restored = scratch / 'restored.sqlite3'
            if item['kind'] == 'sqlite' and shutil.disk_usage(scratch).free < item['bytes'] + reserve:
                raise CapacityError('insufficient_space')
            digest = hashlib.sha256()
            count = 0
            with (restored.open('xb') if item['kind'] == 'sqlite' else nullcontext(None)) as target:
                for block in iter(lambda: data.read(1024 * 1024), b''):
                    digest.update(block)
                    count += len(block)
                    if target is not None:
                        _ReservedWriter(target, scratch, reserve).write(block)
            if count != item['bytes'] or digest.hexdigest() != item['sha256']:
                raise ValueError('verification_failed')
            if item['kind'] == 'sqlite':
                if recovery._database_summary(restored) != item['database']:
                    raise ValueError('verification_failed')
                databases += 1
                # This isolated restore has no writers. Its empty SQLite
                # sidecars belong to this temporary tree, never production.
                for suffix in ('', '-wal', '-shm'):
                    Path(str(restored) + suffix).unlink(missing_ok=True)
        # tar stops at the first end marker. Drain its remaining padding to
        # force gzip CRC/trailer verification and reject non-padding payload.
        for block in iter(lambda: source.fileobj.read(1024 * 1024), b''):
            if any(block):
                raise ValueError('verification_failed')
    if seen != set(expected) | {'manifest.json'}:
        raise ValueError('verification_failed')
    return dict(files_verified=len(items), databases_restored=databases,
                payload_bytes=sum(item['bytes'] for item in items))


def _receipt(path):
    if (path.is_symlink() or not path.is_file() or path.stat().st_size > 16384
            or path.stat().st_uid != os.geteuid() or path.stat().st_nlink != 1):
        return None
    try:
        value = json.loads(path.read_text())
        name = value['point_name']
        if (value.get('state') != 'verified' or not ARCHIVE_NAME.fullmatch(name)
                or path.name != name + '.verified.json'
                or not re.fullmatch('[a-f0-9]{64}', value['archive_sha256'])
                or any(type(value[key]) is not int or value[key] < 0
                       for key in ('files_verified', 'databases_restored', 'payload_bytes'))):
            return None
        created = datetime.fromisoformat(value['created_at'])
        verified = datetime.fromisoformat(value['verified_at'])
        if created.tzinfo is None or verified.tzinfo is None:
            return None
        return value
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _known_points(destination):
    points = []
    for report in destination.glob('daily-*.tar.gz.verified.json'):
        value = _receipt(report)
        if value is None:
            continue
        archive = destination / value['point_name']
        if (archive.is_file() and not archive.is_symlink()
                and archive.stat().st_uid == os.geteuid()
                and archive.stat().st_nlink == 1):
            points.append((value['created_at'], archive, report, value))
    return sorted(points, key=lambda point: point[0], reverse=True)


def retain_verified(destination, *, current_name, count=RETENTION):
    """Never touch the first recovery point, unknown files or the latest good one."""
    points = _known_points(destination)
    current = next((entry for entry in points if entry[1].name == current_name), None)
    if current is None or recovery.fingerprint(current[1]) != current[3]['archive_sha256']:
        raise ValueError('verification_failed')
    removed = 0
    for _created, archive, receipt, _value in points[max(1, count):]:
        if archive.name == current_name:
            continue
        archive.unlink()
        receipt.unlink()
        removed += 1
    return removed


def run(root=PROJECT_ROOT, destination=BACKUP_ROOT, status_path=STATUS_PATH, *,
        idle=worker_idle, reserve=RESERVE_BYTES, force=False, group=None, now=None):
    root, destination, status_path = Path(root).resolve(), Path(destination), Path(status_path)
    _safe_directory(destination, mode=0o700)
    _safe_directory(status_path.parent, mode=0o750)
    if group is not None:
        os.chown(status_path.parent, -1, group)
        os.chmod(status_path.parent, 0o750)
    started = stamp()
    report = dict(version=VERSION, observed_at=started, state='unconfigured',
                  schedule=dict(interval_hours=INTERVAL_HOURS, retention_count=RETENTION),
                  capacity=dict(free_bytes=shutil.disk_usage(destination).free),
                  last_attempt=dict(started_at=started, finished_at=None, state='running', reason_code='none'))
    points = _known_points(destination)
    if points:
        last = points[0][3]
        report['last_backup'] = {key: last[key] for key in (
            'created_at', 'verified_at', 'files_verified', 'databases_restored', 'payload_bytes', 'point_name')}
    elif status_path.is_file() and not status_path.is_symlink() and status_path.stat().st_size < 16384:
        # Keep the operator-verified initial point visible when an attempt is
        # deferred. Do not trust this display receipt for rotation or deletion.
        try:
            previous = json.loads(status_path.read_text())
            last = previous['last_backup']
            if (previous.get('version') == VERSION and status_path.stat().st_uid == os.geteuid()
                    and re.fullmatch('[a-zA-Z0-9_.-]{1,128}', last['point_name'])
                    and all(type(last[key]) is int and last[key] >= 0
                            for key in ('files_verified', 'databases_restored', 'payload_bytes'))):
                report['last_backup'] = {key: last[key] for key in (
                    'created_at', 'verified_at', 'files_verified', 'databases_restored', 'payload_bytes', 'point_name')}
        except (KeyError, TypeError, ValueError, OSError):
            pass

    def publish(state, reason='none'):
        report.update(observed_at=stamp(), state=state)
        report['last_attempt'].update(finished_at=report['observed_at'], state=state, reason_code=reason)
        report['capacity']['free_bytes'] = shutil.disk_usage(destination).free
        _atomic_json(status_path, report, mode=0o640, group=group)
        return report

    lock_path = destination / '.periodic-backup.lock'
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'r+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Do not overwrite the live owner's running status.
            return dict(state='deferred', reason_code='backup_busy')
        try:
            current = now or datetime.now(timezone.utc)
            if report.get('last_backup') and not force:
                elapsed = (current - datetime.fromisoformat(report['last_backup']['created_at'])).total_seconds()
                if 0 <= elapsed < INTERVAL_HOURS * 3600:
                    return publish('verified')
            if not idle():
                return publish('deferred', 'worker_busy')
            plan = capacity_plan(root, destination, reserve=reserve)
            if not plan['sufficient']:
                return publish('deferred', 'insufficient_space')
            report.update(state='running')
            _atomic_json(status_path, report, mode=0o640, group=group)
            name = 'daily-' + current.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8] + '.tar.gz'
            with tempfile.TemporaryDirectory(prefix='.daily-work-', dir=destination) as temporary:
                work = Path(temporary)
                archive = work / name
                manifest = _stream_snapshot(root, archive, work, reserve=reserve)
                scratch = work / 'restore'
                scratch.mkdir(mode=0o700)
                evidence = verify_archive(archive, manifest, scratch, reserve=reserve)
                result = dict(state='verified', point_name=name, created_at=started,
                              verified_at=stamp(), archive_sha256=recovery.fingerprint(archive),
                              atomic_across_databases=False, off_machine_backup=False,
                              credentials_included=False, full_server_configuration_backup=False, **evidence)
                published = destination / name
                if published.exists():
                    raise FileExistsError('destination_exists')
                os.replace(archive, published)
                _atomic_json(destination / (name + '.verified.json'), result)
            report['last_backup'] = {key: result[key] for key in (
                'created_at', 'verified_at', 'files_verified', 'databases_restored', 'payload_bytes', 'point_name')}
            # Only new, actually verified archives can trigger retention.
            retain_verified(destination, current_name=name)
            return publish('verified')
        except CapacityError:
            return publish('deferred', 'insufficient_space')
        except PermissionError:
            return publish('error', 'permission_denied')
        except (OSError, ValueError, sqlite3.Error, tarfile.TarError, EOFError):
            return publish('error', 'verification_failed')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--plan', action='store_true')
    modes.add_argument('--run', action='store_true')
    parser.add_argument('--root', type=Path, default=PROJECT_ROOT)
    parser.add_argument('--destination', type=Path, default=BACKUP_ROOT)
    parser.add_argument('--status-path', type=Path, default=STATUS_PATH)
    parser.add_argument('--force', action='store_true', help='Create a new verified point even within 24 hours (--run only).')
    arguments = parser.parse_args(argv)
    if arguments.force and not arguments.run:
        parser.error('--force requires --run')
    try:
        if arguments.plan:
            value = capacity_plan(arguments.root.resolve(), arguments.destination)
        else:
            if os.geteuid() != 0:
                raise PermissionError()
            value = run(arguments.root, arguments.destination, arguments.status_path,
                        group=grp.getgrnam('patternlab').gr_gid, force=arguments.force)
        print(json.dumps(value, ensure_ascii=False, allow_nan=False))
        return 1 if value.get('state') == 'error' else 0
    except Exception:
        print('{"state":"error","reason_code":"unavailable"}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
