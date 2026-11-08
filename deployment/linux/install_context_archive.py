"""Add an isolated context archive job without changing serving or model code."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import stat
import sys
import tarfile

ROOT=Path('/opt/patternlab')
PYTHON_FILES=(
    'context_observation_store.py','context_archive_sync.py','context_event_quality.py',
    'prematch_context_quality.py','context_quality_audit.py','scripts/sync_context_archive.py',
    'deployment/linux/run_context_archive.py',
)
UNITS=('patternlab-context-archive.service','patternlab-context-archive.timer')
FILES=(*PYTHON_FILES, *(f'deployment/linux/{name}' for name in UNITS))
MANIFEST='context-archive-execution-manifest.json'


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def command(*args):
    return subprocess.run(args,check=True,capture_output=True,text=True).stdout.strip()


def _safe_parent(root, path):
    root, path = Path(root).absolute(), Path(path).absolute()
    if root not in path.parents:
        raise ValueError('Installer destination is outside its protected root.')
    for parent in path.parents:
        if parent.exists() or parent.is_symlink():
            info = parent.lstat()
            if (parent.is_symlink() or not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != 0 or info.st_mode & 0o022):
                raise ValueError('Installer destination directory is unsafe.')
        if parent == root:
            break


def preserved():
    evidence=json.loads((ROOT/'audit/PATTERNLAB2_PRESERVATION_LOT14_2026-11-02.json').read_text())
    candidate=evidence['candidate_integrity']
    controls={**evidence['frozen_source_sha256'],**candidate['implementation_sha256']}
    for name, expected in controls.items():
        if digest(ROOT/name)!=expected:raise ValueError('A frozen source changed.')
    candidate_root=ROOT/'data/api_football/causal_confirmation/candidates'/candidate['candidate_id']
    for name, expected in candidate['artifact_sha256'].items():
        if digest(candidate_root/name)!=expected:raise ValueError('Frozen candidate changed.')
    overlay=json.loads((ROOT/'linux-kit-overlay-manifest.json').read_text())
    for row in overlay['files']:
        if digest(ROOT/row['path'])!=row['sha256']:raise ValueError('Preserved overlay changed.')
    worker=json.loads((ROOT/'worker-scoped-io-manifest.json').read_text())
    paths=set(worker['source_sha256'])|set(worker['preserved_dependency_sha256'])
    hashes={name:digest(ROOT/name) for name in paths}
    return dict(protected_sources=len(controls),candidate_unchanged=True,
                worker_sha256=hashes,overlay_files=len(overlay['files']))


def main():
    if os.geteuid()!=0:raise PermissionError('Root installer required.')
    bundle,expected=Path(sys.argv[1]),sys.argv[2]
    if digest(bundle)!=expected:raise ValueError('Archive digest mismatch.')
    with tarfile.open(bundle) as archive:
        members=archive.getmembers()
        if ({m.name for m in members}!=set(FILES)|{'manifest.json'} or
                len(members)!=len(FILES)+1 or not all(m.isfile() for m in members)):
            raise ValueError('Unexpected installer payload.')
        manifest=json.load(archive.extractfile('manifest.json'))
        payload={name:archive.extractfile(name).read() for name in FILES}
    if (manifest.get('version')!='context-archive-execution-v1'
            or manifest.get('tests_passed') is not True or set(manifest.get('sha256',{}))!=set(FILES)):
        raise ValueError('Unqualified context archive.')
    for name,data in payload.items():
        if hashlib.sha256(data).hexdigest()!=manifest['sha256'][name]:
            raise ValueError('Payload integrity failure.')
        if name.endswith('.py'):compile(data,name,'exec')
        _safe_parent(ROOT, ROOT/name)
        if (ROOT/name).exists() or (ROOT/name).is_symlink():
            raise ValueError('Additive installer refuses an existing application file.')
    for name in UNITS:
        _safe_parent(Path('/etc/systemd/system'), Path('/etc/systemd/system')/name)
        if (Path('/etc/systemd/system')/name).exists():
            raise ValueError('Additive installer refuses an existing unit.')
    if (ROOT/MANIFEST).exists():raise ValueError('Context archive already installed.')
    _safe_parent(ROOT, ROOT/MANIFEST)
    before=preserved()
    service_starts={name:command('/usr/bin/systemctl','show',name,'-p','ExecMainStartTimestampMonotonic','--value')
                    for name in ('patternlab-worker.service','patternlab-web.service')}
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    receipt_root=Path('/var/lib/patternlab-cutover')/('context-archive-'+stamp)
    receipt_root.mkdir(mode=0o700)
    (receipt_root/'manifest.json').write_text(json.dumps(manifest,indent=2))
    installed=[]
    try:
        for name,data in payload.items():
            target=ROOT/name
            target.parent.mkdir(parents=True,exist_ok=True)
            with target.open('xb') as stream:
                stream.write(data);stream.flush();os.fsync(stream.fileno())
            os.chmod(target,0o644);installed.append(target)
        execution=dict(version=manifest['version'],tests_passed=True,
            source_sha256={name:manifest['sha256'][name] for name in PYTHON_FILES},
            qualification=manifest['qualification'])
        with (ROOT/MANIFEST).open('x') as stream:json.dump(execution,stream,indent=2)
        os.chmod(ROOT/MANIFEST,0o644);installed.append(ROOT/MANIFEST)
        for name in UNITS:
            target=Path('/etc/systemd/system')/name
            with target.open('xb') as stream:stream.write(payload['deployment/linux/'+name])
            os.chmod(target,0o644);installed.append(target)
        after=preserved()
        if before!=after:raise ValueError('Preserved implementation changed.')
        command('/usr/bin/systemctl','daemon-reload')
        command('/usr/bin/systemctl','start','--no-block','patternlab-context-archive.service')
    except BaseException:
        # No archive rows are deleted by rollback. The isolated unit is stopped;
        # only files added by this installer are removed.
        subprocess.run(['/usr/bin/systemctl','stop','patternlab-context-archive.service'],capture_output=True)
        for target in installed:target.unlink(missing_ok=True)
        subprocess.run(['/usr/bin/systemctl','daemon-reload'],capture_output=True)
        raise
    receipt=dict(state='context_archive_installed_catchup_started',
        installed_at=datetime.now(timezone.utc).isoformat(),execution=execution,
        before=before,after=after,receipt_directory=str(receipt_root),
        timer_enabled=False,model_training=False,serving_model_changed=False,
        main_services_restarted=False,network_access_added=False,
        source_database_writes=0,
        main_services_same_start={name:service_starts[name]==command('/usr/bin/systemctl','show',name,
            '-p','ExecMainStartTimestampMonotonic','--value') for name in service_starts})
    (receipt_root/'receipt.json').write_text(json.dumps(receipt,indent=2))
    print(json.dumps(receipt))


if __name__=='__main__':
    main()
