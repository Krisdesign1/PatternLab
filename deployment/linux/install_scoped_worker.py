"""Install a new qualified entrypoint; preserve all reference sources and records."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

ROOT=Path('/opt/patternlab')
SOURCES=('automation_worker_scoped.py','worker_fixture_io.py','candidate_tracking_io.py',
         'daily_analysis_worker_io.py','deployment/linux/run_scoped_worker.py')
DROPIN='deployment/linux/patternlab-scoped-io.conf'
FILES=(*SOURCES,DROPIN)
MANIFEST=ROOT/'worker-scoped-io-manifest.json'

def digest(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def command(*args):
    return subprocess.run(args,check=True,capture_output=True,text=True).stdout.strip()

def target(name):
    return (Path('/etc/systemd/system/patternlab-worker.service.d/zz-scoped-io.conf')
            if name==DROPIN else ROOT/name)

def preserve():
    evidence=json.loads((ROOT/'audit/PATTERNLAB2_PRESERVATION_LOT14_2026-11-02.json').read_text())
    candidate=evidence['candidate_integrity']
    sources={**evidence['frozen_source_sha256'],**candidate['implementation_sha256']}
    for name,expected in sources.items():assert digest(ROOT/name)==expected,'Protected source changed'
    directory=ROOT/'data/api_football/causal_confirmation/candidates'/candidate['candidate_id']
    for name,expected in candidate['artifact_sha256'].items():
        assert digest(directory/name)==expected,'Candidate artifact changed'
    overlay=json.loads((ROOT/'linux-kit-overlay-manifest.json').read_text())
    for item in overlay['files']:assert digest(ROOT/item['path'])==item['sha256'],'Original overlay changed'
    for name in ('candidate_tracking.py','daily_analysis_worker.py','daily_analysis_engine.py',
                 'deployment/linux/run_guarded_worker.py','season_intake.py'):
        sources[name]=digest(ROOT/name)
    return sources

def main():
    assert os.geteuid()==0
    os.chdir(ROOT)
    bundle,expected=Path(sys.argv[1]),sys.argv[2]
    assert digest(bundle)==expected,'Bundle differs'
    assert command('systemctl','show','patternlab-worker.service','-p','MainPID','--value')=='0','Active writer'
    assert subprocess.run(['systemctl','is-active','--quiet','patternlab-worker.timer']).returncode != 0,'Timer still active'
    with tarfile.open(bundle) as archive:
        members=archive.getmembers()
        assert len(members)==len(FILES)+1 and all(item.isfile() for item in members)
        assert {item.name for item in members}==set(FILES)|{'manifest.json'}
        manifest=json.load(archive.extractfile('manifest.json'))
        payload={name:archive.extractfile(name).read() for name in FILES}
    assert manifest['version']=='worker-scoped-io-v1' and manifest['qualification']['tests_passed'] is True
    assert set(manifest['source_sha256'])==set(SOURCES)
    for name,data in payload.items():
        expected=manifest['dropin_sha256'] if name==DROPIN else manifest['source_sha256'][name]
        assert hashlib.sha256(data).hexdigest()==expected,'Unqualified source'
        if name.endswith('.py'):compile(data,name,'exec')
        assert not target(name).is_symlink() and not target(name).exists(),'New execution file already exists'
    assert not MANIFEST.exists() and not MANIFEST.is_symlink()
    dependencies=preserve()
    assert dependencies==manifest['preserved_dependency_sha256'],'Dependency qualification differs'
    auth_paths=(ROOT/'admin_access.py',ROOT/'.streamlit/secrets.toml',
                Path('/etc/systemd/system/patternlab-web.service.d/google-auth.conf'))
    auth={str(path):digest(path) for path in auth_paths}
    web_start=command('systemctl','show','patternlab-web.service','-p','ExecMainStartTimestampMonotonic','--value')
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    rollback=Path('/var/lib/patternlab-cutover')/('worker-scoped-'+stamp)
    rollback.mkdir(mode=0o700)
    (rollback/'manifest.json').write_text(json.dumps(manifest,indent=2))
    try:
        for name,data in payload.items():
            path=target(name)
            temporary=path.with_name(path.name+'.scoped-new')
            with temporary.open('xb') as stream:
                stream.write(data);stream.flush();os.fsync(stream.fileno())
            os.chmod(temporary,0o644);os.replace(temporary,path)
        MANIFEST.write_text(json.dumps(manifest,indent=2));os.chmod(MANIFEST,0o644)
        assert preserve()==dependencies
        assert all(digest(Path(name))==value for name,value in auth.items())
        command('systemctl','daemon-reload')
        execution=command('systemctl','show','patternlab-worker.service','-p','ExecStart','--value')
        assert '/opt/patternlab/deployment/linux/run_scoped_worker.py' in execution,'Execution override precedence invalid'
        assert '/opt/patternlab/deployment/linux/run_guarded_worker.py' not in execution,'Old execution still selected'
        checked=command('runuser','-u','patternlab','--',str(ROOT/'.venv/bin/python'),'-c',
            'from deployment.linux.run_scoped_worker import verify_execution; verify_execution(); print("verified")')
        assert checked=='verified'
        command('systemctl','reset-failed','patternlab-worker.service')
        command('systemctl','start','patternlab-worker.timer')
        command('systemctl','start','--no-block','patternlab-worker.service')
    except BaseException:
        # No source/protocol is reverted or edited. Remove only this entrypoint
        # when activation has not started, leaving the reference deployment.
        pid=command('systemctl','show','patternlab-worker.service','-p','MainPID','--value')
        if pid=='0':
            for name in FILES:target(name).unlink(missing_ok=True)
            MANIFEST.unlink(missing_ok=True)
            command('systemctl','daemon-reload')
            command('systemctl','start','patternlab-worker.timer')
        raise
    receipt=dict(state='scoped_worker_deployed',deployed_at=datetime.now(timezone.utc).isoformat(),
        version=manifest['version'],manifest_sha256=digest(MANIFEST),
        preserved_dependency_count=len(dependencies),candidate_unchanged=True,
        original_overlay_unchanged=True,authentication_unchanged=True,
        application_database_writes_by_installer=0,training=False,model_promoted=False,
        network_rules_changed=False,rollback_directory=str(rollback),
        web_same_start=web_start==command('systemctl','show','patternlab-web.service','-p','ExecMainStartTimestampMonotonic','--value'),
        service=command('systemctl','show','patternlab-worker.service','-p','MainPID','-p','ActiveState','-p','SubState'),
        timer=command('systemctl','is-active','patternlab-worker.timer'))
    (rollback/'receipt.json').write_text(json.dumps(receipt,indent=2))
    print(json.dumps(receipt))

if __name__=='__main__':main()
