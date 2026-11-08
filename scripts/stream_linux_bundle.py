"""Verify an existing Linux stage, then stream its exact inventory as tar.gz.

No archive or restored database is written locally. Standard output contains
only archive bytes; count-only status and errors go to standard error. A failed
preflight emits no archive bytes. A change during streaming aborts the stream,
so callers must check this process's exit status before accepting the archive.
"""
import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import gzip
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tarfile


ROOT_MANIFESTS = ('manifest.json', 'verification.json', 'linux-bundle-manifest.json')
_DIGEST = re.compile(r'[a-f0-9]{64}')
_CHUNK = 1024 * 1024
_MAX_JSON = 16 * _CHUNK


@dataclass(frozen=True)
class Entry:
    name: str
    size: int
    digest: str
    metadata: tuple


@dataclass(frozen=True)
class BundlePlan:
    stage: Path
    directory_identity: tuple
    entries: tuple
    inventory_files: int
    inventory_bytes: int


def _relative(value):
    if (not isinstance(value, str) or not value or '\\' in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError('Invalid inventory path.')
    path = PurePosixPath(value)
    if (path.is_absolute() or '..' in path.parts or path == PurePosixPath('.')
            or path.as_posix() != value or re.match(r'^[A-Za-z]:', value)):
        raise ValueError('Invalid inventory path.')
    return path


@contextmanager
def _directory(stage):
    """Open each directory component without following a symbolic link."""
    stage = Path(stage).expanduser().absolute()
    if '..' in stage.parts:
        raise ValueError('Invalid stage directory.')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(stage.anchor, flags)
    try:
        for part in stage.parts[1:]:
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        yield stage, descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _regular_file(directory, name):
    parts = _relative(name).parts
    parent = os.dup(directory)
    descriptor = None
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=parent)
            os.close(parent)
            parent = child
        # NONBLOCK prevents a substituted FIFO from blocking before fstat.
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=parent)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError('Inventory source must be a regular file.')
        with os.fdopen(descriptor, 'rb') as stream:
            descriptor = None
            yield stream
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def _metadata(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode)


def _inspect(directory, name, *, keep_json=False):
    digest = sha256()
    chunks = []
    size = 0
    with _regular_file(directory, name) as source:
        before = _metadata(os.fstat(source.fileno()))
        for block in iter(lambda: source.read(_CHUNK), b''):
            size += len(block)
            if keep_json:
                if size > _MAX_JSON:
                    raise ValueError('Manifest is too large.')
                chunks.append(block)
            digest.update(block)
        if _metadata(os.fstat(source.fileno())) != before or size != before[3]:
            raise ValueError('Source changed during preflight.')
    entry = Entry(name, size, digest.hexdigest(), before)
    return entry, b''.join(chunks) if keep_json else None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Repeated JSON key.')
        result[key] = value
    return result


def _items(manifest, *, supplemental=False):
    version = ('patternlab-linux-local-staging-v1' if supplemental
               else 'local-recovery-v1')
    if (not isinstance(manifest, dict) or manifest.get('version') != version
            or manifest.get('state') != 'complete'
            or not isinstance(manifest.get('files'), list)):
        raise ValueError('Incomplete or invalid inventory.')
    for item in manifest['files']:
        if not isinstance(item, dict):
            raise ValueError('Invalid inventory entry.')
        _relative(item.get('path'))
        required = {'path', 'sha256', 'bytes'}
        if not supplemental:
            required.add('kind')
            if item.get('kind') == 'sqlite':
                required.add('database')
                if not isinstance(item.get('database'), dict):
                    raise ValueError('Invalid database inventory entry.')
            elif item.get('kind') != 'file':
                raise ValueError('Invalid inventory kind.')
        if (set(item) != required or type(item.get('bytes')) is not int
                or item['bytes'] < 0 or not isinstance(item.get('sha256'), str)
                or not _DIGEST.fullmatch(item['sha256'])):
            raise ValueError('Invalid inventory entry.')
    return manifest['files']


def _unchanged(directory, entry):
    with _regular_file(directory, entry.name) as source:
        if _metadata(os.fstat(source.fileno())) != entry.metadata:
            raise ValueError('Source metadata changed.')


def preflight(stage):
    """Hash every selected byte and validate both inventories without writing."""
    with _directory(stage) as (stage, directory):
        identity = _identity(os.fstat(directory))
        roots = []
        manifests = []
        for name in ROOT_MANIFESTS:
            entry, raw = _inspect(directory, name, keep_json=True)
            roots.append(entry)
            manifests.append(json.loads(raw, object_pairs_hook=_unique_object))
        base, verification, supplemental = manifests
        base_items = _items(base)
        extra_items = _items(supplemental, supplemental=True)
        if supplemental.get('recovery_manifest_sha256') != roots[0].digest:
            raise ValueError('Recovery manifest fingerprint mismatch.')
        if (not isinstance(verification, dict)
                or verification.get('version') != 'local-recovery-v1'
                or verification.get('state') != 'verified'
                or type(verification.get('files_verified')) is not int
                or verification['files_verified'] != len(base_items)):
            raise ValueError('Invalid verification receipt.')
        inventory = {}
        for item in (*base_items, *extra_items):
            name = item['path']
            if name in inventory:
                raise ValueError('Repeated inventory path.')
            inventory[name] = item
        total = sum(item['bytes'] for item in inventory.values())
        if type(supplemental.get('bytes')) is not int or supplemental['bytes'] != total:
            raise ValueError('Inventory byte count mismatch.')
        entries = list(roots)
        for name, expected in sorted(inventory.items()):
            entry, _ = _inspect(directory, 'files/' + name)
            if entry.size != expected['bytes'] or entry.digest != expected['sha256']:
                raise ValueError('Inventory fingerprint or size mismatch.')
            entries.append(entry)
        for entry in entries:
            _unchanged(directory, entry)
        return BundlePlan(stage, identity, tuple(entries), len(inventory), total)


class _HashReader:
    def __init__(self, source):
        self.source = source
        self.digest = sha256()
        self.size = 0

    def read(self, size):
        block = self.source.read(size)
        self.digest.update(block)
        self.size += len(block)
        return block


def write_archive(plan, output):
    """Stream a preflighted plan, checking identity and content while reading."""
    with _directory(plan.stage) as (_, directory):
        if _identity(os.fstat(directory)) != plan.directory_identity:
            raise ValueError('Stage directory changed.')
        # Finish all metadata checks before gzip writes its first header byte.
        for entry in plan.entries:
            _unchanged(directory, entry)
        with gzip.GzipFile(fileobj=output, mode='wb', filename='',
                           compresslevel=1, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w|', format=tarfile.PAX_FORMAT) as archive:
                for entry in plan.entries:
                    with _regular_file(directory, entry.name) as source:
                        before = os.fstat(source.fileno())
                        if _metadata(before) != entry.metadata:
                            raise ValueError('Source changed before streaming.')
                        member = tarfile.TarInfo(entry.name)
                        member.size = entry.size
                        member.mode = stat.S_IMODE(before.st_mode) & 0o777
                        member.mtime = before.st_mtime_ns // 1_000_000_000
                        reader = _HashReader(source)
                        archive.addfile(member, reader)
                        if (reader.size != entry.size or reader.digest.hexdigest() != entry.digest
                                or _metadata(os.fstat(source.fileno())) != entry.metadata):
                            raise ValueError('Source changed during streaming.')
                    _unchanged(directory, entry)
    output.flush()


def stream_bundle(stage, output, *, diagnostics=None, check_only=False):
    plan = preflight(stage)
    if diagnostics is not None:
        print(json.dumps(dict(state='preflight_verified', files=plan.inventory_files,
                              bytes=plan.inventory_bytes, root_manifests=len(ROOT_MANIFESTS))),
              file=diagnostics, flush=True)
    if not check_only:
        write_archive(plan, output)
        if diagnostics is not None:
            print(json.dumps(dict(state='stream_complete', files=plan.inventory_files,
                                  bytes=plan.inventory_bytes)), file=diagnostics, flush=True)
    return plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--check-only', action='store_true',
                        help='Hash and validate the inventory; emit no archive bytes.')
    options = parser.parse_args(argv)
    try:
        stream_bundle(options.stage, sys.stdout.buffer, diagnostics=sys.stderr,
                      check_only=options.check_only)
    except Exception:
        # Never expose filenames, JSON values, file contents or credentials.
        print(json.dumps(dict(state='error', error='Bundle verification or streaming failed.')),
              file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
