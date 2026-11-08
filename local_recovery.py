"""Private, local recovery points. No application imports or network access.

SQLite files are backed up with SQLite's online backup API, including committed
WAL pages. Each database is consistent on its own; this is deliberately not a
claim of an atomic snapshot across independent databases and running workers.
Known private configuration paths are excluded. Arbitrary embedded credentials
inside otherwise public data/code are not detected by this filename policy.
"""
from datetime import datetime, timezone
from contextlib import closing
from hashlib import sha256
import json
import os
import re
from pathlib import Path
import shutil
import sqlite3
import tempfile
from time import monotonic


VERSION = 'local-recovery-v1'
DATABASE_SUFFIXES = {'.sqlite', '.sqlite3', '.db'}
CODE_DIRS = ('scripts', 'ui_pages', 'docs', 'assets')
PRIVATE_SUFFIXES = {'.pem', '.key', '.p12', '.pfx', '.jks', '.keystore', '.der'}
CODE_SUFFIXES = {'.py', '.md', '.command', '.sh'}
PRIVATE_WORD = re.compile(r'(^|[^a-z0-9])(secrets?|credentials?|tokens?|passwords?|passwd|'
                          r'api[_-]?key|access[_-]?key|private[_-]?key|keys?|id[_-]?(rsa|ed25519|ecdsa|dsa))'
                          r'([^a-z0-9]|$)')
SECURITY_SCOPE = dict(known_private_paths_excluded=True,
                      embedded_secret_content_audited=False,
                      security_scope='known_private_paths_only')


def fingerprint(path):
    digest = sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _public_path(path):
    # Fail closed for links and private configuration, even under data/.
    suffix = path.suffix.casefold()
    private_config = path.parts[0] == 'data' or suffix not in CODE_SUFFIXES
    return (not any(part.startswith('.') for part in path.parts[:-1])
            and (not path.name.startswith('.') or suffix in DATABASE_SUFFIXES)
            and suffix not in PRIVATE_SUFFIXES
            and not (private_config and any(PRIVATE_WORD.search(part.casefold()) for part in path.parts))
            and not path.name.endswith(('-wal', '-shm', '.lock')))


def source_files(root):
    root = Path(root).resolve()
    candidates = [path for path in root.iterdir()
                  if path.suffix in ('.py', '.md', '.txt', '.command', '.sh', '.toml')]
    candidates += [root / 'requirements.txt', root / '.streamlit' / 'config.toml']
    for directory in ('data', *CODE_DIRS):
        candidates.extend((root / directory).rglob('*'))
    selected = []
    for path in candidates:
        relative = path.relative_to(root)
        is_config = relative.as_posix() == '.streamlit/config.toml'
        if (path.is_file() and (is_config or _public_path(relative))
                and not any(parent.is_symlink() for parent in (path, *path.parents)
                            if parent != root and root in (parent, *parent.parents))
                and '__pycache__' not in relative.parts):
            selected.append(relative)
    return sorted(set(selected))


def _open_readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)


def _database_summary(path):
    with closing(_open_readonly(path)) as connection:
        integrity = [row[0] for row in connection.execute('PRAGMA quick_check')]
        if integrity != ['ok']:
            raise ValueError('Le contrôle d’intégrité SQLite a échoué.')
        names = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        counts = {name: connection.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                  for name in names}
        schema = list(connection.execute(
            'SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name'))
    return dict(quick_check='ok', tables=counts,
                schema_sha256=sha256(json.dumps(schema, ensure_ascii=False).encode()).hexdigest())


def _backup_database(source, destination, *, timeout=60):
    started = monotonic()

    def progress(_status, _remaining, _total):
        if monotonic() - started > timeout:
            raise TimeoutError('La sauvegarde SQLite dépasse son délai ; le point reste incomplet.')

    # Closing is explicit: a sqlite context manager only commits/rolls back.
    with closing(_open_readonly(source)) as src, closing(sqlite3.connect(destination)) as dst:
        src.backup(dst, pages=256, progress=progress, sleep=0.05)


def _manifest_items(manifest):
    """Validate the v1 contract before trusting counts or skipping SQLite QA."""
    if (not isinstance(manifest, dict) or manifest.get('version') != VERSION
            or manifest.get('state') != 'complete' or not isinstance(manifest.get('files'), list)):
        raise ValueError('Le point de reprise n’est pas complet ou son manifeste est invalide.')
    seen = set()
    for item in manifest['files']:
        if not isinstance(item, dict) or not isinstance(item.get('path'), str) or not item['path']:
            raise ValueError('Entrée du manifeste invalide.')
        relative = Path(item['path'])
        if (relative.is_absolute() or '..' in relative.parts or '\\' in item['path']
                or '\x00' in item['path'] or relative.as_posix() != item['path']
                or relative == Path('.') or item['path'] in seen):
            raise ValueError('Chemin de sauvegarde invalide ou répété.')
        seen.add(item['path'])
        kind = 'sqlite' if relative.suffix.casefold() in DATABASE_SUFFIXES else 'file'
        expected = {'path', 'kind', 'sha256', 'bytes'} | ({'database'} if kind == 'sqlite' else set())
        if (set(item) != expected or item.get('kind') != kind
                or type(item.get('bytes')) is not int or item['bytes'] < 0
                or not isinstance(item.get('sha256'), str)
                or re.fullmatch('[a-f0-9]{64}', item['sha256']) is None):
            raise ValueError('Type, taille ou empreinte du manifeste invalide.')
        if kind == 'sqlite':
            summary = item['database']
            if (not isinstance(summary, dict) or set(summary) != {'quick_check', 'tables', 'schema_sha256'}
                    or summary['quick_check'] != 'ok' or not isinstance(summary['tables'], dict)
                    or any(not isinstance(name, str) or not name or type(count) is not int or count < 0
                           for name, count in summary['tables'].items())
                    or not isinstance(summary['schema_sha256'], str)
                    or re.fullmatch('[a-f0-9]{64}', summary['schema_sha256']) is None):
                raise ValueError('Résumé SQLite du manifeste invalide.')
    return manifest['files']


def _verify_snapshot(folder, manifest):
    """Check one inventory, including a not-yet-published creation inventory."""
    items = _manifest_items(manifest)
    verified = []
    with tempfile.TemporaryDirectory(prefix='restore-check-', dir=folder) as temporary:
        for item in items:
            relative = Path(item['path'])
            path = folder / 'files' / relative
            if (relative.is_absolute() or '..' in relative.parts or path.is_symlink()
                    or not path.resolve().is_relative_to(folder / 'files')):
                raise ValueError('Chemin de sauvegarde invalide.')
            if not path.is_file() or path.stat().st_size != item['bytes'] or fingerprint(path) != item['sha256']:
                raise ValueError('L’empreinte d’un fichier sauvegardé ne correspond plus.')
            if item['kind'] == 'sqlite':
                # Exercise actual filesystem restoration, then open the restored
                # DB and compare schema and counts with the backed-up snapshot.
                restored = Path(temporary) / 'restored.sqlite3'
                shutil.copy2(path, restored)
                if fingerprint(restored) != item['sha256']:
                    raise ValueError('La copie restaurée ne correspond plus à la sauvegarde.')
                summary = _database_summary(restored)
                if summary != item['database']:
                    raise ValueError('La restauration SQLite n’est pas identique au point sauvegardé.')
                restored.unlink()
                verified.append(item['path'])
    known_private_excluded = all(_public_path(Path(item['path']))
                                 or item['path'] == '.streamlit/config.toml' for item in items)
    return dict(version=VERSION, verified_at=datetime.now(timezone.utc).isoformat(),
                files_verified=len(manifest['files']), databases_restored=verified,
                state='verified', atomic_across_databases=False,
                **dict(SECURITY_SCOPE, known_private_paths_excluded=known_private_excluded))


def verify_recovery_point(folder):
    """Restore copies in an isolated folder, never over application databases."""
    folder = Path(folder).resolve()
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    return _verify_snapshot(folder, manifest)


def _write_json_atomic(path, value):
    """Publish one JSON file only after its complete bytes have been flushed."""
    descriptor, temporary = tempfile.mkstemp(prefix='.recovery-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def create_recovery_point(root, destination):
    root, destination = Path(root).resolve(), Path(destination).resolve()
    if destination == root or any(destination.is_relative_to(root / folder)
                                  for folder in ('data', *CODE_DIRS)):
        raise ValueError('La sauvegarde doit utiliser un nouveau dossier séparé des données et du code actifs.')
    paths = source_files(root)
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    manifest = dict(version=VERSION, state='incomplete', started_at=datetime.now(timezone.utc).isoformat(),
                    atomic_across_databases=False, **SECURITY_SCOPE, files=[])
    manifest_path = destination / 'manifest.json'
    _write_json_atomic(manifest_path, manifest)
    for relative in paths:
        source, target = root / relative, destination / 'files' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        item = dict(path=relative.as_posix())
        if source.suffix.casefold() in DATABASE_SUFFIXES:
            _backup_database(source, target)
            item.update(kind='sqlite', database=_database_summary(target))
        else:
            before = fingerprint(source)
            shutil.copy2(source, target)
            if before != fingerprint(target) or before != fingerprint(source):
                raise ValueError('Un fichier a changé pendant sa copie ; relancer un nouveau point de reprise.')
            item['kind'] = 'file'
        item.update(sha256=fingerprint(target), bytes=target.stat().st_size)
        manifest['files'].append(item)
    receipt_path = destination / 'verification.json'
    try:
        # The public manifest stays incomplete throughout QA. A hard process
        # stop before the final publication cannot produce a complete point.
        receipt = _verify_snapshot(destination, dict(manifest, state='complete'))
        _write_json_atomic(receipt_path, receipt)
        manifest.update(state='complete', completed_at=datetime.now(timezone.utc).isoformat(),
                        verified_at=receipt['verified_at'])
        _write_json_atomic(manifest_path, manifest)
        return receipt
    except BaseException:
        manifest.update(state='incomplete')
        for key in ('completed_at', 'verified_at'):
            manifest.pop(key, None)
        try:
            _write_json_atomic(manifest_path, manifest)
        except OSError:
            pass
        try:
            receipt_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
