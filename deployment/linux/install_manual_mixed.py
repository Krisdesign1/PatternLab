"""Install the reviewed mixed-manual sources; never migrate live records.

The bundle contains only the fixed source list below. Helpers reuse the existing
source/auth/service/model checks and read-only archive projection. Rollback
restores source files only, preserving any records appended by the live worker.
Run beside install_table_views.py and install_adaptive_proposals.py.
"""
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
from uuid import uuid4


spec = importlib.util.spec_from_file_location(
    'manual_mixed_installer_support', Path(__file__).with_name('install_table_views.py'))
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)
base = support.base

FILES = (
    'manual_analysis_panel.py', 'manual_analysis_store.py', 'manual_journal_rows.py',
    'manual_proposal_archive.py', 'scenario_workspace_panel.py', 'table_views.py',
    'manual_mixed_analysis.py', 'manual_mixed_panel.py',
)
NEW_FILES = frozenset(('manual_mixed_analysis.py', 'manual_mixed_panel.py'))
SHA256 = re.compile(r'^[0-9a-f]{64}$')


def load_bundle(path, expected):
    """Verify the bytes, fixed scope, syntax and reviewed live baseline."""
    base.require(base.digest(path) == expected, 'Bundle hash mismatch')
    with tarfile.open(path) as archive:
        members = archive.getmembers()
        base.require(len(members) == len(FILES) + 1 and all(item.isfile() for item in members)
                     and {item.name for item in members} == set(FILES) | {'manifest.json'},
                     'Unexpected manual-mixed bundle scope')
        manifest = json.load(archive.extractfile('manifest.json'))
        base.require(isinstance(manifest, dict)
                     and isinstance(manifest.get('sha256'), dict)
                     and isinstance(manifest.get('previous_sha256'), dict)
                     and set(manifest['sha256']) == set(FILES)
                     and set(manifest['previous_sha256']) == set(FILES),
                     'Incomplete reviewed manual-mixed manifest')
        payload = {name: archive.extractfile(name).read() for name in FILES}
    base.require(base.ROOT.is_dir() and not base.ROOT.is_symlink(), 'Unexpected application root')
    for name, data in payload.items():
        expected_new, expected_old = manifest['sha256'][name], manifest['previous_sha256'][name]
        base.require(isinstance(expected_new, str) and SHA256.fullmatch(expected_new),
                     'Invalid reviewed source hash')
        base.require((expected_old is None and name in NEW_FILES)
                     or isinstance(expected_old, str) and SHA256.fullmatch(expected_old),
                     'Only new mixed-manual files may have an absent baseline')
        base.require(hashlib.sha256(data).hexdigest() == expected_new, 'Reviewed source hash mismatch')
        compile(data, name, 'exec')
        target = base.ROOT / name
        base.require(not target.is_symlink() and (not target.exists() or target.is_file()),
                     'Unexpected target symlink or non-file')
        base.require((base.digest(target) if target.exists() else None) == expected_old,
                     'Live source differs from reviewed baseline: ' + name)
    return manifest, payload


def policy_assets():
    """Keep existing scenario activation and calibration outside this rollout."""
    result = {}
    for name in (base.ENABLED, base.ARTIFACT):
        path = base.ROOT / name
        base.require(not path.is_symlink(), 'Unexpected policy symlink')
        result[name] = base.digest(path) if path.exists() else None
    return result


def proxy_active():
    result = subprocess.run(['/usr/bin/systemctl', 'is-active', '--quiet',
                             'patternlab-proxy.service'], capture_output=True)
    return result.returncode == 0


def deploy(bundle, expected, *, backup_root=None):
    base.require(os.geteuid() == 0, 'Root privileges required')
    manifest, payload = load_bundle(Path(bundle), expected)
    # The helper module is private to this installer; its frozen-source checks
    # must use this feature's exact allowlist rather than the original rollout.
    base.FILES = FILES
    frozen = base.frozen()
    preserved = support.protected_sources(FILES)
    policies = policy_assets()
    archives = support.readonly_archives()
    base.require(shutil.disk_usage(base.ROOT).free > 2 * sum(map(len, payload.values())) + 64 * 1024**2,
                 'Insufficient space for source rollback')
    resume_proxy = proxy_active()
    backup = Path(backup_root or base.BACKUP_ROOT) / (
        'manual-mixed-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:6])
    backup.mkdir(mode=0o700, parents=True)
    os.chmod(backup, 0o700)
    (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    metadata = {}
    for name in FILES:
        target = base.ROOT / name
        if target.exists():
            state = target.stat()
            metadata[name] = (state.st_mode & 0o777, (state.st_uid, state.st_gid))
            shutil.copy2(target, backup / name)
        else:
            metadata[name] = (0o644, base.application_owner())

    def check():
        base.require(support.protected_sources(FILES) == preserved,
                     'Protected source, authentication or service configuration changed')
        base.require(base.frozen() == frozen, 'Frozen model state changed')
        base.require(policy_assets() == policies, 'Scenario policy or calibration changed')
        base.require_legacy_unchanged(archives)

    def start():
        base.command('/usr/bin/systemctl', 'start', 'patternlab-web.service')
        if resume_proxy:
            base.command('/usr/bin/systemctl', 'start', 'patternlab-proxy.service')

    try:
        base.command('/usr/bin/systemctl', 'stop', 'patternlab-web.service')
        for name, data in payload.items():
            mode, owner = metadata[name]
            base.atomic_write(base.ROOT / name, data, mode=mode, owner=owner)
        base.require(all(base.digest(base.ROOT / name) == manifest['sha256'][name] for name in FILES),
                     'Installed manual-mixed source hash mismatch')
        check()
        start()
        base.require(base.healthy(), 'Web health failed')
        check()
    except BaseException:
        base.command('/usr/bin/systemctl', 'stop', 'patternlab-web.service')
        for name in FILES:
            previous = backup / name
            if previous.exists():
                mode, owner = metadata[name]
                base.atomic_write(base.ROOT / name, previous.read_bytes(), mode=mode, owner=owner)
            elif manifest['previous_sha256'][name] is None:
                (base.ROOT / name).unlink(missing_ok=True)
        start()
        (backup / 'receipt.json').write_text(json.dumps(dict(
            state='manual_mixed_rolled_back', rolled_back_at=datetime.now(timezone.utc).isoformat(),
            backup_directory=str(backup), rollback_scope='source_only', database_writes=0,
            worker_restarted=False, timers_changed=False, public_proxy_resumed=resume_proxy), indent=2))
        raise
    receipt = dict(state='manual_mixed_deployed', deployed_at=datetime.now(timezone.utc).isoformat(),
        source_sha256=manifest['sha256'], previous_source_sha256=manifest['previous_sha256'],
        backup_directory=str(backup), rollback_scope='source_only',
        records_preserved={name: len(rows) for name, rows in archives.items()},
        web_healthy=True, public_proxy_resumed=resume_proxy, database_writes=0,
        models_unchanged=True, training_performed=False, worker_restarted=False,
        timers_unchanged=True, authentication_unchanged=True, scenario_policy_unchanged=True,
        network_rules_changed=False, mixed_manual_enabled=True)
    (backup / 'receipt.json').write_text(json.dumps(receipt, indent=2))
    return receipt


if __name__ == '__main__':
    base.require(len(sys.argv) == 3, 'Usage: install_manual_mixed.py BUNDLE SHA256')
    print(json.dumps(deploy(sys.argv[1], sys.argv[2])))
