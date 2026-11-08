"""Private snapshot of installed packages and offline rehydration into a new venv.

This captures installed package bytes, not wheels or a package-index lockfile.
The base interpreter and compatible OS/native libraries remain prerequisites.
No pip command, index, credential or network request is used.
"""
from datetime import datetime, timezone
from hashlib import sha256
import importlib.metadata as metadata
import json
from pathlib import Path
import platform
import re
import shutil
import sys
import sysconfig
import venv

import local_recovery as recovery


VERSION='offline-installed-environment-v1'


def runtime():
    return dict(python=platform.python_version(),implementation=platform.python_implementation(),
                platform=sysconfig.get_platform(),machine=platform.machine())


def packages():
    return sorted([dict(name=d.metadata['Name'],version=d.version) for d in metadata.distributions()],
                  key=lambda item:item['name'].casefold())


def _path(base,name):
    if not isinstance(name,str) or not name or '\\' in name or '\x00' in name:
        raise ValueError('Chemin de dépendance invalide.')
    rel=Path(name)
    if rel.is_absolute() or '..' in rel.parts or rel.as_posix()!=name or rel==Path('.'):
        raise ValueError('Chemin de dépendance invalide.')
    path=base
    for part in rel.parts:
        path=path/part
        if path.is_symlink():raise ValueError('Lien de dépendance refusé.')
    if not path.resolve().is_relative_to(base.resolve()):raise ValueError('Chemin de dépendance invalide.')
    return path


def _items(manifest):
    if not isinstance(manifest,dict) or manifest.get('version')!=VERSION or manifest.get('state')!='complete':
        raise ValueError('Le paquet d’environnement est incomplet.')
    if manifest.get('runtime')!=runtime():
        raise ValueError('Cet environnement exige le même Python, la même plateforme et architecture.')
    if not isinstance(manifest.get('files'),list) or not manifest['files']:
        raise ValueError('Inventaire des dépendances absent.')
    seen=set()
    for item in manifest['files']:
        if (not isinstance(item,dict) or set(item)!={'path','bytes','sha256'}
                or not isinstance(item.get('path'),str) or item['path'] in seen
                or type(item.get('bytes')) is not int or item['bytes']<0
                or not isinstance(item.get('sha256'),str) or re.fullmatch('[a-f0-9]{64}',item['sha256']) is None):
            raise ValueError('Inventaire des dépendances invalide.')
        _path(Path('/dependency-inventory'),item['path']);seen.add(item['path'])
    return manifest['files']


def capture(destination):
    destination=Path(destination).resolve()
    source=Path(sysconfig.get_path('purelib')).resolve()
    if destination.is_relative_to(source):raise ValueError('Choisir un dossier séparé de l’environnement actif.')
    destination.mkdir(parents=True,exist_ok=False,mode=0o700)
    manifest=dict(version=VERSION,state='incomplete',started_at=datetime.now(timezone.utc).isoformat(),
                  runtime=runtime(),distributions=packages(),files=[],origin='installed_package_snapshot',
                  wheel_installation_proven=False,base_interpreter_included=False)
    index=destination/'manifest.json';recovery._write_json_atomic(index,manifest)
    for path in sorted(source.rglob('*')):
        if '__pycache__' in path.parts or path.suffix in ('.pyc','.pyo'):continue
        if path.is_symlink():raise ValueError('Un lien existe dans l’environnement source ; copie refusée.')
        if not path.is_file():continue
        name=path.relative_to(source).as_posix();target=_path(destination/'site-packages',name)
        if path.suffix=='.pth':
            # Only ordinary in-bundle relative paths are permitted. Executable
            # startup hooks need separate review, not silent restoration.
            for line in path.read_text().splitlines():
                line=line.strip()
                if not line or line.startswith('#'):continue
                if line.startswith('import ') or line.startswith('import\t'):
                    raise ValueError('Un script de démarrage Python doit être examiné séparément.')
                _path(source,line)
        before=recovery.fingerprint(path);target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,target)
        if before!=recovery.fingerprint(target) or before!=recovery.fingerprint(path):
            raise ValueError('Une dépendance a changé pendant sa copie.')
        manifest['files'].append(dict(path=name,bytes=target.stat().st_size,sha256=before))
    manifest.update(state='complete',completed_at=datetime.now(timezone.utc).isoformat())
    _items(manifest);recovery._write_json_atomic(index,manifest)
    return dict(manifest_sha256=recovery.fingerprint(index),runtime=manifest['runtime'],
                files=len(manifest['files']),distributions=len(manifest['distributions']),
                bytes=sum(i['bytes'] for i in manifest['files']),wheel_installation_proven=False)


def rebuild(bundle,destination,expected_manifest_sha256):
    bundle=Path(bundle).resolve();destination=Path(destination).absolute()
    if destination.exists() or destination.is_symlink():raise ValueError('L’environnement de destination existe déjà.')
    if not isinstance(expected_manifest_sha256,str) or re.fullmatch('[a-f0-9]{64}',expected_manifest_sha256) is None:
        raise ValueError('Empreinte de l’environnement absente.')
    index=bundle/'manifest.json'
    if index.is_symlink() or recovery.fingerprint(index)!=expected_manifest_sha256:
        raise ValueError('Le paquet d’environnement ne correspond pas à son empreinte.')
    manifest=json.loads(index.read_text());items=_items(manifest)
    for item in items:
        path=_path(bundle/'site-packages',item['path'])
        if not path.is_file() or path.stat().st_size!=item['bytes'] or recovery.fingerprint(path)!=item['sha256']:
            raise ValueError('Une dépendance sauvegardée est absente ou altérée.')
    venv.EnvBuilder(with_pip=False,symlinks=False).create(destination)
    site=destination/'lib'/f'python{sys.version_info.major}.{sys.version_info.minor}'/'site-packages'
    if not site.is_dir():raise ValueError('La structure du nouvel environnement Python est incompatible.')
    for item in items:
        source=_path(bundle/'site-packages',item['path']);target=_path(site,item['path'])
        target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
        if recovery.fingerprint(target)!=item['sha256']:raise ValueError('La dépendance restaurée a été altérée.')
    if recovery.fingerprint(index)!=expected_manifest_sha256:raise ValueError('Le manifeste d’environnement a changé.')
    return dict(version=VERSION,state='rehydrated',runtime=manifest['runtime'],manifest_sha256=expected_manifest_sha256,
                files=len(items),distributions=manifest['distributions'],python=str(destination/'bin/python'),
                base_interpreter_included=False,wheel_installation_proven=False,network_used=False)
