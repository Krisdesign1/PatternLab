"""Offline startup probe executed only inside a marked restored workspace.

The restored code and independent virtual environment are exercised directly.
No real key, external connection, production service or launchd registration is
used. A bounded worker pass replays one retained calendar, then pauses before
capture/inference phases. Its writes belong only to the disposable restoration.
"""
from contextlib import ExitStack, closing
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import argparse
import json
import math
import os
from pathlib import Path
import re
import socket
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import patch


VERSION = 'restored-service-probe-v1'
MARKER_VERSION = 'local-service-restoration-sandbox-v1'
SCRIPT_ROOT = Path(__file__).resolve().parent


class ProbeError(ValueError):
    pass


def _safe_path(root, relative):
    value = root
    for part in Path(relative).parts:
        value = value/part
        if value.is_symlink():
            raise ProbeError('sandbox_path_invalid')
    if not value.resolve().is_relative_to(root):
        raise ProbeError('sandbox_path_invalid')
    return value


def validate_sandbox(project_root):
    """Fail before importing application code outside the approved sandbox."""
    given = Path(project_root)
    if given.is_symlink():
        raise ProbeError('sandbox_root_invalid')
    root = given.resolve()
    if root != SCRIPT_ROOT.resolve() or Path.cwd().resolve() != root:
        raise ProbeError('sandbox_location_mismatch')
    marker = _safe_path(root, '.restoration_sandbox.json')
    if not marker.is_file():
        raise ProbeError('sandbox_marker_missing')
    value = json.loads(marker.read_text(encoding='utf-8'))
    if (not isinstance(value, dict) or value.get('version') != MARKER_VERSION
            or value.get('project_root') != str(root) or value.get('offline') is not True
            or value.get('production') is not False):
        raise ProbeError('sandbox_marker_invalid')
    if Path(sys.prefix).resolve() != (root/'.venv').resolve():
        raise ProbeError('independent_environment_required')
    if os.environ.get('PATTERNLAB_STORAGE_MODE') != 'local':
        raise ProbeError('explicit_local_storage_required')
    for name in ('API_FOOTBALL_KEY', 'PATTERNLAB_DATABASE_URL', 'PATTERNLAB_CLOUD_REQUIRED',
                 'PATTERNLAB_CLOUD_ADOPT_LOCAL'):
        if os.environ.get(name):
            raise ProbeError('private_environment_not_allowed')
    # This profile is synthetic and contains no real API/OIDC/database access.
    import tomllib
    profile = tomllib.loads(_safe_path(root,'.streamlit/secrets.toml').read_text(encoding='utf-8'))
    if profile != {'PATTERNLAB_STORAGE_MODE':'local', 'access':{'local_owner':True}}:
        raise ProbeError('synthetic_profile_required')
    return root


def _origin(module, root):
    if not Path(module.__file__).resolve().is_relative_to(root):
        raise ProbeError('application_import_outside_sandbox')


def _candidate_replay(root):
    """Load the restored binary and reproduce one existing frozen prediction."""
    import causal_confirmation_store as store
    import causal_candidate as candidates
    heads = store.heads(root/'data/api_football')
    if not heads:
        return dict(state='missing', reason='retained_candidate_forecast_missing')
    selected = heads[0]['payload']
    identity = selected['id']
    with closing(store._connect(root/'data/api_football')) as con:
        row = con.execute('SELECT * FROM forecasts WHERE id=?',(identity,)).fetchone()
        payload = store._forecast(con,row)['payload']
    candidate_id=payload['candidate_id']
    if not isinstance(candidate_id,str) or re.fullmatch('[a-f0-9]{64}',candidate_id) is None:
        raise ProbeError('candidate_identity_invalid')
    directory=_safe_path(root,Path('data/api_football/causal_confirmation/candidates')/candidate_id)
    saved = candidates.load_candidate(directory)
    result = candidates.predict_candidate(saved,[payload['snapshot']['features']])[0]
    expected = payload['methods']['candidate']
    if set(result) != set(expected) or any(not math.isfinite(result[k]) for k in result):
        raise ProbeError('candidate_prediction_invalid')
    difference = max(abs(result[k]-expected[k]) for k in result)
    return dict(state='passed' if difference <= 1e-12 else 'failed',
                forecast_id=identity,candidate_id=payload['candidate_id'],
                max_absolute_probability_difference=difference,model_binary_loaded=True,
                new_forecast_published=False,predictive_evaluation=False)


def _offline_cycle(root, api, worker, service):
    """Run the actual worker through its startup/calendar/pause/finally path."""
    import api_match_workspace as calendar
    from zoneinfo import ZoneInfo
    now = datetime.now(timezone.utc)
    day = now.astimezone(ZoneInfo(worker.TIMEZONE)).date()
    raw = calendar.local_day_matches(day, path=root/'data/api_football/fixtures.json')
    state_path=root/'data/api_football/automation_state.json'
    prior=state_path.read_bytes() if state_path.exists() else b''
    state=worker._read(state_path,{})
    gate_fields=('quota','minute_calls','cooldown_until','budget_day','requests_today','quota_seen_at')
    removed=[name for name in gate_fields if name in state]
    for name in removed:
        state.pop(name)
    # Recorded provider cooldowns/quotas are not permissions for this offline
    # replay. Clear only these gate fields in the disposable restored copy;
    # queues, plans and actual production state are retained elsewhere.
    worker._write(state_path,state)

    class OfflineClient:
        quota = {}
        def __init__(self):
            self.calls = []
        def fixtures(self, **params):
            self.calls.append(dict(params))
            if len(self.calls) == 1 and params == {'date':day.isoformat(),'timezone':worker.TIMEZONE}:
                return dict(response=deepcopy(raw), results=len(raw), errors=[], paging=dict(current=1,total=1))
            raise worker._Pause('Offline restored exercise boundary; retained calendar only.')

    client = OfflineClient()
    output = {}
    def run_cycle(**kwargs):
        if Path(kwargs['root']).resolve() != root/'data/api_football':
            raise ProbeError('worker_root_mismatch')
        output.update(worker.run_cycle(**kwargs,client=client))
        return output
    config = dict(worker.DEFAULT_CONFIG,enabled=True,models_enabled=False,history_enabled=False)
    def boundary(*_args,**_kwargs):
        raise worker._Pause('Offline restored exercise boundary; capture excluded.')
    def forbidden(*_args,**_kwargs):
        raise ProbeError('forecast_or_training_forbidden')
    with ExitStack() as guards:
        # A successful initial result pass can otherwise enter capture directly.
        # This is a declared probe boundary, not a fake successful capture.
        guards.enter_context(patch.object(worker,'_track_causal_confirmation',side_effect=boundary))
        for name in ('_forecast','_track_candidates','_prepare_daily_learning',
                     '_track_daily_learning','_track_argumented_selection'):
            guards.enter_context(patch.object(worker,name,side_effect=forbidden))
        summary = service._run_once(SimpleNamespace(run_cycle=run_cycle),root,
            dry_run=False,config=config,skip_models=True,max_duration_seconds=10)
    return dict(state='passed' if summary['status']=='paused' and len(client.calls)>=1
                and output.get('day_service',{}).get('calendar_at') else 'failed',
                worker_status=summary['status'],provider_mode='retained_calendar_replay_only',
                provider_attempts=len(client.calls),retained_calendar_items=len(raw),
                calendar_completed=bool(output.get('day_service',{}).get('calendar_at')),
                reconciliation_entered='causal_confirmation_results' in output,
                seeded_offline_gate=True,removed_gate_fields=removed,
                original_restored_state_sha256=sha256(prior).hexdigest(),
                full_forecast_queue_executed=False,models_prepared=False,
                production_service_registered=False)


def probe(project_root):
    root = validate_sandbox(project_root)
    counters = dict(network_attempts=0,credential_attempts=0,fit_attempts=0)
    def blocked_network(*_args,**_kwargs):
        counters['network_attempts'] += 1
        raise ProbeError('network_forbidden')
    def blocked_key(*_args,**_kwargs):
        counters['credential_attempts'] += 1
        raise ProbeError('credential_access_forbidden')
    def blocked_fit(*_args,**_kwargs):
        counters['fit_attempts'] += 1
        raise ProbeError('training_forbidden')
    checks=[]
    with ExitStack() as guards:
        guards.enter_context(patch.object(socket.socket,'connect',side_effect=blocked_network))
        guards.enter_context(patch.object(socket.socket,'connect_ex',side_effect=blocked_network))
        import api_football as api
        import automation_worker as worker
        import cloud_storage
        import causal_candidate as candidates
        import admin_access
        import recovery_rehearsal
        from scripts import automation_service as service
        for module in (api,worker,cloud_storage,candidates,admin_access,recovery_rehearsal,service):
            _origin(module,root)
        if Path(api.DATA_DIR).resolve() != root/'data/api_football' or cloud_storage.configured():
            raise ProbeError('application_data_outside_sandbox')
        guards.enter_context(patch.object(api,'load_key',side_effect=blocked_key))
        guards.enter_context(patch.object(api.ApiFootballClient,'get',side_effect=blocked_network))
        guards.enter_context(patch.object(candidates,'fit_candidate',side_effect=blocked_fit))
        import catboost
        guards.enter_context(patch.object(catboost.CatBoostClassifier,'fit',side_effect=blocked_fit))
        profile={'PATTERNLAB_STORAGE_MODE':'local','access':{'local_owner':True}}
        allowed=admin_access.authorized(profile,{},server_address='127.0.0.1')
        denied=not admin_access.authorized(profile,{},server_address='0.0.0.0')
        checks.append(dict(component='synthetic_local_authorization',state='passed' if allowed and denied else 'failed',
                           loopback_allowed=allowed,public_bind_denied=denied,google_signin_tested=False))
        for name in recovery_rehearsal.STORES:
            try:
                data=recovery_rehearsal._domain(name,root)
                checks.append(dict(component=name,state='passed',**data))
            except (ValueError,OSError,sqlite3.Error,KeyError,TypeError,RuntimeError):
                checks.append(dict(component=name,state='failed',reason='restored_domain_read_failed'))
        for name,operation in (('candidate_replay',lambda:_candidate_replay(root)),
                               ('bounded_worker_startup',lambda:_offline_cycle(root,api,worker,service))):
            try:
                checks.append(dict(component=name,**operation()))
            except (ValueError,OSError,sqlite3.Error,KeyError,TypeError,RuntimeError):
                checks.append(dict(component=name,state='failed',reason='restored_operation_failed'))
    return dict(version=VERSION,checked_at=datetime.now(timezone.utc).isoformat(),
                state='passed' if all(c['state']=='passed' for c in checks) and not any(counters.values()) else 'failed',
                checks=checks,**counters,private_profile_restored=False,synthetic_profile_used=True,
                same_host=True,production_restored=False,live_provider_tested=False,
                complete_service_recovery_demonstrated=False,launchd_registered=False,
                limitations=['Same-host isolated restored code and environment, not a different-machine recovery.',
                    'Calendar is replayed from the backup, not a fresh API-Football response.',
                    'The worker pass stops before new forecast queues or training; its writes are disposable test data.',
                    'One retained prediction replay checks deterministic recovery, not predictive accuracy.',
                    'Real private configuration, Google sign-in and provider access are not restored or tested.'])


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root',type=Path,required=True)
    parser.add_argument('--output',type=Path,default=Path('.restoration/probe.json'))
    args=parser.parse_args(argv)
    try:
        root=validate_sandbox(args.project_root)
        given=args.output
        if given.is_absolute() or '..' in given.parts:
            raise ProbeError('output_path_invalid')
        output=_safe_path(root,given)
        if output.exists():
            raise ProbeError('output_exists')
        result=probe(root)
        output.parent.mkdir(parents=True,exist_ok=True)
        with output.open('x',encoding='utf-8') as stream:
            json.dump(result,stream,ensure_ascii=False,indent=2,allow_nan=False)
        print(json.dumps(dict(state=result['state'],output=str(given)),ensure_ascii=False))
        return 0 if result['state']=='passed' else 1
    except (ValueError,OSError,sqlite3.Error,KeyError,TypeError,RuntimeError):
        parser.exit(2,'Restored service probe refused or unavailable; no private details displayed.\n')


if __name__=='__main__':
    raise SystemExit(main())
