"""Install date-exact calendar downloads without changing stored results or forecasts."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time

ROOT = Path('/opt/patternlab')
FILES = ('calendar_export_data.py', 'calendar_export_formats.py', 'calendar_export_panel.py', 'calendar_panel.py')


def target_path(name):
    return ROOT/name


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def frozen():
    evidence = json.loads((ROOT/'audit/PATTERNLAB2_PRESERVATION_LOT14_2026-11-02.json').read_text())
    candidate = evidence['candidate_integrity']
    sources = {**evidence['frozen_source_sha256'], **candidate['implementation_sha256']}
    for name, expected in sources.items():
        assert digest(ROOT/name) == expected, 'Frozen source mismatch'
    directory = ROOT/'data/api_football/causal_confirmation/candidates'/candidate['candidate_id']
    for name, expected in candidate['artifact_sha256'].items():
        assert digest(directory/name) == expected, 'Candidate mismatch'
    overlay = json.loads((ROOT/'linux-kit-overlay-manifest.json').read_text())
    for item in overlay['files']:
        assert digest(ROOT/item['path']) == item['sha256'], 'Original overlay mismatch'
    return dict(protected_sources=len(sources), overlay_files=len(overlay['files']),
                candidate_unchanged=True)


def healthy():
    for _ in range(25):
        result = subprocess.run(['/usr/bin/curl', '-fsS', '--max-time', '2',
                                 'http://127.0.0.1:8501/_stcore/health'], capture_output=True)
        if result.returncode == 0 and result.stdout.strip() == b'ok':
            return True
        time.sleep(1)
    return False


def main():
    assert os.geteuid() == 0
    bundle, expected = Path(sys.argv[1]), sys.argv[2]
    assert digest(bundle) == expected, 'Bundle hash mismatch'
    with tarfile.open(bundle) as archive:
        members = archive.getmembers()
        assert len(members) == len(FILES)+1 and all(member.isfile() for member in members)
        assert {member.name for member in members} == set(FILES)|{'manifest.json'}
        manifest = json.load(archive.extractfile('manifest.json'))
        assert set(manifest['sha256']) == set(FILES)
        payload = {name: archive.extractfile(name).read() for name in FILES}
    for name, data in payload.items():
        assert hashlib.sha256(data).hexdigest() == manifest['sha256'][name]
        if name.endswith('.py'):
            compile(data, name, 'exec')
        target = target_path(name)
        assert not target.is_symlink()
        expected_old = manifest['previous_sha256'].get(name)
        assert (digest(target) if target.exists() else None) == expected_old, 'Unreviewed existing file'
    before = frozen()
    worker_control = json.loads((ROOT/'worker-scoped-io-manifest.json').read_text())
    worker_hashes = {name:digest(ROOT/name) for name in (*worker_control['source_sha256'], *worker_control['preserved_dependency_sha256'])}
    archive_sources = {name:digest(ROOT/name) for name in ('manual_analysis_store.py','prediction_journal.py','scenario_outcomes.py','bet_simulation.py','manual_bet_store.py')}
    auth_before = {name:digest(Path(name)) for name in ('/opt/patternlab/admin_access.py', '/opt/patternlab/.streamlit/secrets.toml', '/etc/systemd/system/patternlab-web.service.d/google-auth.conf')}
    worker_start = command('/usr/bin/systemctl','show','patternlab-worker.service','-p','ExecMainStartTimestampMonotonic','--value')
    available = next(int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:'))
    assert available >= 2*1024*1024, 'Not enough memory headroom for web restart'
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    rollback = Path('/var/lib/patternlab-cutover')/('calendar-export-'+stamp)
    rollback.mkdir(mode=0o700)
    (rollback/'manifest.json').write_text(json.dumps(manifest, indent=2))
    try:
        for name, data in payload.items():
            target = target_path(name)
            if target.exists():
                previous = rollback/'files'/name
                previous.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                shutil.copy2(target, previous)
            temporary = target.with_name(target.name+'.calendar-export-new')
            with temporary.open('xb') as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            os.chmod(temporary, 0o644)
            os.replace(temporary, target)
        after = frozen()
        assert all(digest(ROOT/name)==value for name,value in worker_hashes.items()), 'Worker source changed'
        assert all(digest(ROOT/name)==value for name,value in archive_sources.items()), 'Settlement/archive source changed'
        assert all(digest(Path(name))==value for name,value in auth_before.items()), 'Authentication changed'
        command('/usr/bin/systemctl', 'daemon-reload')
        command('/usr/bin/systemctl', 'restart', 'patternlab-web.service')
        assert healthy(), 'Web health failed'
    except BaseException:
        for name in FILES:
            previous = rollback/'files'/name
            if previous.exists():
                shutil.copy2(previous, target_path(name))
            elif manifest['previous_sha256'].get(name) is None:
                target_path(name).unlink(missing_ok=True)
        command('/usr/bin/systemctl', 'daemon-reload')
        command('/usr/bin/systemctl', 'restart', 'patternlab-web.service')
        raise
    receipt = dict(state='calendar_export_deployed', deployed_at=datetime.now(timezone.utc).isoformat(),
        before=before, after=after, source_sha256=manifest['sha256'],
        backup_directory=str(rollback), web_healthy=True,
        application_database_writes=0,
        worker_restarted=False, training_performed=False, model_promoted=False,
        network_rules_changed=False, authentication_unchanged=True,
        archive_settlement_sources_unchanged=True, worker_execution_sources_unchanged=True,
        worker_same_start=worker_start==command('/usr/bin/systemctl','show','patternlab-worker.service','-p','ExecMainStartTimestampMonotonic','--value'),
        scheduling_weights=command('/usr/bin/systemctl','show','patternlab-web.service','-p','CPUWeight','-p','IOWeight'))
    (rollback/'receipt.json').write_text(json.dumps(receipt, indent=2))
    (ROOT/'calendar-export-upgrade-manifest.json').write_text(json.dumps(manifest, indent=2))
    print(json.dumps(receipt))


if __name__ == '__main__':
    main()
