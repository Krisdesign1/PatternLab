"""Replace only the workspace view, preserving models, archives and services.

The archive checks use read-only logical fingerprints. Only the old Python
source is copied for rollback; SQLite files are never written or restored.
"""
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
    'workspace_installer_base', Path(__file__).with_name('install_adaptive_proposals.py'))
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
PRESERVED_SOURCES = tuple(name for name in base.SOURCES if name != 'scenario_workspace_panel.py')
SOURCES = FILES = ('scenario_workspace_panel.py',)
base.SOURCES = SOURCES
base.FILES = FILES


def load_bundle(path, expected):
    base.require(base.digest(path) == expected, 'Bundle hash mismatch')
    with tarfile.open(path) as archive:
        members = archive.getmembers()
        base.require(len(members) == 2 and all(item.isfile() for item in members)
                     and {item.name for item in members} == set(FILES)|{'manifest.json'},
                     'Unexpected bundle scope')
        manifest = json.load(archive.extractfile('manifest.json'))
        payload = {name: archive.extractfile(name).read() for name in FILES}
    base.require(set(manifest.get('sha256', {})) == set(FILES)
                 and set(manifest.get('previous_sha256', {})) == set(FILES),
                 'Incomplete reviewed manifest')
    for name, data in payload.items():
        base.require(hashlib.sha256(data).hexdigest() == manifest['sha256'][name], 'Source hash mismatch')
        compile(data, name, 'exec')
        target = base.target_path(name)
        base.require(not target.is_symlink(), 'Unexpected symlink')
        base.require(target.is_file() and base.digest(target) == manifest['previous_sha256'][name],
                     'Unreviewed live source')
    return manifest, payload


def readonly_archives():
    previous = {}
    for filename, table in base.ARCHIVES.items():
        path = base.ROOT/'data/api_football'/filename
        base.require(not path.is_symlink(), 'Unexpected archive symlink')
        if not path.exists():
            previous[filename] = {}
            continue
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=30) as connection:
            previous[filename] = base.archive_rows(connection, table)
    return previous


def preserved_sources():
    paths = [base.ROOT/name for name in (*PRESERVED_SOURCES, base.ARTIFACT, base.ENABLED)]
    return {str(path): base.digest(path) if path.exists() else None for path in paths}


def resource_headroom(payload):
    # Only a source copy and its atomic replacement are created. Existing
    # verified database backups remain in place and are not duplicated.
    size = sum(map(len, payload.values()))
    base.require(shutil.disk_usage(base.ROOT).free >= 2*size+64*1024*1024,
                 'Not enough disk headroom for source rollback')
    available = next(int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()
                     if line.startswith('MemAvailable:'))
    base.require(available >= 2*1024*1024, 'Not enough memory headroom for web restart')


def proxy_active():
    return subprocess.run(['/usr/bin/systemctl', 'is-active', '--quiet',
                           'patternlab-proxy.service'], capture_output=True).returncode == 0


def deploy(bundle, expected, *, backup_root=None):
    base.require(os.geteuid() == 0, 'Root privileges required')
    manifest, payload = load_bundle(Path(bundle), expected)
    before = base.frozen()
    protected = base.protected_snapshot()
    source_protections = preserved_sources()
    archives = readonly_archives()
    resource_headroom(payload)
    resume_proxy = proxy_active()
    rollback = Path(backup_root or base.BACKUP_ROOT)/('workspace-continuity-'+
        datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:6])
    rollback.mkdir(parents=True, mode=0o700)
    (rollback/'manifest.json').write_text(json.dumps(manifest, indent=2))
    metadata = {}
    for name in FILES:
        target = base.target_path(name)
        state = target.stat()
        metadata[name] = (state.st_mode&0o777, (state.st_uid, state.st_gid))
        shutil.copy2(target, rollback/name)

    def check_preserved():
        base.require_protected_unchanged(protected)
        base.require(preserved_sources() == source_protections, 'Proposal/archive or policy source changed')
        value = base.frozen()
        base.require_legacy_unchanged(archives)
        return value

    def resume_web():
        base.command('/usr/bin/systemctl', 'start', 'patternlab-web.service')
        if resume_proxy:
            base.command('/usr/bin/systemctl', 'start', 'patternlab-proxy.service')

    try:
        base.command('/usr/bin/systemctl', 'stop', 'patternlab-web.service')
        for name, data in payload.items():
            mode, owner = metadata[name]
            base.atomic_write(base.target_path(name), data, mode=mode, owner=owner)
        base.require(all(base.digest(base.target_path(name)) == manifest['sha256'][name] for name in FILES),
                     'Installed source hash mismatch')
        after = check_preserved()
        resume_web()
        base.require(base.healthy(), 'Web health failed')
        after = check_preserved()
    except BaseException:
        base.command('/usr/bin/systemctl', 'stop', 'patternlab-web.service')
        for name in FILES:
            mode, owner = metadata[name]
            base.atomic_write(base.target_path(name), (rollback/name).read_bytes(), mode=mode, owner=owner)
        # Restore source only, retaining any concurrent worker appends.
        resume_web()
        raise
    receipt = dict(state='workspace_continuity_deployed', deployed_at=datetime.now(timezone.utc).isoformat(),
        before=before, after=after, source_sha256=manifest['sha256'],
        backup_directory=str(rollback), backup_scope=list(FILES),
        original_records_preserved={name: len(rows) for name, rows in archives.items()},
        archive_check='read_only_logical_fingerprints', database_writes=0,
        web_healthy=True, public_proxy_resumed=resume_proxy,
        worker_restarted=False, timers_unchanged=True, models_unchanged=True,
        authentication_unchanged=True, proposal_archive_sources_unchanged=True,
        network_rules_changed=False)
    (rollback/'receipt.json').write_text(json.dumps(receipt, indent=2))
    (base.ROOT/'workspace-continuity-upgrade-manifest.json').write_text(json.dumps(manifest, indent=2))
    return receipt


if __name__ == '__main__':
    base.require(len(sys.argv) == 3, 'Usage: install_workspace_continuity.py BUNDLE SHA256')
    print(json.dumps(deploy(Path(sys.argv[1]), sys.argv[2])))
