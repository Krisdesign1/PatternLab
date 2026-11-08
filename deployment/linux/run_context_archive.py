"""Integrity-checked offline context archive job; independent of serving/ML."""
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

ROOT = Path(__file__).resolve().parents[2]
VERSION = 'context-archive-execution-v1'
MANIFEST = 'context-archive-execution-manifest.json'
SOURCES = (
    'context_observation_store.py', 'context_archive_sync.py',
    'context_event_quality.py', 'prematch_context_quality.py', 'context_quality_audit.py',
    'scripts/sync_context_archive.py', 'deployment/linux/run_context_archive.py',
)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def safe_parent(root, path, *, require_root_owned=True):
    """Protect control directories as well as the leaf's own inode."""
    root, path = Path(root).absolute(), Path(path).absolute()
    if root not in path.parents:
        raise ValueError('Execution path is outside its protected root.')
    for parent in path.parents:
        info = parent.lstat()
        if (parent.is_symlink() or not stat.S_ISDIR(info.st_mode)
                or require_root_owned and (info.st_uid != 0 or info.st_mode & 0o022)):
            raise ValueError('Context archive execution directory is unsafe.')
        if parent == root:
            break


def verify(root=ROOT, *, require_root_owned=True):
    root = Path(root)
    for name in (MANIFEST, *SOURCES):
        path=root/name
        safe_parent(root, path, require_root_owned=require_root_owned)
        info=path.lstat()
        if (path.is_symlink() or not stat.S_ISREG(info.st_mode)
                or require_root_owned and (info.st_uid != 0 or info.st_mode & 0o022)):
            raise ValueError('Context archive execution control is unsafe.')
    manifest=json.loads((root/MANIFEST).read_text())
    if (manifest.get('version') != VERSION or manifest.get('tests_passed') is not True
            or set(manifest.get('source_sha256',{})) != set(SOURCES)
            or manifest['source_sha256'] != {name:digest(root/name) for name in SOURCES}):
        raise ValueError('Context archive execution does not match its qualification.')
    return manifest


def main():
    try:
        verify()
        sys.path.insert(0,str(ROOT))
        from scripts.sync_context_archive import run
        result=run(ROOT/'data/api_football/fixtures.sqlite3',
                   ROOT/'data/api_football/context_observations.sqlite3')
        print(json.dumps(dict(state='complete',execution_version=VERSION,result=result),ensure_ascii=False))
        return 0
    except Exception as error:
        print(json.dumps(dict(state='error',execution_version=VERSION,error_type=type(error).__name__,
            message='Context archive stopped; inspect its separate status. Main collector was not changed.')))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
