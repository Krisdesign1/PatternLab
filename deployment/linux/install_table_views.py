"""Install reviewed table presentation sources only; preserve live records."""
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from uuid import uuid4


spec = importlib.util.spec_from_file_location(
    'table_view_installer_base', Path(__file__).with_name('install_adaptive_proposals.py'))
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)


def require(value, message):
    if not value:
        raise RuntimeError(message)


def load_bundle(path, expected):
    require(base.digest(path) == expected, 'Bundle hash mismatch')
    with tarfile.open(path) as archive:
        members = archive.getmembers()
        require(all(item.isfile() for item in members), 'Only regular files allowed')
        require(len({item.name for item in members}) == len(members), 'Duplicate member')
        manifest = json.load(archive.extractfile('manifest.json'))
        names = set(manifest['sha256'])
        require(names == set(manifest['previous_sha256']), 'Incomplete previous hashes')
        require({item.name for item in members} == names | {'manifest.json'}, 'Unexpected scope')
        require(all('/' not in name and name.endswith(('.py', '.js', '.css')) for name in names),
                'Only root presentation files allowed')
        require({'table_views.py', 'table_view_state.py', 'table_view_editor.js',
                 'table_view_editor.css', 'app.py'} <= names, 'Missing view implementation')
        payload = {name: archive.extractfile(name).read() for name in names}
    for name, data in payload.items():
        require(hashlib.sha256(data).hexdigest() == manifest['sha256'][name], 'Payload hash mismatch')
        if name.endswith('.py'):
            compile(data, name, 'exec')
        target = base.ROOT / name
        require(not target.is_symlink(), 'Unexpected target symlink')
        require((base.digest(target) if target.exists() else None) == manifest['previous_sha256'][name],
                'Live source differs from reviewed baseline: ' + name)
    return manifest, payload


def protected_sources(names):
    paths = {p for p in base.ROOT.rglob('*.py')
             if p.relative_to(base.ROOT).parts[0] not in ('.venv', '.git', '__pycache__')
             and str(p.relative_to(base.ROOT)) not in names}
    paths.update(Path(name) for name in base.AUTH if Path(name).is_file())
    units = Path('/etc/systemd/system')
    paths.update(p for p in units.rglob('*patternlab*') if p.is_file())
    for directory in units.glob('*patternlab*.d'):
        paths.update(p for p in directory.rglob('*') if p.is_file())
    return {str(path): base.digest(path) for path in sorted(paths)}


def readonly_archives():
    result = {}
    for name, table in base.ARCHIVES.items():
        path = base.ROOT / 'data/api_football' / name
        require(not path.is_symlink(), 'Unexpected archive symlink')
        if path.exists():
            with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=30) as db:
                result[name] = base.archive_rows(db, table)
        else:
            result[name] = {}
    return result


def deploy(bundle, expected, *, backup_prefix='table-views', receipt_state='table_views_deployed',
           storage='browser_preferences_only'):
    require(os.geteuid() == 0, 'Root privileges required')
    manifest, payload = load_bundle(Path(bundle), expected)
    base.FILES = tuple(payload)
    frozen = base.frozen()
    preserved = protected_sources(payload)
    archived = readonly_archives()
    require(shutil.disk_usage(base.ROOT).free > 2 * sum(map(len, payload.values())) + 64 * 1024**2,
            'Insufficient space for source rollback')
    proxy_active = subprocess.run(['/usr/bin/systemctl', 'is-active', '--quiet',
                                   'patternlab-proxy.service']).returncode == 0
    backup = base.BACKUP_ROOT / (backup_prefix + '-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
                                + '-' + uuid4().hex[:6])
    backup.mkdir(mode=0o700, parents=True)
    (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    metadata = {}
    for name in payload:
        target = base.ROOT / name
        if target.exists():
            stat = target.stat()
            metadata[name] = (stat.st_mode & 0o777, (stat.st_uid, stat.st_gid))
            shutil.copy2(target, backup / name)
        else:
            metadata[name] = (0o644, base.application_owner())

    def check():
        require(protected_sources(payload) == preserved, 'Protected source or service configuration changed')
        require(base.frozen() == frozen, 'Frozen model state changed')
        base.require_legacy_unchanged(archived)

    def start():
        base.command('/usr/bin/systemctl', 'start', 'patternlab-web.service')
        if proxy_active:
            base.command('/usr/bin/systemctl', 'start', 'patternlab-proxy.service')

    try:
        base.command('/usr/bin/systemctl', 'stop', 'patternlab-web.service')
        for name, data in payload.items():
            mode, owner = metadata[name]
            base.atomic_write(base.ROOT / name, data, mode=mode, owner=owner)
        require(all(base.digest(base.ROOT / name) == manifest['sha256'][name] for name in payload),
                'Installed hash mismatch')
        check()
        start()
        require(base.healthy(), 'Web health failed')
        check()
    except BaseException:
        base.command('/usr/bin/systemctl', 'stop', 'patternlab-web.service')
        for name in payload:
            old = backup / name
            if old.exists():
                mode, owner = metadata[name]
                base.atomic_write(base.ROOT / name, old.read_bytes(), mode=mode, owner=owner)
            else:
                (base.ROOT / name).unlink(missing_ok=True)
        start()
        raise
    receipt = dict(state=receipt_state, deployed_at=datetime.now(timezone.utc).isoformat(),
                   source_sha256=manifest['sha256'], backup_directory=str(backup),
                   records_preserved={name: len(rows) for name, rows in archived.items()},
                   web_healthy=True, public_proxy_resumed=proxy_active,
                   database_writes=0, models_unchanged=True, worker_restarted=False,
                   timers_unchanged=True, authentication_unchanged=True,
                   network_rules_changed=False, storage=storage)
    (backup / 'receipt.json').write_text(json.dumps(receipt, indent=2))
    return receipt


if __name__ == '__main__':
    require(len(sys.argv) == 3, 'Usage: install_table_views.py BUNDLE SHA256')
    print(json.dumps(deploy(sys.argv[1], sys.argv[2])))
