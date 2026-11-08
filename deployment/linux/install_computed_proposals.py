"""Remove qualification gating without rewriting any existing forecast.

Reuse the reviewed rollback/archive/protection checks. This rollout replaces
only the seven reviewed files and marker; calibration remains read-only evidence.
"""
from datetime import datetime, timezone, timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from uuid import uuid4

spec = importlib.util.spec_from_file_location('adaptive_installer', Path(__file__).with_name('install_adaptive_proposals.py'))
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
SOURCES = ('scenario_proposal_archive.py', 'manual_analysis_store.py',
           'manual_proposal_archive.py', 'scenario_tables.py',
           'scenario_presentation.py', 'scenario_workspace_panel.py', 'adaptive_scenario_proposals.py')
ENABLED = base.ENABLED
FILES = (*SOURCES, ENABLED)
MARKER = b'computed-scenario-proposals-v2\n'
base.FILES = FILES
base.SOURCES = SOURCES


def load_bundle(path, expected):
    base.require(base.digest(path) == expected, 'Bundle hash mismatch')
    with tarfile.open(path) as archive:
        members = archive.getmembers()
        base.require(all(item.isfile() for item in members) and len(members) == len(FILES)+1
                     and {item.name for item in members} == set(FILES)|{'manifest.json'}, 'Unexpected bundle scope')
        manifest = json.load(archive.extractfile('manifest.json'))
        payload = {name:archive.extractfile(name).read() for name in FILES}
    base.require(set(manifest.get('sha256', {})) == set(FILES)
                 and set(manifest.get('previous_sha256', {})) == set(FILES), 'Incomplete reviewed manifest')
    for name, data in payload.items():
        base.require(hashlib.sha256(data).hexdigest() == manifest['sha256'][name], 'Source hash mismatch')
        if name in SOURCES:
            compile(data, name, 'exec')
        target = base.target_path(name)
        base.require(not target.is_symlink(), 'Unexpected symlink')
        base.require((base.digest(target) if target.exists() else None) == manifest['previous_sha256'][name],
                     'Unreviewed live source')
    base.require(payload[ENABLED] == MARKER, 'Wrong forward policy marker')
    return manifest, payload


def validate_installed_policy():
    # Isolated in memory, not a prediction written to the production journals.
    source = '''from datetime import datetime, timezone, timedelta
import json
import adaptive_scenario_proposals as p
now=datetime.now(timezone.utc)
for size in range(2,21):
    fixtures=[dict(match_id=i+1,timestamp=(now+timedelta(days=1)).isoformat()) for i in range(size)]
    ranking=[dict(sequence=[value]*size,probability=chance) for value,chance in [('2nd',.2),('1st',.15),('Égal',.1)]]
    methods={'clubs':dict(version='verification-ranking-only',ranking=ranking)}
    saved=p.capture(methods,fixtures,now=now,artifact_path='/nonexistent/calibration.json')
    p.validate(saved,methods,fixtures,now.isoformat())
    assert saved['methods']['clubs']['ranking']==ranking and not saved['unavailable']
    assert saved['methods']['clubs']['evidence']['status']=='unavailable'
print(json.dumps(dict(policy=p.POLICY_VERSION, sizes_verified=list(range(2,21)), qualification_blocks_proposals=False)))'''
    return json.loads(base.command('/usr/sbin/runuser', '-u', 'patternlab', '--',
        str(base.ROOT/'.venv/bin/python'), '-B', '-c', source))


def proxy_active():
    # This unit follows the web unit's shutdown, but a subsequent web start
    # does not automatically resume it. Preserve its prior running state.
    result = subprocess.run(['/usr/bin/systemctl', 'is-active', '--quiet',
                             'patternlab-proxy.service'], capture_output=True)
    return result.returncode == 0


def deploy(bundle, expected, *, backup_root=None):
    base.require(os.geteuid() == 0, 'Root privileges required')
    manifest, payload = load_bundle(Path(bundle), expected)
    before = base.frozen()
    protected = base.protected_snapshot()
    calibration_path = base.ROOT/base.ARTIFACT
    calibration_hash = base.digest(calibration_path) if calibration_path.exists() else None
    base.resource_headroom(payload)
    rollback = Path(backup_root or base.BACKUP_ROOT)/('computed-proposals-'+
        datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:6])
    rollback.mkdir(parents=True, mode=0o700)
    (rollback/'manifest.json').write_text(json.dumps(manifest, indent=2))
    metadata = {}
    for name in FILES:
        target = base.target_path(name)
        if target.exists():
            state = target.stat()
            metadata[name] = (state.st_mode&0o777, (state.st_uid, state.st_gid))
            previous = rollback/'files'/name
            previous.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(target, previous)
    archives = base.archive_snapshot(rollback)
    resume_proxy = proxy_active()
    try:
        # Prevent Streamlit from retaining an older manual archive module while
        # importing the newer capture module during this short file replacement.
        base.command('/usr/bin/systemctl', 'stop', 'patternlab-web.service')
        for name in FILES:
            state = metadata.get(name)
            base.atomic_write(base.target_path(name), payload[name],
                mode=state[0] if state else 0o644, owner=state[1] if state else None)
        verified = validate_installed_policy()
        base.require(all(base.digest(base.target_path(name)) == manifest['sha256'][name] for name in FILES),
                     'Installed source hash mismatch')
        base.require_protected_unchanged(protected)
        base.frozen()
        base.require_legacy_unchanged(archives)
        base.require((base.digest(calibration_path) if calibration_path.exists() else None) == calibration_hash,
                     'Historical calibration changed')
        base.command('/usr/bin/systemctl', 'start', 'patternlab-web.service')
        if resume_proxy:
            base.command('/usr/bin/systemctl', 'start', 'patternlab-proxy.service')
        base.require(base.healthy(), 'Web health failed')
        base.require_legacy_unchanged(archives)
        base.require_protected_unchanged(protected)
    except BaseException:
        base.command('/usr/bin/systemctl', 'stop', 'patternlab-web.service')
        for name in FILES:
            previous = rollback/'files'/name
            if previous.exists():
                mode, owner = metadata[name]
                base.atomic_write(base.target_path(name), previous.read_bytes(), mode=mode, owner=owner)
            elif manifest['previous_sha256'][name] is None:
                base.target_path(name).unlink(missing_ok=True)
        # Never restore a database over live appends.
        base.command('/usr/bin/systemctl', 'start', 'patternlab-web.service')
        if resume_proxy:
            base.command('/usr/bin/systemctl', 'start', 'patternlab-proxy.service')
        raise
    receipt = dict(state='computed_proposals_deployed', deployed_at=datetime.now(timezone.utc).isoformat(),
        policy=verified, source_sha256=manifest['sha256'], protected=before,
        backup_directory=str(rollback), original_records_preserved={name:len(rows) for name,rows in archives.items()},
        archive_backups={name:str(rollback/name) for name in archives if (rollback/name).exists()},
        historical_calibration_unchanged=True, web_healthy=True, worker_restarted=False,
        database_writes=0, models_unchanged=True, authentication_unchanged=True,
        club_trends_unchanged=True, timers_unchanged=True, public_proxy_resumed=resume_proxy)
    (rollback/'receipt.json').write_text(json.dumps(receipt, indent=2))
    (base.ROOT/'computed-proposals-upgrade-manifest.json').write_text(json.dumps(manifest, indent=2))
    return receipt


if __name__ == '__main__':
    base.require(len(sys.argv) == 3, 'Usage: install_computed_proposals.py BUNDLE SHA256')
    print(json.dumps(deploy(Path(sys.argv[1]), sys.argv[2])))
