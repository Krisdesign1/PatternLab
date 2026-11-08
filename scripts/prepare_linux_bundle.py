"""Prepare or verify a private LOCAL Linux staging point; never transfer it.

The existing recovery implementation snapshots current code/data (including
untracked files) and uses SQLite's online backup API for committed WAL pages.
Public audit evidence and the Linux deployment kit are added separately.
Filename exclusions do not inspect file contents for embedded credentials.
No installed Mac packages, Python environment, service, API call or training
are started by this script. Databases remain individually consistent, not a
single transaction across running application writers.
"""
from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import local_recovery as recovery


VERSION = 'patternlab-linux-local-staging-v1'
MANIFEST_NAME = 'linux-bundle-manifest.json'
PUBLIC_SUFFIXES = frozenset({'.json', '.md', '.py', '.sh', '.toml', '.service',
    '.timer', '.template', '.example', '.txt', '.constraints'})
# Exact public template/version metadata and the two archived delivery test logs.
# Other logs and files with private names stay excluded.
PUBLIC_EXCEPTIONS = frozenset({'deployment/linux/secrets.toml.example',
    'deployment/linux/.python-version',
    'audit/PATTERNLAB2_LOT11_SOURCES_2026-11-02/tests.log',
    'audit/PATTERNLAB2_LOT12_SOURCES_2026-11-02/tests.log'})
_CACHE_OR_LOG = re.compile(r'(^|[^a-z0-9])(caches?|logs?|tmp|temp)([^a-z0-9]|$)')
_DIGEST = re.compile('[a-f0-9]{64}')


def _relative(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise ValueError('Chemin de staging invalide.')
    path = Path(value)
    if (path.is_absolute() or '..' in path.parts or path == Path('.')
            or path.as_posix() != value):
        raise ValueError('Chemin de staging invalide.')
    return path


def _public_parts(parts):
    return all(not part.startswith('.') and part != '__pycache__'
               and not recovery.PRIVATE_WORD.search(part.casefold())
               and not _CACHE_OR_LOG.search(part.casefold()) for part in parts)


def _public_extra(relative):
    name = relative.as_posix()
    permitted_root = ((len(relative.parts) > 1 and relative.parts[0] == 'audit')
        or (len(relative.parts) > 2 and relative.parts[:2] == ('deployment', 'linux')))
    if not permitted_root:
        return False
    if name in PUBLIC_EXCEPTIONS:
        return True
    return relative.suffix.casefold() in PUBLIC_SUFFIXES and _public_parts(relative.parts)


def _safe_path(base, relative):
    """Reject links in every component, including the inventory's files root."""
    if base.is_symlink() or not base.is_dir():
        raise ValueError('Répertoire de staging invalide ou lié.')
    current = base
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('Un lien ne peut pas figurer dans le staging.')
    if not current.resolve().is_relative_to(base.resolve()):
        raise ValueError('Un chemin sort du staging.')
    return current


def supplemental_sources(project):
    """Select allowlisted regular files without following any source links."""
    selected = []
    for folder in (Path('audit'), Path('deployment/linux')):
        directory = project / folder
        if not directory.is_dir() or directory.is_symlink():
            continue
        try:
            _safe_path(project, folder)
        except ValueError:
            continue
        for parent, directories, names in os.walk(directory, followlinks=False):
            parent = Path(parent)
            directories[:] = sorted(name for name in directories
                if _public_parts((name,)) and not (parent / name).is_symlink())
            for name in sorted(names):
                relative = (parent / name).relative_to(project)
                if not _public_extra(relative) or (parent / name).is_symlink():
                    continue
                source = _safe_path(project, relative)
                if source.is_file():
                    selected.append(relative)
    return sorted(selected)


def _supplemental_items(manifest):
    if (not isinstance(manifest, dict) or manifest.get('version') != VERSION
            or manifest.get('state') != 'complete'
            or not isinstance(manifest.get('files'), list)
            or not isinstance(manifest.get('recovery_manifest_sha256'), str)
            or _DIGEST.fullmatch(manifest['recovery_manifest_sha256']) is None):
        raise ValueError('Le manifeste de staging est incomplet ou invalide.')
    seen = set()
    for item in manifest['files']:
        if not isinstance(item, dict) or set(item) != {'path', 'bytes', 'sha256'}:
            raise ValueError('Inventaire complémentaire invalide.')
        relative = _relative(item['path'])
        if (not _public_extra(relative) or item['path'] in seen
                or type(item['bytes']) is not int or item['bytes'] < 0
                or not isinstance(item['sha256'], str)
                or _DIGEST.fullmatch(item['sha256']) is None):
            raise ValueError('Entrée complémentaire privée, répétée ou invalide.')
        seen.add(item['path'])
    return manifest['files']


def _verify_supplemental(destination, manifest):
    items = _supplemental_items(manifest)
    index = _safe_path(destination, Path('manifest.json'))
    if recovery.fingerprint(index) != manifest['recovery_manifest_sha256']:
        raise ValueError('Le manifeste de reprise a changé.')
    files_root = _safe_path(destination, Path('files'))
    for item in items:
        path = _safe_path(files_root, _relative(item['path']))
        if (not path.is_file() or path.stat().st_size != item['bytes']
                or recovery.fingerprint(path) != item['sha256']):
            raise ValueError('Un fichier complémentaire a changé.')
    return len(items)


def _receipt(destination, manifest, recovery_report):
    return dict(version=VERSION, state='verified_local_staging',
        destination=str(destination), manifest_sha256=recovery.fingerprint(destination / MANIFEST_NAME),
        recovery_files_verified=recovery_report['files_verified'],
        databases_restored=recovery_report['databases_restored'],
        supplemental_files_verified=len(manifest['files']),
        bytes=manifest['bytes'], atomic_across_databases=False,
        known_private_paths_excluded=True, embedded_secret_content_audited=False,
        mac_environment_copied=False, network_used=False, upload_performed=False,
        content_review_required_before_transfer=True)


def verify_linux_bundle(destination):
    """Independently verify both inventories; restore DB copies only in staging."""
    destination = Path(destination).expanduser().absolute()
    index = _safe_path(destination, Path(MANIFEST_NAME))
    manifest = json.loads(index.read_text(encoding='utf-8'))
    _verify_supplemental(destination, manifest)
    report = recovery.verify_recovery_point(destination)
    # Detect an inventory replacement while the larger recovery check ran.
    _verify_supplemental(destination, manifest)
    if json.loads(index.read_text(encoding='utf-8')) != manifest:
        raise ValueError('Le manifeste de staging a changé.')
    return _receipt(destination, manifest, report)


def prepare_linux_bundle(project, destination):
    """Create a new point; do not overwrite an existing stage or active data."""
    project = Path(project).expanduser().absolute()
    if project.is_symlink() or not project.is_dir():
        raise ValueError('Le projet doit être un répertoire réel existant.')
    project = project.resolve()
    supplied = Path(destination).expanduser().absolute()
    if supplied.exists() or supplied.is_symlink():
        raise FileExistsError('Le dossier de staging doit être neuf.')
    if supplied.is_relative_to(project):
        current = project
        for part in supplied.relative_to(project).parts:
            current = current / part
            if current.is_symlink():
                raise ValueError('Le dossier de staging ne doit pas traverser un lien.')
    destination = supplied.resolve()
    if destination.is_relative_to(project):
        relative = destination.relative_to(project)
        if len(relative.parts) < 2 or relative.parts[0] != '.local_backups':
            raise ValueError('Dans le projet, utiliser un nouveau dossier sous .local_backups.')
    kit = _safe_path(project, Path('deployment/linux'))
    if not kit.is_dir():
        raise ValueError('Le kit deployment/linux doit être préparé avant le staging.')
    extras = supplemental_sources(project)
    if not any(path.parts[:2] == ('deployment', 'linux') for path in extras):
        raise ValueError('Le kit Linux public est vide.')
    # This is the sole code/data/SQLite snapshot implementation. It performs
    # its own isolated DB restoration and publishes only a verified base point.
    report = recovery.create_recovery_point(project, destination)
    base = json.loads((destination / 'manifest.json').read_text(encoding='utf-8'))
    manifest = dict(version=VERSION, state='incomplete',
        started_at=datetime.now(timezone.utc).isoformat(),
        recovery_manifest_sha256=recovery.fingerprint(destination / 'manifest.json'),
        files=[], bytes=sum(item['bytes'] for item in recovery._manifest_items(base)),
        supplemental_public_suffixes=sorted(PUBLIC_SUFFIXES),
        supplemental_exact_public_exceptions=sorted(PUBLIC_EXCEPTIONS),
        atomic_across_databases=False, **recovery.SECURITY_SCOPE,
        purpose='local_staging_only', mac_environment_copied=False,
        network_used=False, upload_performed=False,
        content_review_required_before_transfer=True)
    index = destination / MANIFEST_NAME
    recovery._write_json_atomic(index, manifest)
    try:
        for relative in extras:
            source = _safe_path(project, relative)
            target = destination / 'files' / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            before = recovery.fingerprint(source)
            shutil.copy2(source, target)
            if (source.is_symlink() or before != recovery.fingerprint(target)
                    or before != recovery.fingerprint(_safe_path(project, relative))):
                raise ValueError('Un fichier public a changé pendant sa copie.')
            item = dict(path=relative.as_posix(), bytes=target.stat().st_size, sha256=before)
            manifest['files'].append(item)
            manifest['bytes'] += item['bytes']
        # The base was already independently restored by create_recovery_point.
        # Verify all added bytes before publishing the overall completion state.
        _verify_supplemental(destination, dict(manifest, state='complete'))
        manifest.update(state='complete', completed_at=datetime.now(timezone.utc).isoformat())
        recovery._write_json_atomic(index, manifest)
        return _receipt(destination, manifest, report)
    except BaseException:
        manifest['state'] = 'incomplete'
        manifest.pop('completed_at', None)
        recovery._write_json_atomic(index, manifest)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=ROOT)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--destination', type=Path, help='Nouveau dossier privé de staging local.')
    action.add_argument('--verify', type=Path, help='Vérifier un staging existant sans toucher aux sources.')
    options = parser.parse_args(argv)
    try:
        report = (verify_linux_bundle(options.verify) if options.verify else
                  prepare_linux_bundle(options.project, options.destination))
    except Exception:
        print(json.dumps(dict(state='error',
            error='Staging non vérifié : vérifier les chemins, les fichiers publics et l’espace disponible.',
            upload_performed=False), ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
