"""Same-host offline restoration of saved code/data and an independent venv.

Explicit maintenance tool: it executes a recovery point only when its expected
manifest hash is supplied. It never installs launchd, copies private credentials
or replaces live data. The restored worker is a bounded, offline exercise.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
import tomllib
from urllib.request import urlopen

import local_recovery as recovery
import offline_environment as environment


VERSION='same-host-service-restoration-v1'
PROJECT=Path(__file__).resolve().parent


def clean_environment():
    # A narrow child environment avoids inherited API, OIDC, cloud or Python
    # search-path settings. HOME is not redirected or repurposed.
    value={name:os.environ[name] for name in ('PATH','LANG','LC_CTYPE','TMPDIR','SYSTEMROOT') if name in os.environ}
    value.update(PATTERNLAB_STORAGE_MODE='local',PYTHONDONTWRITEBYTECODE='1',
                 STREAMLIT_BROWSER_GATHER_USAGE_STATS='false')
    return value


def _destination(value):
    given=Path(value)
    if given.exists() or given.is_symlink():raise ValueError('Choisir un nouveau dossier de restauration.')
    dest=given.resolve()
    if (PROJECT/'.local_backups').is_symlink():raise ValueError('Le dossier privé ne peut pas être un lien.')
    # Executable copies belong in the private drill area, never active data.
    if not dest.is_relative_to(PROJECT/'.local_backups'):
        raise ValueError('La restauration de service exige un nouveau dossier sous .local_backups.')
    return dest


def restore_files(point,destination,expected_manifest_sha256):
    point=Path(point).resolve();destination=_destination(destination)
    if recovery.fingerprint(point/'manifest.json')!=expected_manifest_sha256:
        raise ValueError('Le point ne correspond pas à l’empreinte attendue.')
    verification=recovery.verify_recovery_point(point)
    if verification.get('known_private_paths_excluded') is not True:
        raise ValueError('Le point contient des chemins privés connus.')
    manifest=json.loads((point/'manifest.json').read_text());items=recovery._manifest_items(manifest)
    names={i['path'] for i in items}
    if not {'app.py','scripts/automation_service.py','data/api_football/fixtures.sqlite3',
            'data/api_football/predictions.sqlite3','data/api_football/causal_confirmation.sqlite3'}<=names:
        raise ValueError('Le point ne contient pas les composants nécessaires au démarrage.')
    destination.mkdir(parents=True,exist_ok=False,mode=0o700)
    for item in items:
        source=point/'files'/item['path'];target=environment._path(destination,item['path'])
        target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
        if recovery.fingerprint(target)!=item['sha256']:
            raise ValueError('Un fichier restauré ne correspond plus au point vérifié.')
    if recovery.fingerprint(point/'manifest.json')!=expected_manifest_sha256:
        raise ValueError('Le manifeste a changé pendant la restauration.')
    return destination,verification


GUARD = '''import socket
_connect=socket.socket.connect
_connect_ex=socket.socket.connect_ex
_resolve=socket.getaddrinfo
def _allowed(address):
    return isinstance(address,tuple) and address[0] in ('127.0.0.1','::1','localhost')
def _guard_connect(self,address):
    if not _allowed(address):raise RuntimeError('Offline restoration: external connection refused')
    return _connect(self,address)
def _guard_connect_ex(self,address):
    if not _allowed(address):raise RuntimeError('Offline restoration: external connection refused')
    return _connect_ex(self,address)
def _guard_resolve(host,*args,**kwargs):
    if host not in (None,'127.0.0.1','::1','localhost'):raise RuntimeError('Offline restoration: DNS refused')
    return _resolve(host,*args,**kwargs)
socket.socket.connect=_guard_connect
socket.socket.connect_ex=_guard_connect_ex
socket.getaddrinfo=_guard_resolve
'''


APP_PROBE = '''from pathlib import Path
import json,sys
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
from unittest.mock import patch
from streamlit.testing.v1 import AppTest
import api_football as api,catboost
assert Path(api.__file__).resolve().parent==root
with patch.object(api,'load_key',side_effect=AssertionError('No credentials')) as key, \\
     patch.object(api.ApiFootballClient,'get',side_effect=AssertionError('No API')) as network, \\
     patch.object(catboost.CatBoostClassifier,'fit',side_effect=AssertionError('No training')) as fit:
    at=AppTest.from_file(str(root/'app.py'),default_timeout=90).run()
    assert not at.exception
    pages=[dict(page='app.py',tables=len(at.dataframe))]
    at.switch_page('ui_pages/journal.py').run()
    assert not at.exception
    pages.append(dict(page='ui_pages/journal.py',tables=len(at.dataframe)))
    key.assert_not_called();network.assert_not_called();fit.assert_not_called()
    result=dict(state='passed',pages=pages,api_calls=0,credential_reads=0,fits=0,headless=True)
(root/'.restoration/app_probe.json').write_text(json.dumps(result,indent=2))
'''


def _run(arguments,root,timeout=180,*,label='subprocess'):
    log=root/'.restoration'/f'{label}.log'
    try:
        result=subprocess.run(arguments,cwd=root,env=clean_environment(),capture_output=True,text=True,timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        log.write_text(str(exc.stdout or '')+'\n'+str(exc.stderr or '')+'\nTIMEOUT\n')
        raise
    log.write_text(result.stdout+'\n'+result.stderr)
    if result.returncode:
        raise ValueError('Un contrôle du service restauré a échoué ; consulter les fichiers locaux de l’exercice.')
    return result


def server_probe(root,python,*,timeout=40):
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1',0));port=reserve.getsockname()[1]
    command=[str(python),'-m','streamlit','run','app.py','--server.address=127.0.0.1',
        f'--server.port={port}','--server.headless=true','--server.fileWatcherType=none',
        '--browser.gatherUsageStats=false']
    process=None;health=False;started=time.monotonic()
    with (root/'.restoration/streamlit.log').open('w') as log:
        try:
            process=subprocess.Popen(command,cwd=root,env=clean_environment(),stdout=log,stderr=subprocess.STDOUT)
            while time.monotonic()-started<timeout:
                if process.poll() is not None:break
                try:
                    with urlopen(f'http://127.0.0.1:{port}/_stcore/health',timeout=1) as response:
                        health=response.status==200 and response.read().strip()==b'ok'
                    if health:break
                except (OSError,ValueError):pass
                time.sleep(.2)
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
    if not health:raise ValueError('Le serveur restauré n’a pas confirmé sa disponibilité locale.')
    return dict(state='passed',address='127.0.0.1',port=port,health_http_status=200,
                pid=process.pid,process_stopped=process.poll() is not None,
                only_health_endpoint_tested=True)


def rehearse(point,bundle,destination,*,expected_manifest_sha256,expected_environment_sha256):
    root,verification=restore_files(point,destination,expected_manifest_sha256)
    control=root/'.restoration';control.mkdir(mode=0o700)
    recovery._write_json_atomic(root/'.restoration_sandbox.json',dict(version='local-service-restoration-sandbox-v1',
        project_root=str(root),offline=True,production=False))
    env=environment.rebuild(bundle,root/'.venv',expected_environment_sha256)
    python=root/'.venv/bin/python'
    site=root/'.venv/lib'/f'python{os.sys.version_info.major}.{os.sys.version_info.minor}'/'site-packages'
    (site/'restoration_guard.py').write_text(GUARD)
    (site/'restoration_guard.pth').write_text('import restoration_guard\n')
    # No production key or OIDC settings are copied; only a synthetic local profile.
    (root/'.streamlit').mkdir(exist_ok=True)
    profile=root/'.streamlit/secrets.toml'
    profile.write_text('PATTERNLAB_STORAGE_MODE = "local"\n[access]\nlocal_owner = true\n');profile.chmod(0o600)
    config=root/'.streamlit/config.toml'
    prior_config=config.read_text() if config.exists() else ''
    if 'secrets' in tomllib.loads(prior_config):
        raise ValueError('La configuration sauvegardée contient déjà une section secrets à examiner.')
    config.write_text(prior_config+f'\n[secrets]\nfiles = [{json.dumps(str(profile))}]\n')
    probe=PROJECT/'restored_service_probe.py';shutil.copy2(probe,root/probe.name)
    (control/'app_probe.py').write_text(APP_PROBE)
    return _check_prepared(root,env,verification,expected_manifest_sha256,expected_environment_sha256)


def _check_prepared(root,env,verification,expected_manifest_sha256,expected_environment_sha256):
    """Execute checks on the isolated workspace prepared by rehearse()."""
    control=root/'.restoration';python=root/'.venv/bin/python'
    environment_check=_run([str(python),'-I','-c',
        'import importlib.metadata as m,json,sys,streamlit,pandas,numpy,scipy,catboost,psycopg;'
        'print(json.dumps({"prefix":sys.prefix,"packages":sorted([{ "name":d.metadata["Name"],"version":d.version} for d in m.distributions()],key=lambda d:d["name"].casefold()),'
        '"module_origins":[x.__file__ for x in (streamlit,pandas,numpy,scipy,catboost,psycopg)]}))'],root,timeout=90,label='imports')
    imported=json.loads(environment_check.stdout)
    if Path(imported['prefix']).resolve()!=(root/'.venv').resolve() or imported['packages']!=env['distributions']:
        raise ValueError('Les dépendances restaurées ne correspondent pas à leur inventaire.')
    if not all(Path(p).resolve().is_relative_to(root/'.venv') for p in imported['module_origins']):
        raise ValueError('Une dépendance provient de l’environnement actif.')
    server=server_probe(root,python)
    _run([str(python),str(control/'app_probe.py')],root,timeout=180,label='application')
    _run([str(python),str(root/'restored_service_probe.py'),'--project-root',str(root)],root,timeout=240,label='worker')
    app=json.loads((control/'app_probe.json').read_text());service=json.loads((control/'probe.json').read_text())
    if app['state']!='passed' or service['state']!='passed':raise ValueError('Les contrôles restaurés ne sont pas tous concluants.')
    return dict(version=VERSION,checked_at=datetime.now(timezone.utc).isoformat(),state='passed_offline_same_host',
                restoration_root=str(root),recovery_manifest_sha256=expected_manifest_sha256,
                environment_manifest_sha256=expected_environment_sha256,verification=verification,
                environment=env,imported_environment=imported,server=server,application=app,worker=service,
                harness_sha256={name:recovery.fingerprint(PROJECT/name) for name in
                    ('service_restoration.py','offline_environment.py','restored_service_probe.py')},
                production_restored=False,production_service_restarted=False,private_credentials_copied=False,
                different_machine_tested=False,live_provider_tested=False,launchd_installed=False,
                complete_service_disaster_recovery_demonstrated=False,
                limits=['Same host, compatible system Python/native libraries still required.',
                    'Venv rehydrated from installed package bytes, not reinstalled from wheels or a package index.',
                    'Saved code version is the selected recovery point; controlled offline profile and guard added.',
                    'HTTP health and two headless pages checked; not a browser/mobile audit.',
                    'Worker uses retained calendar replay and a declared pause boundary, not a fresh provider response or a full forecast queue.',
                    'Real private profile, Google sign-in, off-machine backup and full external service recovery remain untested.'])
