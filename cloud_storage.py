"""Optional durable workspace with fenced PostgreSQL checkpoints.

Local use is unchanged when no database URL is configured. Cloud mode fails
closed: a local success is returned only after the corresponding remote commit.
"""
from contextlib import contextmanager, closing
from contextvars import ContextVar
from functools import wraps
from hashlib import sha256
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile
import threading
import time
import tomllib
import uuid
import zlib

from cloud_postgres import BackendBusy, BackendError, PostgresBackend


MAX_STATE_BYTES = 96 * 1024 * 1024
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_RAW_STATE_BYTES = 1024 * 1024 * 1024
MAX_FILES = 10000
RESTORE_BATCH_BYTES = 4 * 1024 * 1024
RESTORE_BATCH_FILES = 32
_PROJECT = Path(__file__).resolve().parent
_data_root = _PROJECT / 'data'
_backend_factory = PostgresBackend
_current = ContextVar('patternlab_cloud_transaction', default=None)
_local_lock = threading.RLock()
_file_cache = {}


class CloudStorageError(RuntimeError):
    pass


class CloudStorageBusy(CloudStorageError):
    pass


@contextmanager
def _process_lock():
    if not _local_lock.acquire(blocking=False):
        raise CloudStorageBusy('Un autre traitement local utilise les données.')
    try:
        yield
    finally:
        _local_lock.release()


def _options():
    values = {}
    path = _PROJECT / '.streamlit' / 'secrets.toml'
    if path.exists():
        try:
            values = tomllib.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise CloudStorageError('Les secrets du stockage distant sont illisibles.') from None
    return {key: os.environ.get(key, values.get(key)) for key in (
        'PATTERNLAB_DATABASE_URL', 'PATTERNLAB_WORKSPACE', 'PATTERNLAB_CLOUD_ADOPT_LOCAL',
        'PATTERNLAB_CLOUD_ALLOW_INSECURE_LOCALHOST_TEST', 'PATTERNLAB_CLOUD_REQUIRED')}


def configured():
    options = _options()
    return bool(options.get('PATTERNLAB_DATABASE_URL')) or str(options.get('PATTERNLAB_CLOUD_REQUIRED')).lower() == 'true'


def _backend():
    options = _options()
    try:
        return _backend_factory(options['PATTERNLAB_DATABASE_URL'], options['PATTERNLAB_WORKSPACE'] or 'patternlab-main',
                                allow_localhost=str(options['PATTERNLAB_CLOUD_ALLOW_INSECURE_LOCALHOST_TEST']).lower() == 'true')
    except BackendError as error:
        raise CloudStorageError(str(error)) from None


def _safe_path(name):
    if not isinstance(name, str) or not name or '\\' in name or '\x00' in name:
        raise CloudStorageError('Le point de reprise contient un chemin invalide.')
    value = PurePosixPath(name)
    if value.is_absolute() or str(value) != name or any(p in ('.', '..') or p.startswith('.') for p in value.parts):
        raise CloudStorageError('Le point de reprise contient un chemin invalide.')
    if any(p.endswith(('.lock', '.log', '.plist', '.tmp', '.temp', '-wal', '-shm', '-journal')) for p in value.parts):
        raise CloudStorageError('Le point de reprise contient un fichier exclu.')
    # These are the only application-owned locations, never secrets or code.
    if value.parts[0] not in ('api_football', 'models', 'workspace_settings.json'):
        raise CloudStorageError('Le point de reprise sort des données autorisées.')
    if value.parts[0] == 'workspace_settings.json' and len(value.parts) != 1:
        raise CloudStorageError('Le chemin des préférences est invalide.')
    return value


def _tracked(root):
    if not root.exists():
        return []
    result = []
    for path in root.rglob('*'):
        if path.is_symlink():
            raise CloudStorageError('Un lien symbolique empêche la sauvegarde sûre des données.')
        if not path.is_file():
            continue
        name = path.relative_to(root).as_posix()
        try:
            _safe_path(name)
        except CloudStorageError:
            continue
        result.append((name, path))
    return sorted(result)


def _manifest(value):
    if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('files'), dict):
        raise CloudStorageError('Le manifeste distant est invalide ; restauration annulée.')
    entries = value['files']
    if len(entries) > MAX_FILES:
        raise CloudStorageError('Le point de reprise contient trop de fichiers.')
    compressed_total, raw_total = 0, 0
    for name, entry in entries.items():
        _safe_path(name)
        if (not isinstance(entry, dict) or set(entry) != {'sha256', 'size', 'compressed_size'}
                or not isinstance(entry['sha256'], str) or len(entry['sha256']) != 64
                or any(c not in '0123456789abcdef' for c in entry['sha256'])
                or type(entry['size']) is not int or not 0 <= entry['size'] <= MAX_FILE_BYTES
                or type(entry['compressed_size']) is not int or not 0 < entry['compressed_size'] <= MAX_STATE_BYTES):
            raise CloudStorageError('Le manifeste distant contient un fichier invalide.')
        compressed_total += entry['compressed_size']
        raw_total += entry['size']
    if compressed_total > MAX_STATE_BYTES:
        raise CloudStorageError('Les données compressées dépassent la limite de 96 Mio ; aucune publication effectuée.')
    if raw_total > MAX_RAW_STATE_BYTES:
        raise CloudStorageError('Les données dépassent la limite de restauration de 1 Gio.')
    # File/directory collisions must be rejected before touching the workspace.
    names = set(entries)
    if any(str(parent) in names for name in names for parent in PurePosixPath(name).parents if str(parent) != '.'):
        raise CloudStorageError('Le manifeste distant contient des chemins incompatibles.')
    return value


def _read_marker(root):
    try:
        return json.loads((root / '.cloud-state.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, prefix='.cloud-', delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _marker(session, *, dirty):
    _write_json(session.root / '.cloud-state.json', dict(version=1, workspace=session.backend.workspace,
                 database=getattr(session.backend, 'identity', session.backend.workspace),
                 revision=session.revision, manifest=session.manifest, dirty=dirty))


def _signature(path):
    info = path.stat()
    wal = path.with_name(path.name + '-wal')
    wal_info = wal.stat() if wal.exists() else None
    return (info.st_ino, info.st_size, info.st_mtime_ns,
            None if wal_info is None else (wal_info.st_ino, wal_info.st_size, wal_info.st_mtime_ns))


def _encode_file(path):
    if path.stat().st_size > MAX_FILE_BYTES:
        raise CloudStorageError('Un fichier dépasse la limite locale de 512 Mio.')
    if path.suffix == '.sqlite3':
        with tempfile.TemporaryDirectory(prefix='patternlab-sqlite-') as directory:
            target = Path(directory) / 'snapshot.sqlite3'
            try:
                with closing(sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True, timeout=10)) as source:
                    with closing(sqlite3.connect(target)) as dest:
                        source.backup(dest)
                        if dest.execute('PRAGMA quick_check').fetchone() != ('ok',):
                            raise CloudStorageError('Le journal SQLite est invalide ; sauvegarde annulée.')
                raw = target.read_bytes()
            except sqlite3.Error:
                raise CloudStorageError('Le journal SQLite ne peut pas être sauvegardé de façon cohérente.') from None
    else:
        raw = path.read_bytes()
    if len(raw) > MAX_FILE_BYTES:
        raise CloudStorageError('Un fichier dépasse la limite locale de 512 Mio.')
    encoded = zlib.compress(raw, 6)
    return dict(sha256=sha256(raw).hexdigest(), size=len(raw), compressed_size=len(encoded)), encoded


def _snapshot(root):
    files, contents = {}, {}
    for name, path in _tracked(root):
        signature = _signature(path)
        key = str(path)
        cached = _file_cache.get(key)
        if cached is not None and cached[0] == signature:
            entry, encoded = cached[1:]
        else:
            entry, encoded = _encode_file(path)
            if _signature(path) != signature:
                raise CloudStorageError('Les données ont changé pendant la sauvegarde ; publication annulée.')
            _file_cache[key] = signature, entry, encoded
        files[name], contents[entry['sha256']] = entry, encoded
    manifest = _manifest(dict(version=1, files=files))
    return manifest, contents


def _decode(content, entry):
    if len(content) != entry['compressed_size']:
        raise CloudStorageError('Un fichier distant est incomplet ; restauration annulée.')
    try:
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(content, entry['size'] + 1)
        if (len(raw) != entry['size'] or not decompressor.eof or decompressor.unused_data
                or decompressor.unconsumed_tail or sha256(raw).hexdigest() != entry['sha256']):
            raise ValueError()
    except (zlib.error, ValueError):
        raise CloudStorageError('L’intégrité d’un fichier distant n’est pas vérifiée ; restauration annulée.') from None
    return raw


def _verify_destination(root, name):
    destination = root.joinpath(*_safe_path(name).parts)
    if root.is_symlink() or any(parent.is_symlink() for parent in (destination, *destination.parents) if parent == root or root in parent.parents):
        raise CloudStorageError('Un lien symbolique empêche la restauration sûre des données.')
    return destination


def _restore_batches(entries):
    """Bound each download by compressed bytes and distinct content objects."""
    batch, compressed_size = {}, 0
    for digest, entry in entries.items():
        if batch and (len(batch) >= RESTORE_BATCH_FILES
                      or compressed_size + entry['compressed_size'] > RESTORE_BATCH_BYTES):
            yield batch
            batch, compressed_size = {}, 0
        batch[digest] = entry
        compressed_size += entry['compressed_size']
    if batch:
        yield batch


def _read_restore_batch(session, entries):
    session.check()
    bulk_reader = getattr(session.backend, 'read_blobs', None)
    if callable(bulk_reader):
        contents = bulk_reader(list(entries))
    else:
        # Existing local/test backends keep their single-object interface.
        contents = {}
        for digest in entries:
            session.check()
            contents[digest] = session.backend.read_blob(digest)
    session.check()
    if not isinstance(contents, dict) or set(contents) != set(entries):
        raise CloudStorageError('Les fichiers du point de reprise distant sont incomplets ou inattendus.')
    return contents


def _restore(session, old_marker):
    manifest = _manifest(session.manifest)
    current = dict(_tracked(session.root))
    same_workspace = (old_marker.get('version') == 1 and old_marker.get('workspace') == session.backend.workspace
                      and old_marker.get('database') == getattr(session.backend, 'identity', session.backend.workspace))
    if not same_workspace and current:
        if str(_options()['PATTERNLAB_CLOUD_ADOPT_LOCAL']).lower() != 'true':
            raise CloudStorageError('Des données locales existent déjà. Exportez-les ou activez explicitement PATTERNLAB_CLOUD_ADOPT_LOCAL=true pour sauvegarder puis adopter les données distantes.')
        backup = session.root / '.cloud-backups' / uuid.uuid4().hex
        for name, path in current.items():
            target = backup / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    needed, objects = [], {}
    for name, entry in manifest['files'].items():
        destination = _verify_destination(session.root, name)
        existing = current.get(name)
        if existing is not None:
            try:
                signature = _signature(existing)
                cached = _file_cache.get(str(existing))
                if cached is not None and cached[0] == signature:
                    local_entry = cached[1]
                else:
                    local_entry, encoded_local = _encode_file(existing)
                    _file_cache[str(existing)] = signature, local_entry, encoded_local
                if local_entry == entry:
                    continue
            except (OSError, CloudStorageError):
                # A corrupt local cache is replaced only after validating
                # the entire committed remote snapshot below.
                pass
        digest = entry['sha256']
        if digest in objects and objects[digest] != entry:
            raise CloudStorageError('Le manifeste distant contient des tailles de fichier incohérentes.')
        objects[digest] = entry
        needed.append((name, destination, digest))
    # Stage and validate ALL incoming content before changing ANY tracked file.
    # Duplicate destinations share one verified staged object and one download.
    with tempfile.TemporaryDirectory(prefix='patternlab-restore-') as directory:
        staging = Path(directory)
        sqlite_digests = {digest for name, _, digest in needed if Path(name).suffix == '.sqlite3'}
        for batch in _restore_batches(objects):
            contents = _read_restore_batch(session, batch)
            for digest, entry in batch.items():
                encoded, raw_size = contents[digest]
                if raw_size != entry['size']:
                    raise CloudStorageError('La taille d’un fichier distant est incohérente.')
                raw = _decode(encoded, entry)
                target = staging / digest
                target.write_bytes(raw)
                if digest in sqlite_digests:
                    try:
                        with closing(sqlite3.connect(f'file:{target.as_posix()}?mode=ro', uri=True)) as con:
                            if con.execute('PRAGMA quick_check').fetchone() != ('ok',):
                                raise CloudStorageError('Le journal distant est invalide.')
                    except sqlite3.Error:
                        raise CloudStorageError('Le journal distant est invalide.') from None
        changed = [(name, destination, staging / digest) for name, destination, digest in needed]
        for name in current:
            _verify_destination(session.root, name)
        session.check()
        _marker(session, dirty=True)
        try:
            for name, destination, target in changed:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile('wb', dir=destination.parent, prefix='.cloud-restore-', delete=False) as handle:
                    temporary = Path(handle.name)
                    with target.open('rb') as source:
                        shutil.copyfileobj(source, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
                for suffix in ('-wal', '-shm', '-journal'):
                    destination.with_name(destination.name + suffix).unlink(missing_ok=True)
            for name, path in current.items():
                if name not in manifest['files']:
                    path.unlink()
        finally:
            for _, destination, _ in changed:
                _file_cache.pop(str(destination), None)
        _marker(session, dirty=False)


class _Session:
    def __init__(self, backend, root, lease, revision, manifest):
        self.backend, self.root, self.lease = backend, root, lease
        self.revision, self.manifest = revision, manifest
        self.failed = None
        self.stopped = threading.Event()
        self.heartbeat = threading.Thread(target=self._heartbeat, name='patternlab-storage-lease', daemon=True)

    def _heartbeat(self):
        while not self.stopped.wait(30):
            try:
                self.backend.renew(self.lease)
            except Exception:
                self.failed = 'Le verrou distant ne peut plus être renouvelé ; traitement suspendu.'
                return

    def check(self):
        if self.failed:
            raise CloudStorageError(self.failed)
        try:
            self.backend.assert_lease(self.lease)
        except BackendError as error:
            self.failed = str(error)
            raise CloudStorageError(str(error)) from None


def assert_lease():
    session = _current.get()
    if session is not None:
        session.check()
    elif configured():
        raise CloudStorageError('Une transaction distante est nécessaire avant toute écriture ou requête API.')


def checkpoint():
    session = _current.get()
    if session is None:
        if configured():
            raise CloudStorageError('Aucune transaction distante active pour sauvegarder les données.')
        return None
    session.check()
    try:
        manifest, contents = _snapshot(session.root)
        if manifest != session.manifest:
            old = {entry['sha256'] for entry in session.manifest.get('files', {}).values()}
            for entry in manifest['files'].values():
                if entry['sha256'] not in old:
                    session.check()
                    session.backend.upload(session.lease, entry['sha256'], contents[entry['sha256']], entry['size'])
                    old.add(entry['sha256'])
            session.check()
            session.revision = session.backend.publish(session.lease, session.revision, manifest)
            session.manifest = manifest
        _marker(session, dirty=False)
        return session.revision
    except BackendError as error:
        session.failed = str(error)
        raise CloudStorageError(str(error)) from None


@contextmanager
def transaction():
    active = _current.get()
    if active is not None:
        active.check()
        yield active
        return
    if not configured():
        yield None
        return
    root = Path(_data_root)
    root.mkdir(parents=True, exist_ok=True)
    with _process_lock():
        with (root / '.cloud-operation.lock').open('a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CloudStorageBusy('Un autre traitement local utilise les données.') from None
            session, token = None, None
            try:
                backend = _backend()
                lease, revision, manifest = backend.acquire(uuid.uuid4().hex)
                session = _Session(backend, root, lease, revision, manifest)
                session.heartbeat.start()
                if revision == 0:
                    raise CloudStorageError('Le stockage distant est vide. Initialisez-le explicitement avec la commande seed avant de démarrer le Cloud.')
                _restore(session, _read_marker(root))
                _marker(session, dirty=True)
                token = _current.set(session)
                yield session
                checkpoint()
            except BackendBusy as error:
                raise CloudStorageBusy(str(error)) from None
            except BackendError as error:
                raise CloudStorageError(str(error)) from None
            except BaseException:
                if session is not None and token is not None:
                    # No partial operation is published on failure. The next
                    # operation must restore the last successfully committed head.
                    _marker(session, dirty=True)
                    _file_cache.clear()
                raise
            finally:
                if token is not None:
                    _current.reset(token)
                if session is not None:
                    session.stopped.set()
                    session.heartbeat.join(timeout=11)
                    try:
                        session.backend.release(session.lease)
                    except BackendError:
                        pass  # Expiration still fences this owner; do not mask a durable success.
                fcntl.flock(handle, fcntl.LOCK_UN)


def persisted(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with transaction():
            session = _current.get()
            if session is not None:
                _marker(session, dirty=True)
            try:
                result = function(*args, **kwargs)
                checkpoint()
                return result
            except BaseException:
                if session is not None:
                    # Expected validation or provider errors can be handled by
                    # the caller when they changed no durable state. Partial
                    # local writes must instead poison the enclosing operation.
                    changed = True
                    if session.failed is None:
                        try:
                            changed = _snapshot(session.root)[0] != session.manifest
                        except Exception:
                            pass
                    if changed or session.failed is not None:
                        session.failed = 'Une opération n’a pas abouti ; les modifications non confirmées ne seront pas publiées.'
                        _marker(session, dirty=True)
                        _file_cache.clear()
                    else:
                        _marker(session, dirty=False)
                raise
    return wrapped


def seed():
    """Explicitly initialize an EMPTY remote workspace from current local data."""
    if not configured():
        raise CloudStorageError('Configurez PATTERNLAB_DATABASE_URL avant l’initialisation distante.')
    if _current.get() is not None:
        raise CloudStorageError('L’initialisation ne peut pas être imbriquée dans une autre opération.')
    root = Path(_data_root)
    root.mkdir(parents=True, exist_ok=True)
    with _process_lock(), (root / '.cloud-operation.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CloudStorageBusy('Un autre traitement local utilise les données.') from None
        session, token = None, None
        try:
            backend = _backend()
            lease, revision, manifest = backend.acquire(uuid.uuid4().hex)
            session = _Session(backend, root, lease, revision, manifest)
            if revision != 0:
                raise CloudStorageError('Le stockage distant contient déjà des données ; initialisation refusée.')
            snapshot, contents = _snapshot(root)
            if not snapshot['files']:
                raise CloudStorageError('Aucune donnée locale exploitable à initialiser.')
            session.heartbeat.start()
            token = _current.set(session)
            checkpoint()
            return status()
        except BackendBusy as error:
            raise CloudStorageBusy(str(error)) from None
        except BackendError as error:
            raise CloudStorageError(str(error)) from None
        finally:
            if token is not None:
                _current.reset(token)
            if session is not None:
                session.stopped.set()
                if session.heartbeat.ident is not None:
                    session.heartbeat.join(timeout=11)
                try:
                    session.backend.release(session.lease)
                except BackendError:
                    pass
            fcntl.flock(handle, fcntl.LOCK_UN)


def status():
    if not configured():
        return dict(configured=False, initialized=False, revision=0, files=0, bytes=0, compressed_bytes=0)
    session = _current.get()
    try:
        updated_at = None
        if session is not None:
            manifest, revision, workspace = session.manifest, session.revision, session.backend.workspace
        else:
            backend = _backend()
            remote = backend.status()
            manifest, revision, workspace = remote['manifest'], remote['revision'], backend.workspace
            updated_at = remote.get('updated_at')
        if revision:
            _manifest(manifest)
        entries = manifest.get('files', {})
        return dict(configured=True, workspace=workspace, initialized=revision > 0, revision=revision,
                    files=len(entries), file_count=len(entries), updated_at=updated_at,
                    bytes=sum(e['size'] for e in entries.values()),
                    compressed_bytes=sum(e['compressed_size'] for e in entries.values()))
    except BackendError as error:
        raise CloudStorageError(str(error)) from None
