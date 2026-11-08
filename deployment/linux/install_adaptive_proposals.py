"""Scoped adaptive-proposal rollout; preserve archives and roll back files only.

The bundle is read as bytes, never extracted. New selection is activated only
when all reviewed sources and the installed calibration have been verified.
Existing forecast rows are never migrated, regenerated or restored over live
worker appends. Only the web service may be restarted by this installer.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from uuid import uuid4

ROOT = Path('/opt/patternlab')
BACKUP_ROOT = Path('/var/lib/patternlab-cutover')
SOURCES = (
    'adaptive_scenario_selection.py', 'adaptive_scenario_calibration.py', 'adaptive_scenario_proposals.py',
    'prediction_journal.py', 'scenario_proposal_archive.py', 'scenario_tables.py', 'scenario_outcomes.py',
    'bet_simulation.py', 'group_probability_diagnostics.py', 'manual_analysis_store.py',
    'manual_proposal_archive.py', 'manual_scenario_progress.py', 'manual_journal_rows.py',
    'scenario_presentation.py', 'scenario_workspace_panel.py', 'manual_analysis_panel.py',
    'context_neighbors_panel.py', 'daily_analysis_panel.py', 'journal_panel.py', 'journal_browser_data.py',
    'prediction_panel.py', 'high_probability_panel.py', 'high_probability_history_panel.py',
    'cloud_ui.py', 'bet_simulation_panel.py', 'scripts/calibrate_scenario_policy.py',
)
ARTIFACT = 'data/api_football/scenario_policy/calibration.json'
ENABLED = 'data/api_football/scenario_policy/ENABLED'
FILES = (*SOURCES, ARTIFACT, ENABLED)
MARKER = b'adaptive-scenario-proposals-v1\n'
ARCHIVES = {'predictions.sqlite3': 'predictions', 'manual_analyses.sqlite3': 'manual_analyses'}
CONTEXT_SOURCES = (
    'club_trends.py', 'club_trends_panel.py', 'club_trends_day.py', 'ui_pages/club_trends.py', 'navigation_local.py',
    'competition_trends.py', 'competition_trends_panel.py', 'context_observation_store.py', 'context_archive_sync.py',
    'context_quality_audit.py', 'context_event_quality.py', 'prematch_context_quality.py',
    'scripts/sync_context_archive.py', 'deployment/linux/run_context_archive.py', 'manual_bet_store.py',
)
AUTH = ('/opt/patternlab/admin_access.py', '/opt/patternlab/.streamlit/secrets.toml',
        '/etc/systemd/system/patternlab-web.service.d/google-auth.conf')


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def target_path(name):
    require(name in FILES, 'File outside reviewed deployment scope')
    return ROOT/name


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, cwd=ROOT).stdout.strip()


def frozen():
    evidence = json.loads((ROOT/'audit/PATTERNLAB2_PRESERVATION_LOT14_2026-11-02.json').read_text())
    candidate = evidence['candidate_integrity']
    sources = {**evidence['frozen_source_sha256'], **candidate['implementation_sha256']}
    require(not set(sources)&set(FILES), 'Reviewed bundle touches frozen model source')
    for name, expected in sources.items():
        require(digest(ROOT/name) == expected, 'Frozen source mismatch')
    directory = ROOT/'data/api_football/causal_confirmation/candidates'/candidate['candidate_id']
    for name, expected in candidate['artifact_sha256'].items():
        require(digest(directory/name) == expected, 'Candidate artifact mismatch')
    overlay = json.loads((ROOT/'linux-kit-overlay-manifest.json').read_text())
    for item in overlay['files']:
        require(item['path'] not in FILES, 'Reviewed bundle touches original overlay')
        require(digest(ROOT/item['path']) == item['sha256'], 'Original overlay mismatch')
    return dict(protected_sources=len(sources), overlay_files=len(overlay['files']), candidate_unchanged=True)


def protected_snapshot():
    worker = json.loads((ROOT/'worker-scoped-io-manifest.json').read_text())
    worker_sources = {*worker['source_sha256'], *worker['preserved_dependency_sha256']}
    require(not worker_sources&set(FILES), 'Reviewed bundle touches protected worker source')
    paths = {ROOT/name for name in (*worker_sources, *CONTEXT_SOURCES)}|{Path(name) for name in AUTH}
    # Unit files and timer symlinks stay byte-for-byte unchanged. This does not
    # confuse a worker's natural next scheduled invocation with a restart here.
    unit_root = Path('/etc/systemd/system')
    for path in unit_root.rglob('*patternlab*'):
        if path.is_file():
            paths.add(path)
        elif path.is_dir():
            paths.update(item for item in path.rglob('*') if item.is_file())
    return {str(path): digest(path) for path in sorted(paths)}


def require_protected_unchanged(before):
    require(protected_snapshot() == before, 'Authentication, worker, timers, club trends or context sources changed')


def load_bundle(bundle, expected):
    require(digest(bundle) == expected, 'Bundle hash mismatch')
    with tarfile.open(bundle) as archive:
        members = archive.getmembers()
        require(len(members) == len(FILES)+1 and all(member.isfile() for member in members), 'Unexpected bundle members')
        require({member.name for member in members} == set(FILES)|{'manifest.json'}, 'Unexpected bundle scope')
        manifest = json.load(archive.extractfile('manifest.json'))
        require(set(manifest.get('sha256', {})) == set(FILES), 'Incomplete source manifest')
        require(set(manifest.get('previous_sha256', {})) == set(FILES), 'Incomplete previous-source manifest')
        payload = {name: archive.extractfile(name).read() for name in FILES}
    for name, data in payload.items():
        require(hashlib.sha256(data).hexdigest() == manifest['sha256'][name], 'Reviewed source hash mismatch')
        if name in SOURCES:
            compile(data, name, 'exec')
        target = target_path(name)
        require(not target.is_symlink(), 'Deployment target must not be a symlink')
        require((digest(target) if target.exists() else None) == manifest['previous_sha256'][name], 'Unreviewed existing file')
    require(payload[ENABLED] == MARKER, 'Activation marker mismatch')
    require(len(payload[ARTIFACT]) <= 4*1024*1024, 'Calibration artifact too large')
    require(isinstance(json.loads(payload[ARTIFACT]), dict), 'Calibration artifact is not an object')
    return manifest, payload


def archive_rows(connection, table):
    require(table in ARCHIVES.values(), 'Unreviewed archive table')
    return {key: (stamp, value, hashlib.sha256(payload.encode('utf-8')).hexdigest())
            for key, stamp, value, payload in connection.execute(
                f'SELECT id,created_at,digest,payload FROM {table}')}


def archive_snapshot(rollback):
    previous = {}
    for filename, table in ARCHIVES.items():
        source = ROOT/'data/api_football'/filename
        if not source.exists():
            previous[filename] = {}
            continue
        require(not source.is_symlink(), 'Archive file must not be a symlink')
        with sqlite3.connect(source.resolve().as_uri()+'?mode=ro', uri=True, timeout=30) as connection:
            connection.execute('BEGIN')
            previous[filename] = archive_rows(connection, table)
            with sqlite3.connect(rollback/filename) as backup:
                connection.backup(backup)
                require(backup.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'Archive backup failed')
            connection.rollback()
        os.chmod(rollback/filename, 0o600)
    return previous


def require_legacy_unchanged(previous):
    for filename, rows in previous.items():
        if not rows:
            continue
        source = ROOT/'data/api_football'/filename
        with sqlite3.connect(source.resolve().as_uri()+'?mode=ro', uri=True, timeout=30) as connection:
            current = archive_rows(connection, ARCHIVES[filename])
        require(all(current.get(key) == value for key, value in rows.items()), 'Original archived forecast data changed')


def resource_headroom(payload):
    archive_bytes = 0
    for filename in ARCHIVES:
        source = ROOT/'data/api_football'/filename
        for path in (source, Path(str(source)+'-wal')):
            if path.exists():
                archive_bytes += path.stat().st_size
    # Include rollback copies and transient backups, never run down the root
    # volume while live imports and logs continue to grow.
    require(shutil.disk_usage(ROOT).free >= 2*archive_bytes+sum(map(len, payload.values()))+64*1024*1024,
            'Not enough disk headroom for verified archive backups')
    available = next(int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()
                     if line.startswith('MemAvailable:'))
    require(available >= 2*1024*1024, 'Not enough memory headroom for web restart')


def application_owner():
    owner = pwd.getpwnam('patternlab')
    return owner.pw_uid, owner.pw_gid


def atomic_write(target, data, *, mode=0o644, owner=None):
    owner = application_owner() if owner is None else owner
    if not target.parent.exists():
        target.parent.mkdir(parents=True, mode=0o755)
        os.chown(target.parent, *owner)
    temporary = target.with_name(target.name+'.adaptive-new-'+uuid4().hex)
    try:
        with temporary.open('xb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.chown(temporary, *owner)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def healthy():
    for _ in range(25):
        result = subprocess.run(['/usr/bin/curl', '-fsS', '--max-time', '2',
                                 'http://127.0.0.1:8501/_stcore/health'], capture_output=True)
        if result.returncode == 0 and result.stdout.strip() == b'ok':
            return True
        time.sleep(1)
    return False


def validate_installed_artifact():
    source = (
        'from datetime import datetime,timezone; '
        'import adaptive_scenario_proposals as p; '
        'from adaptive_scenario_calibration import stamp; '
        'import json; '
        'value=p.load_artifact(); p.validate_artifact(value); '
        'assert stamp(value["created_at"]) <= datetime.now(timezone.utc), "Future calibration"; '
        'count=sum(row["status"]=="qualified" for row in value["contexts"].values()); '
        'assert count>0, "No qualified scenario policy"; '
        'assert not p.is_enabled(), "Activation preceded validation"; '
        'print(json.dumps(dict(sha256=value["sha256"],qualified_contexts=count)))'
    )
    return json.loads(command('/usr/sbin/runuser', '-u', 'patternlab', '--',
        str(ROOT/'.venv/bin/python'), '-B', '-c', source))


def deploy(bundle, expected, *, backup_root=None):
    require(os.geteuid() == 0, 'Root privileges required for reviewed rollout')
    manifest, payload = load_bundle(Path(bundle), expected)
    before = frozen()
    protected = protected_snapshot()
    resource_headroom(payload)
    worker_start = command('/usr/bin/systemctl', 'show', 'patternlab-worker.service',
                           '-p', 'ExecMainStartTimestampMonotonic', '--value')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    rollback = Path(backup_root or BACKUP_ROOT)/('adaptive-proposals-'+stamp+'-'+uuid4().hex[:6])
    rollback.mkdir(parents=True, mode=0o700)
    os.chmod(rollback, 0o700)
    (rollback/'manifest.json').write_text(json.dumps(manifest, indent=2))
    original_metadata = {}
    for name in FILES:
        target = target_path(name)
        if target.exists():
            stat = target.stat()
            original_metadata[name] = (stat.st_mode&0o777, (stat.st_uid, stat.st_gid))
            previous = rollback/'files'/name
            previous.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            shutil.copy2(target, previous)
    archives = archive_snapshot(rollback)
    try:
        # On an update, suspend activation while reviewed sources are replaced.
        # The old marker was backed up and is restored last on rollback.
        target_path(ENABLED).unlink(missing_ok=True)
        for name in (*SOURCES, ARTIFACT):
            metadata = original_metadata.get(name)
            atomic_write(target_path(name), payload[name],
                mode=metadata[0] if metadata else 0o644,
                owner=metadata[1] if metadata else None)
        require(all(digest(target_path(name)) == manifest['sha256'][name]
                    for name in (*SOURCES, ARTIFACT)), 'Installed source hash mismatch')
        after = frozen()
        require_protected_unchanged(protected)
        calibration = validate_installed_artifact()
        require_legacy_unchanged(archives)
        command('/usr/bin/systemctl', 'restart', 'patternlab-web.service')
        require(healthy(), 'Web health failed before activation')
        atomic_write(target_path(ENABLED), payload[ENABLED])
        require(healthy(), 'Web health failed after activation')
        require(all(digest(target_path(name)) == manifest['sha256'][name] for name in FILES),
                'Activated source hash mismatch')
        require_legacy_unchanged(archives)
        require_protected_unchanged(protected)
    except BaseException:
        target_path(ENABLED).unlink(missing_ok=True)
        for name in (*SOURCES, ARTIFACT, ENABLED):
            previous = rollback/'files'/name
            if previous.exists():
                mode, owner = original_metadata[name]
                atomic_write(target_path(name), previous.read_bytes(), mode=mode, owner=owner)
            elif manifest['previous_sha256'][name] is None:
                target_path(name).unlink(missing_ok=True)
        # Databases are deliberately not restored over any live worker appends.
        command('/usr/bin/systemctl', 'restart', 'patternlab-web.service')
        raise
    receipt = dict(state='adaptive_proposals_deployed', deployed_at=datetime.now(timezone.utc).isoformat(),
        before=before, after=after, source_sha256=manifest['sha256'], calibration=calibration,
        backup_directory=str(rollback), web_healthy=True, selection_enabled=True,
        original_records_preserved={name:len(rows) for name, rows in archives.items()},
        archive_backups={name:str(rollback/name) for name in archives if (rollback/name).exists()},
        application_database_writes=0, worker_restarted=False, training_performed=False, model_promoted=False,
        network_rules_changed=False, authentication_unchanged=True, club_and_context_sources_unchanged=True,
        timer_units_unchanged=True, worker_execution_sources_unchanged=True,
        worker_same_start=worker_start == command('/usr/bin/systemctl', 'show', 'patternlab-worker.service',
                                                  '-p', 'ExecMainStartTimestampMonotonic', '--value'))
    (rollback/'receipt.json').write_text(json.dumps(receipt, indent=2))
    (ROOT/'adaptive-proposals-upgrade-manifest.json').write_text(json.dumps(manifest, indent=2))
    return receipt


def main():
    require(len(sys.argv) == 3, 'Usage: install_adaptive_proposals.py BUNDLE EXPECTED_SHA256')
    print(json.dumps(deploy(Path(sys.argv[1]), sys.argv[2])))


if __name__ == '__main__':
    main()
