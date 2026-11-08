"""Preserve optional context already received by the fixture importer.

This command performs no provider requests and no training. The source is
read-only; only the separate archive and its own status receipt are written.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from context_archive_sync import sync_archive

STATUS_VERSION = 'context-archive-status-v1'


def write_status(path, value):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Status symlinks are not permitted.')
    if path.exists():
        previous = json.loads(path.read_text())
        if previous.get('version') != STATUS_VERSION:
            raise ValueError('Refusing to replace an unrelated status file.')
    value = dict(value, version=STATUS_VERSION)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                dir=path.parent, prefix='.context-archive-', delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run(source, destination, *, batch_size=500):
    source, destination = Path(source).resolve(strict=True), Path(destination).absolute()
    if (destination.is_symlink() or source == destination.resolve()
            or destination.exists() and os.path.samefile(source, destination)):
        raise ValueError('Source and destination must be distinct, without a destination symlink.')
    destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    status = destination.with_suffix('.status.json')
    started = datetime.now(timezone.utc).isoformat()
    last_status = [0.]

    def progress(value):
        if shutil.disk_usage(destination.parent).free < 1024**3:
            raise OSError('Context archive paused to preserve one GiB of free disk space.')
        now = time.monotonic()
        if now - last_status[0] < 2:
            return
        write_status(status, dict(phase='running', started_at=started,
            updated_at=datetime.now(timezone.utc).isoformat(), progress=value,
            api_calls=0, model_training=False, source_database_writes=0))
        last_status[0] = now

    write_status(status, dict(phase='starting', started_at=started, updated_at=started,
        api_calls=0, model_training=False, source_database_writes=0))
    try:
        if shutil.disk_usage(destination.parent).free < 1024**3:
            raise OSError('Insufficient free disk space for context archive.')
        result = sync_archive(source, destination, batch_size=batch_size, progress=progress)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        # No payload or raw exception text is published.
        write_status(status, dict(phase='error', started_at=started,
            updated_at=datetime.now(timezone.utc).isoformat(), error_type=type(error).__name__,
            resumable=True, api_calls=0, model_training=False, source_database_writes=0))
        raise
    write_status(status, dict(phase='caught_up_to_starting_watermark', started_at=started,
        updated_at=datetime.now(timezone.utc).isoformat(), result=result,
        api_calls=0, model_training=False, source_database_writes=0))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--destination', required=True, type=Path)
    parser.add_argument('--batch-size', type=int, default=500,
        help='Memory/transaction size only: all rows through the initial watermark are processed.')
    options = parser.parse_args()
    try:
        result = run(options.source, options.destination, batch_size=options.batch_size)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        print(json.dumps(dict(state='error', error_type=type(error).__name__,
            message='Context archive refused or interrupted; inspect its status and rerun after correction.')))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
