#!/usr/bin/env python3
"""Versioned collector entry point with scoped local reads and explicit provenance.

The guarded season intake, service lock, request accounting and scheduling remain
in their existing implementations. This is a new orchestration implementation;
unchanged registered dependency hashes are not presented as proof that the old
orchestrator was executed. No model training or promotion is enabled here.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import uuid

PROJECT_ROOT = Path(__file__).resolve().parents[2]
VERSION = 'worker-scoped-io-v1'
MANIFEST = 'worker-scoped-io-manifest.json'
SOURCES = ('automation_worker_scoped.py', 'worker_fixture_io.py',
           'candidate_tracking_io.py', 'daily_analysis_worker_io.py',
           'deployment/linux/run_scoped_worker.py')


def _digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify_execution(root=PROJECT_ROOT, *, require_root_owned=True):
    """Refuse missing, changed or writable deployment sources before any cycle."""
    root = Path(root)
    path = root/MANIFEST
    controls = [path, *(root/name for name in SOURCES)]
    for control in controls:
        info = control.lstat()
        if (not stat.S_ISREG(info.st_mode) or
                require_root_owned and (info.st_uid != 0 or info.st_mode & 0o022)):
            raise ValueError('Collector execution control is invalid.')
    manifest = json.loads(path.read_text())
    if (manifest.get('version') != VERSION or
            not isinstance(manifest.get('qualification'), dict) or
            manifest['qualification'].get('tests_passed') is not True or
            set(manifest.get('source_sha256', {})) != set(SOURCES)):
        raise ValueError('Collector execution manifest is invalid.')
    observed = {name:_digest(root/name) for name in SOURCES}
    if observed != manifest['source_sha256']:
        raise ValueError('Collector execution sources differ from their qualification.')
    # These are retained dependencies. They are checked separately from the
    # actual executed entry point, never substituted for its identity.
    dependencies = manifest.get('preserved_dependency_sha256')
    if not isinstance(dependencies, dict) or 'automation_worker.py' not in dependencies:
        raise ValueError('Collector preserved dependencies are missing.')
    for name, expected in dependencies.items():
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts or (root/relative).is_symlink():
            raise ValueError('Collector dependency path is invalid.')
        if _digest(root/relative) != expected:
            raise ValueError('A preserved collector dependency changed.')
    return dict(version=VERSION, entrypoint='deployment/linux/run_scoped_worker.py',
        source_sha256=observed, manifest_sha256=_digest(path),
        preserved_dependency_sha256=dependencies,
        actual_orchestrator_matches_registered=False,
        qualification=manifest['qualification'])


def _receipt(root, execution):
    directory = Path(root)/'data/api_football/worker_executions'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    identity = uuid.uuid4().hex
    value = dict(execution, id=identity,
                 started_at=datetime.now(timezone.utc).isoformat())
    path = directory/(identity+'.json')
    with path.open('x', encoding='utf-8') as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    return value


def run_once(root=PROJECT_ROOT, *, service=None, provider=None,
             max_duration_seconds=None, execution=None):
    sys.path.insert(0, str(root))
    from deployment.linux import run_guarded_worker as guarded
    import automation_worker_scoped as worker
    execution = verify_execution(root) if execution is None else execution
    receipt = _receipt(root, execution)
    # Use the existing injectable worker interface; no global module patching.
    class Boundary:
        @staticmethod
        def run_cycle(**arguments):
            return worker.run_cycle(**arguments, execution_io=receipt)

    result = guarded.run_once(root, service=service, worker=Boundary,
        provider=provider, max_duration_seconds=max_duration_seconds)
    result['execution_io'] = dict(version=VERSION, receipt_id=receipt['id'],
        manifest_sha256=receipt['manifest_sha256'],
        actual_orchestrator_matches_registered=False)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true', required=True)
    parser.add_argument('--max-duration-seconds', type=float, default=None,
                        help='Optional qualification window, not a collection cap.')
    options = parser.parse_args(argv)
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from deployment.linux.preflight import check
        check(PROJECT_ROOT, worker=True)
        result = run_once(max_duration_seconds=options.max_duration_seconds)
    except Exception:
        print('{"status":"error","error":"Scoped collector refused; inspect execution controls and service status."}')
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return int(result['status'] == 'error')


if __name__ == '__main__':
    raise SystemExit(main())
