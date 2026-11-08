"""Versioned individual shadow captures for a pinned causal candidate.

The candidate stays isolated. Fixed-T sources remain conditionally reconstructed;
post-commit receipts separately attest actual storage before kickoff.
"""
from collections import Counter
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from importlib.metadata import version as installed_version
import json
import fcntl
import os
import platform
import shutil
import sqlite3

import api_football as api
import api_match_workspace as calendar
import causal_candidate as candidates
import causal_confirmation_store as store
import causal_feature_workspace as features
import causal_population as population
import cloud_storage
import fixture_import_policy as imports
import fixture_temporal_store as temporal
from fixture_temporal import utc_instant
from halftime_domain import CLASS_ORDER, validate_probabilities, outcome
from local_recovery import fingerprint
import prematch_feature_snapshot as snapshots


VERSION = 'causal-confirmation-v1'
METHODS = ('candidate', 'clubs', 'frequency')


def _root(root=None):
    return Path(api.DATA_DIR if root is None else root).resolve()


def _now(root=None, clock=None):
    # The real scheduler supplies a callback too. Never let that callback
    # override wall time in the production directory.
    value = datetime.now(timezone.utc) if _root(root) == Path(api.DATA_DIR).resolve() or clock is None else clock()
    return utc_instant(value)


def _seal(value):
    return dict(value, id=features.digest(value))


def implementation():
    result = features.implementation()
    root = Path(__file__).resolve().parent
    for name in ('causal_confirmation.py', 'causal_confirmation_store.py', 'causal_confirmation_worker.py',
                 'causal_population.py','automation_worker.py', 'causal_confirmation_followup.py',
                 'causal_candidate.py', 'causal_confirmation_diagnostics.py'):
        result[name] = fingerprint(root/name)
    return result


def _runtime():
    return dict(python=platform.python_version(),
                packages={name: installed_version(name) for name in ('catboost', 'numpy', 'scipy')})


def _compatible(protocol):
    return (protocol['implementation'] == implementation()
            and ('followup' not in protocol or protocol.get('runtime') == _runtime()))


def _validate_protocol(value):
    store._sealed(value)
    if (value.get('version') != VERSION or value.get('decision_hour_utc') != 8
            or type(value['decision_hour_utc']) is not int
            or type(value.get('capture_grace_minutes')) is not int or value['capture_grace_minutes'] != 30
            or type(value.get('calendar_warmup_minutes')) is not int or value['calendar_warmup_minutes'] != 30
            or value.get('methods') != list(METHODS) or value.get('automatic_promotion') is not False
            or value.get('source_visibility_at_T_proven') is not False
            or value.get('source_time_quality') != 'local_import_recorded'
            or type(value.get('synthetic_clock')) is not bool):
        raise ValueError('Le protocole de confirmation est incompatible.')
    if not utc_instant(value['candidate_completed_at']) <= utc_instant(value['registered_at']):
        raise ValueError('Le protocole précède la disponibilité du candidat.')
    start, end = utc_instant(value['confirmation_start']), utc_instant(value['confirmation_end'])
    if start >= end or any((v.hour,v.minute,v.second,v.microsecond) != (0,0,0,0) for v in (start,end)):
        raise ValueError('Les journées de confirmation doivent être explicites et entières en UTC.')
    if 'followup' in value:
        from causal_confirmation_followup import validate_plan
        validate_plan(value['followup'], value['confirmation_start'], value['confirmation_end'])
        original = value['candidate_original_confirmation']
        if utc_instant(original['end']) > start:
            raise ValueError('Le nouveau suivi doit commencer après la confirmation originale du candidat.')
        if utc_instant(value['registered_at']) >= _decision(value, start.date()):
            raise ValueError('Le suivi doit être inscrit avant sa première décision.')
        runtime = value.get('runtime')
        if (not isinstance(runtime, dict) or set(runtime) != {'python', 'packages'}
                or not isinstance(runtime['python'], str)
                or not isinstance(runtime['packages'], dict)
                or set(runtime['packages']) != {'catboost', 'numpy', 'scipy'}
                or any(not isinstance(v, str) or not v for v in runtime['packages'].values())):
            raise ValueError('Les versions de calcul du suivi sont absentes ou invalides.')
    return value


def protocols(root=None):
    return [_validate_protocol(row) for row in store.protocols(root=root)]


def _attempt(protocol, kind, root, current, **fields):
    value = _seal(dict(version=VERSION, protocol_id=protocol['id'], kind=kind,
                       observed_at_utc=current.isoformat(), **fields))
    store.attempts(protocol['id'], value, root=root)
    return value


def register(candidate_directory, root=None, *, clock=None):
    """Explicit enrollment pins all candidate files, without capture or fit."""
    root = _root(root)
    cloud_storage._assert_writable()
    if cloud_storage.configured():raise ValueError('Le suivi de confirmation exige le stockage local.')
    directory=root/'causal_confirmation'
    directory.mkdir(parents=True,exist_ok=True)
    with (directory/'registration.lock').open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        try:return _register(candidate_directory,root,clock=clock)
        finally:fcntl.flock(handle,fcntl.LOCK_UN)


def register_followup(candidate_directory, *, start, end, root=None, clock=None):
    """Enroll a NEW future study; never extend the candidate's original test."""
    from causal_confirmation_followup import make_plan
    plan = make_plan(start, end)
    root = _root(root)
    cloud_storage._assert_writable()
    if cloud_storage.configured():
        raise ValueError('Le suivi de confirmation exige le stockage local.')
    directory = root/'causal_confirmation'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory/'registration.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            return _register(candidate_directory, root, clock=clock, followup=plan)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _register(candidate_directory, root, *, clock=None, followup=None):
    source = Path(candidate_directory).resolve()
    saved = candidates.load_candidate(source)
    report = saved['report']
    original_window = deepcopy(report['plan']['windows']['confirmation'])
    window = original_window if followup is None else dict(
        start=followup['confirmation_start'], end=followup['confirmation_end'])
    if followup is not None and utc_instant(original_window['end']) > utc_instant(window['start']):
        raise ValueError('La nouvelle période doit suivre la confirmation originale, sans la remplacer.')
    current = _now(root, clock)
    if current < utc_instant(report['completed_at']) or current >= utc_instant(window['end']):
        raise ValueError('Le candidat est futur ou sa période de confirmation est déjà terminée.')
    enrollment = dict(candidate_id=report['id'], package_id=saved['package']['id'],
        window=window, hour_utc=8, grace_minutes=30, warmup_minutes=30, implementation=implementation())
    if followup is not None:
        enrollment['followup'] = followup
        enrollment['runtime'] = _runtime()
    key = features.digest(enrollment)
    prior = next((row for row in protocols(root) if row['enrollment_key'] == key), None)
    if prior is not None:
        if not any(row['kind']=='registration_ack' for row in store.attempts(prior['id'],root=root)):
            # A new real read supplies a NEW upper bound after an interrupted
            # enrollment. It never invents an earlier registration receipt.
            _attempt(prior,'registration_ack',root,_now(root,clock),
                     time_quality='synthetic_unattested' if prior['synthetic_clock']
                     or clock is not None and root!=Path(api.DATA_DIR).resolve() else 'local_registration_ack')
        return prior
    first_decision=datetime.combine(utc_instant(window['start']).date(),time(8),timezone.utc)
    if current>=first_decision:
        raise ValueError('La première décision de ce protocole est passée ; choisir un futur protocole explicite.')
    destination = root/'causal_confirmation'/'candidates'/report['id']
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)
        for name in ('candidate.cbm','report.json','COMPLETE.json'):
            with (destination/name).open('rb') as stream:os.fsync(stream.fileno())
    pinned = candidates.load_candidate(destination)
    if pinned['report'] != report or pinned['package'] != saved['package']:
        raise ValueError('La copie du candidat ne correspond pas à sa version d’origine.')
    current = _now(root, clock)
    fields = dict(version=VERSION, enrollment_key=key, candidate_id=report['id'],
        package_id=saved['package']['id'], candidate_files=deepcopy(saved['package']['files']),
        candidate_completed_at=report['completed_at'], registered_at=current.isoformat(),
        confirmation_start=window['start'], confirmation_end=window['end'],
        decision_hour_utc=8, capture_grace_minutes=30, calendar_warmup_minutes=30, methods=list(METHODS),
        implementation=implementation(), source_time_quality='local_import_recorded',
        source_visibility_at_T_proven=False, automatic_promotion=False,
        primary='paired multiclass logloss on the same admissible settled fixtures',
        secondary=['Brier sum','favorite accuracy','class calibration and coverage'],
        population='All retained NS fixtures at08UTC, same UTC kickoff day, kickoff strictly after08UTC; no support/probability filter',
        missing='Missing/administrative/unusable results and missed captures are separate from losses',
        interpretation='Descriptive comparison; no automatic predictive advantage or model promotion',
        synthetic_clock=clock is not None and root != Path(api.DATA_DIR).resolve())
    if followup is not None:
        fields.update(followup=deepcopy(followup), candidate_original_confirmation=original_window,
                      runtime=deepcopy(enrollment['runtime']))
    protocol = _seal(fields)
    _validate_protocol(protocol)
    store.put_protocol(protocol, root=root)
    _attempt(protocol, 'registration_ack', root, _now(root, clock),
             time_quality='synthetic_unattested' if protocol['synthetic_clock'] else 'local_registration_ack')
    return protocol


def _dates(protocol):
    start, end = utc_instant(protocol['confirmation_start']).date(), utc_instant(protocol['confirmation_end']).date()
    return [start+timedelta(days=n) for n in range((end-start).days)]


def _decision(protocol, day):
    return datetime.combine(day, time(protocol['decision_hour_utc']), timezone.utc)


def _closed_window(protocol,day,root,current, *, clock=None):
    events=store.attempts(protocol['id'],root=root)
    if any(row['kind'] in ('window_missed','window_closed') and row.get('day')==day.isoformat() for row in events):
        return
    anchor=store.anchor(protocol['id'],day.isoformat(),root=root)
    if anchor is None:
        _attempt(protocol,'window_missed',root,current,day=day.isoformat(),
                 reason='Fenêtre terminée sans recensement attesté ; aucune prévision passée ne sera recréée.')
    else:
        captured={row['payload']['fixture']['match_id'] for row in store.heads(root=root,protocol_id=protocol['id'])
                  if utc_instant(row['payload']['planned_decision_at']).date() == day}
        missing=[row['match_id'] for row in anchor['targets'] if row['match_id'] not in captured]
        closure = _attempt(protocol,'window_closed',root,current,day=day.isoformat(),missing_match_ids=missing,
                           reason=f'Fenêtre terminée ; {len(missing)} cible(s) sans enregistrement. Les accusés sont évalués séparément.')
        if 'followup' in protocol:
            # Actual upper bound sampled AFTER the closure event's COMMIT.
            # Missing acknowledgments after a crash are never backfilled.
            observed = _now(root, clock) if _root(root) == Path(api.DATA_DIR).resolve() or clock else current
            _attempt(protocol, 'window_closed_ack', root, observed, day=day.isoformat(),
                     closure_id=closure['id'], time_quality='local_closure_commit_ack'
                     if _root(root) == Path(api.DATA_DIR).resolve() else 'synthetic_unattested')


def _registration_ready(protocol, decision, root, *, proof=False):
    return any(row['kind'] == 'registration_ack'
               and utc_instant(protocol['registered_at']) <= utc_instant(row['observed_at_utc']) < decision
               and (not proof or not protocol['synthetic_clock'] and row.get('time_quality')=='local_registration_ack')
               for row in store.attempts(protocol['id'], root=root))


def _pin(root, protocol):
    directory = root/'causal_confirmation'/'candidates'/protocol['candidate_id']
    pinned = candidates.load_candidate(directory)
    if (pinned['package']['id'] != protocol['package_id'] or pinned['report']['id'] != protocol['candidate_id']
            or pinned['package']['files'] != protocol['candidate_files']):
        raise ValueError('Le candidat inscrit a été modifié.')
    return pinned


def _anchor(protocol, day, root, clock):
    saved = store.anchor(protocol['id'], day.isoformat(), root=root)
    if saved is not None:
        return saved
    decision = _decision(protocol, day)
    started = _now(root, clock)
    state = temporal.state_as_of(root/'fixtures.json', decision)
    # Census retains all relevant exclusions. Its global future-version list is
    # summarized once, rather than copied into every forecast.
    # A postponed fixture already captured in this protocol is not a new
    # observation. Exclude it explicitly from the next day's target census.
    prior_ids = {row['payload']['fixture']['match_id'] for row in store.heads(root=root, protocol_id=protocol['id'])}
    census = population.census_day(state, day.isoformat(), already_selected=prior_ids)
    prepared = features.FeaturePreparation(dict(state, temporal_exclusions=[]))
    contexts = {}
    selected = {row['match_id']: row for row in state['selected']}
    for league in {row['fixture']['league'] for row in census['targets']}:
        history, refs, errors = prepared.history(league, day.isoformat())
        history=sorted(history,key=lambda row:(row['timestamp'],int(row['match_id'])))
        refs=sorted(refs,key=lambda row:int(row['match_id']))
        context_id = store.put_history(history, refs, root=root)
        contexts[league] = dict(context_id=context_id, quality_exclusions=errors)
    targets = [dict(row, target_raw=deepcopy(selected[row['match_id']]['raw'])) for row in census['targets']]
    value = _seal(dict(version=VERSION, protocol_id=protocol['id'], day=day.isoformat(),
        decision_at=decision.isoformat(), targets=targets, exclusions=census['exclusions'],
        excluded_selected_counts=census['excluded_selected_counts'],
        temporal_exclusion_counts=dict(Counter(row['reason'] for row in state['temporal_exclusions'])),
        untracked_current_inventory=state['untracked_inventory_count'], contexts=contexts,
        read_started_at=started.isoformat(), read_finished_at=_now(root, clock).isoformat(),
        source_store=state['source_store'], source_visibility_at_T_proven=False))
    return store.anchor(protocol['id'], day.isoformat(), value, root=root)


def _snapshot(anchor, target, root):
    context = anchor['contexts'][target['fixture']['league']]
    history = store.get_history(context['context_id'], root=root)
    return snapshots.build(history['history_rows'], target['fixture'], decision_at=anchor['decision_at'],
                           source_refs=history['source_refs'], target_ref=target['target_ref'], target_raw=target['target_raw'])


def _fresh(snapshot, raw, started, received, current):
    if (raw.get('provider') not in (None,'api-football','API-Football')
            or imports.quality_error(raw) or imports.QUALITY_KEY in raw or api._status_short(raw) != 'NS'):
        raise ValueError('Le fournisseur ne confirme plus une affiche non commencée.')
    fixture = calendar.fixture_from_raw(raw)
    if any(str(fixture.get(key)) != str(snapshot['fixture'].get(key))
           for key in ('match_id','league','home_id','away_id','timestamp','season')):
        raise ValueError('Identité ou horaire modifié depuis la décision.')
    if not started <= received <= current < calendar._stamp(raw) or (current-received).total_seconds() > 120:
        raise ValueError('La confirmation fournisseur est tardive ou trop ancienne.')


def capture_due(root=None, *, clock=None, refresh=None, check_time=None):
    """Run only within the registered window; the scheduler owns API calls."""
    root = _root(root)
    output = dict(status='idle', created=0, existing=0, missed=0, issues=[])
    for protocol in protocols(root):
        current = _now(root, clock)
        if not _compatible(protocol):
            output['issues'].append('Implémentation modifiée : nouvelle inscription explicite nécessaire.')
            continue
        pinned = None
        events = store.attempts(protocol['id'], root=root)
        closed_days = {row.get('day') for row in events if row['kind'] in ('window_closed', 'window_missed')}
        for day in _dates(protocol):
            decision = _decision(protocol, day)
            deadline = decision+timedelta(minutes=protocol['capture_grace_minutes'])
            if current < decision:
                # The ordinary calendar is in Kinshasa time. Prepare the exact
                # UTC day once BEFORE T so its final local-hour fixtures are
                # not silently absent from this distinct UTC census.
                if current>=decision-timedelta(minutes=protocol['calendar_warmup_minutes']) and refresh is not None:
                    prior=store.attempts(protocol['id'],root=root)
                    if not any(row['kind']=='calendar_prepared' and row.get('day')==day.isoformat() for row in prior):
                        try:
                            if check_time:check_time()
                            raw=refresh(date=day.isoformat(),timezone='UTC')
                            if any(calendar._stamp(row).date()!=day for row in raw):
                                raise ValueError('Le calendrier UTC contient une autre journée.')
                            api.save_fixtures(raw,root/'fixtures.json')
                            _attempt(protocol,'calendar_prepared',root,_now(root,clock),day=day.isoformat(),received=len(raw))
                        except Exception as error:
                            _attempt(protocol,'calendar_preparation_unavailable',root,_now(root,clock),day=day.isoformat(),reason=str(error)[:400])
                            raise
                continue
            if current >= deadline:
                if day.isoformat() not in closed_days:
                    _closed_window(protocol,day,root,current,clock=clock)
                output['missed'] += 1
                continue
            if not _registration_ready(protocol, decision, root):
                _attempt(protocol, 'registration_after_decision', root, current, day=day.isoformat())
                output['missed'] += 1
                continue
            if refresh is None:
                output['issues'].append('Confirmation API fraîche indisponible ; aucune capture lancée.')
                continue
            if check_time:
                check_time()
            try:
                pinned = _pin(root, protocol) if pinned is None else pinned
                anchor = _anchor(protocol, day, root, clock)
            except (ValueError,OSError,sqlite3.Error) as error:
                _attempt(protocol,'preparation_unavailable',root,_now(root,clock),day=day.isoformat(),reason=str(error)[:400])
                output['issues'].append('Source ou candidat indisponible ; recensement non confirmé.')
                continue
            existing = {row['payload']['fixture']['match_id'] for row in store.heads(root=root, protocol_id=protocol['id'])}
            targets = [row for row in anchor['targets'] if row['match_id'] not in existing]
            output['existing'] += sum(row['match_id'] in existing for row in anchor['targets'])
            for offset in range(0, len(targets), 20):
                if check_time:
                    check_time()
                if _now(root, clock) >= deadline:
                    break
                batch, inputs = targets[offset:offset+20], []
                for target in batch:
                    try:
                        inputs.append((target, _snapshot(anchor, target, root)))
                    except ValueError as error:
                        _attempt(protocol, 'feature_unavailable', root, _now(root, clock),
                                 day=day.isoformat(), match_id=target['match_id'], reason=str(error))
                if not inputs:
                    continue
                probabilities = candidates.predict_candidate(pinned, [row['features'] for _, row in inputs])
                if len(probabilities) != len(inputs):
                    raise ValueError('Le nombre de probabilités ne correspond pas aux cibles à conserver.')
                # Confirmation follows inference. It can reject a changed target,
                # but cannot alter its population, source cutoff or features.
                started = _now(root, clock)
                try:
                    raw = refresh(ids='-'.join(target['match_id'] for target, _ in inputs))
                except Exception as error:
                    # Audit the provider/budget failure, then let the existing
                    # scheduler handle its own pause or quota exception.
                    _attempt(protocol,'provider_unavailable',root,_now(root,clock),day=day.isoformat(),reason=str(error)[:400])
                    raise
                received = _now(root, clock)
                indexed, conflicts = {}, set()
                for row in raw:
                    mid = str(row.get('fixture', {}).get('id'))
                    if mid in indexed and indexed[mid] != row:
                        conflicts.add(mid)
                    indexed[mid] = row
                for (target, snapshot), probability in zip(inputs, probabilities):
                    current = _now(root, clock)
                    try:
                        if current >= deadline:
                            raise ValueError('Fenêtre de capture terminée.')
                        if target['match_id'] in conflicts or target['match_id'] not in indexed:
                            raise ValueError('Affiche absente ou contradictoire dans la confirmation API.')
                        confirmed = indexed[target['match_id']]
                        _fresh(snapshot, confirmed, started, received, current)
                        methods = dict(candidate=validate_probabilities(probability),
                            clubs=validate_probabilities(snapshot['references']['clubs']),
                            frequency=validate_probabilities(snapshot['references']['frequency']))
                        value = _seal(dict(version=VERSION, protocol_id=protocol['id'], candidate_id=protocol['candidate_id'],
                            fixture=snapshot['fixture'], snapshot=snapshot, methods=methods,
                            prepared_at=current.isoformat(), planned_decision_at=anchor['decision_at'],
                            source_read=dict(start=anchor['read_started_at'], end=anchor['read_finished_at']),
                            confirmation=dict(requested_at=started.isoformat(), received_at=received.isoformat(), raw=confirmed),
                            source_visibility_at_T_proven=False))
                        mutation_clock = None if root == Path(api.DATA_DIR).resolve() else clock
                        saved = store.put_forecast(value, root=root, clock=mutation_clock)
                        output['existing' if saved['existing'] else 'created'] += 1
                    except ValueError as error:
                        _attempt(protocol, 'capture_unavailable', root, current, day=day.isoformat(),
                                 match_id=target['match_id'], reason=str(error))
    return dict(output, status='captured' if output['created'] else 'unavailable' if output['issues'] else 'idle')


def _settle(fixture, source, current):
    empty = dict(status='pending', ht=None, ft=None, actual=None, issues=[], source_ref=None)
    if source is None:
        return dict(empty, status='unresolved', issues=['Résultat absent du suivi local.'])
    raw = source['raw']
    reference = features._ref(source)
    try:
        if imports.quality_error(raw):
            raise ValueError(imports.quality_error(raw))
        actual = calendar.fixture_from_raw(raw)
        if any(str(actual.get(key)) != str(fixture.get(key)) for key in ('match_id','league','home_id','away_id','season')):
            raise ValueError('Identité du match modifiée.')
        short = api._status_short(raw)
        if short in ('CANC','ABD','AWD','WO'):
            return dict(empty, status='void', issues=['Décision administrative : pas de résultat sportif par mi-temps.'], source_ref=reference)
        if short in ('PST','SUSP','INT'):
            return dict(empty, status='unresolved', issues=['Match reporté, suspendu ou interrompu.'], source_ref=reference)
        if actual['timestamp'] != fixture['timestamp']:
            raise ValueError('Horaire modifié depuis la capture ; résultat à examiner.')
        if short not in api.FINISHED_STATUSES:
            if current >= calendar._stamp(raw)+timedelta(hours=4):
                return dict(empty, status='unresolved', issues=['Résultat final non reçu.'], source_ref=reference)
            return dict(empty, source_ref=reference)
        if not calendar._stamp(raw) <= utc_instant(source['observed_at_utc']) < current:
            raise ValueError('Résultat final enregistré avant le coup d’envoi ou après le contrôle.')
        normalized = api.normalize_fixture(raw)
        return dict(status='settled', ht=normalized['ht'], ft=normalized['ft'], actual=normalized['value'], issues=[], source_ref=reference)
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        return dict(empty, status='unresolved', issues=[str(error)], source_ref=reference)


def reconcile(root=None, *, now=None):
    root = _root(root)
    if now is not None and root == Path(api.DATA_DIR).resolve():
        raise ValueError('Le rapprochement réel utilise uniquement son horloge actuelle.')
    current = _now(root, (lambda: now) if now is not None else None)
    # This audit also runs in the scheduler's result/finally path when provider
    # calls were paused before the inference turn. It never creates a forecast.
    for protocol in protocols(root):
        closed_days = {row.get('day') for row in store.attempts(protocol['id'], root=root)
                       if row['kind'] in ('window_missed', 'window_closed')}
        for day in _dates(protocol):
            if (day.isoformat() not in closed_days
                    and current>=_decision(protocol,day)+timedelta(minutes=protocol['capture_grace_minutes'])):
                _closed_window(protocol,day,root,current,clock=(lambda: current) if now is not None else None)
    saved = store.heads(root=root)
    if not saved:
        endpoints = _finalize_endpoints(root, current, clock=(lambda: current) if now is not None else None)
        return dict(entries=0, changed=0, settled=0, endpoints_sealed=endpoints)
    ids = [row['payload']['fixture']['match_id'] for row in saved]
    state = temporal.state_as_of(root/'fixtures.json', current, ids=ids)
    selected = {row['match_id']: row for row in state['selected']}
    changed, settled = 0, 0
    for forecast in saved:
        payload = forecast['payload']
        if forecast['ack'] and utc_instant(forecast['ack']['observed_at_utc']) > current:
            continue
        result = _settle(payload['fixture'], selected.get(payload['fixture']['match_id']), current)
        previous = store.results(payload['id'], root=root)
        old = previous[-1]['payload'] if previous else None
        if old and utc_instant(old['checked_at']) > current:
            continue
        # Observations may change without rewriting the original probabilities.
        if old and {k:v for k,v in old.items() if k not in ('id','checked_at')} == result:
            settled += int(result['status'] == 'settled')
            continue
        if old and old['status'] == 'settled' and result['status'] == 'pending':
            settled += 1
            continue
        value = _seal(dict(result, checked_at=current.isoformat()))
        store.put_result(payload['id'], value, root=root,
                         clock=(lambda: current) if now is not None else None)
        changed += 1; settled += int(result['status'] == 'settled')
    endpoints = _finalize_endpoints(root, _now(root, (lambda: current) if now is not None else None),
                                   clock=(lambda: current) if now is not None else None)
    return dict(entries=len(saved), changed=changed, settled=settled, endpoints_sealed=endpoints)


def _entries(root=None, protocol_id=None, *, result_cutoff=None, endpoint=None):
    output = []
    by_protocol = {row['id']:row for row in protocols(root)}
    anchored={}
    for row in store.heads(root=root, protocol_id=protocol_id):
        payload, ack = row['payload'], row['ack']
        protocol = by_protocol[payload['protocol_id']]
        if (payload.get('version') != VERSION or payload.get('candidate_id') != protocol['candidate_id']
                or set(payload['methods']) != set(METHODS)
                or payload.get('source_visibility_at_T_proven') is not False
                or payload['planned_decision_at'] != payload['snapshot']['decision_at']):
            raise ValueError('La prévision ne correspond pas au protocole figé.')
        for probability in payload['methods'].values():
            validate_probabilities(probability)
        if (payload['methods']['clubs'] != payload['snapshot']['references']['clubs']
                or payload['methods']['frequency'] != payload['snapshot']['references']['frequency']):
            raise ValueError('Les références conservées ne correspondent pas au même instantané.')
        day = utc_instant(payload['fixture']['timestamp']+'+00:00').date()
        decision = _decision(protocol, day)
        deadline = decision+timedelta(minutes=protocol['capture_grace_minutes'])
        prepared = utc_instant(payload['prepared_at'])
        if (day not in _dates(protocol) or utc_instant(payload['planned_decision_at']) != decision
                or not decision <= utc_instant(payload['source_read']['start'])
                <= utc_instant(payload['source_read']['end'])
                <= utc_instant(payload['confirmation']['requested_at'])
                <= utc_instant(payload['confirmation']['received_at']) <= prepared < deadline):
            raise ValueError('Les heures de la prévision ne correspondent pas à la fenêtre déclarée.')
        _fresh(payload['snapshot'],payload['confirmation']['raw'],
               utc_instant(payload['confirmation']['requested_at']),utc_instant(payload['confirmation']['received_at']),prepared)
        anchor_key=(protocol['id'],day.isoformat())
        if anchor_key not in anchored:
            anchor=store.anchor(protocol['id'],day.isoformat(),root=root)
            if anchor is None:raise ValueError('Une prévision est absente du recensement conservé.')
            anchored[anchor_key]=(anchor,{target['match_id']:target for target in anchor['targets']})
        anchor,targets=anchored[anchor_key]
        target=targets.get(payload['fixture']['match_id'])
        # The snapshot writer canonicalizes display labels and numeric IDs.
        # Compare through that SAME contract, retaining the immutable raw
        # target/ref/context checks. Leading spaces in a provider club label
        # must not make an otherwise identical saved forecast unreadable.
        canonical_target = snapshots._fixture(target['fixture'], decision)[0] if target else None
        if (target is None or canonical_target!=payload['fixture']
                or target['target_ref']!=payload['snapshot']['target_ref']
                or target['target_raw']!=payload['snapshot']['target_observation']
                or row['context_id']!=anchor['contexts'][payload['fixture']['league']]['context_id']):
            raise ValueError('La prévision ne correspond pas à la cible et à l’historique du recensement.')
        revisions = store.results(payload['id'], root=root)
        if endpoint is not None:
            if payload['id'] not in endpoint['selection']:
                raise ValueError('La prévision est absente de la sélection finale figée.')
            choice = endpoint['selection'][payload['id']]
            revisions = [revision for revision in revisions if revision['revision'] == choice['revision']]
            if (len(revisions) != int(choice['revision'] is not None)
                    or revisions and revisions[0]['payload']['id'] != choice['result_id']):
                raise ValueError('La révision du résultat contredit la sélection finale figée.')
            cutoff = utc_instant(endpoint['result_cutoff_utc'])
            if any(utc_instant(revision['payload']['checked_at']) > cutoff
                   or revision.get('ack') is None
                   or utc_instant(revision['ack']['observed_at_utc']) > cutoff
                   or (_root(root) == Path(api.DATA_DIR).resolve()
                       and revision['ack']['time_quality'] != 'local_result_commit_ack')
                   for revision in revisions):
                raise ValueError('Une révision du bilan final ne possède pas de preuve avant la coupure fixe.')
        elif result_cutoff is not None:
            revisions = [revision for revision in revisions
                         if utc_instant(revision['payload']['checked_at']) <= result_cutoff]
            if 'followup' in protocol:
                # checked_at alone is a source-scan time, not a COMMIT proof.
                # A pass starting before the endpoint can finish afterwards.
                revisions = [revision for revision in revisions if revision.get('ack') is not None
                             and utc_instant(revision['ack']['observed_at_utc']) <= result_cutoff
                             and (revision['ack']['time_quality'] == 'local_result_commit_ack'
                                  or _root(root) != Path(api.DATA_DIR).resolve())]
        result = revisions[-1]['payload'] if revisions else dict(status='pending',ht=None,ft=None,actual=None,issues=[])
        if result['status'] not in ('pending','settled','unresolved','void'):
            raise ValueError('Le statut de résultat conservé est invalide.')
        if result['status']=='settled':
            if (result['actual']!=outcome(result['ht'],result['ft']) or result.get('source_ref') is None
                    or result['source_ref']['match_id']!=payload['fixture']['match_id']
                    or not utc_instant(payload['fixture']['timestamp']+'+00:00')
                    <=utc_instant(result['source_ref']['observed_at_utc'])<utc_instant(result['checked_at'])):
                raise ValueError('Le score, le label ou la chronologie du résultat conservé est invalide.')
        elif any(result.get(key) is not None for key in ('ht','ft','actual')):
            raise ValueError('Un résultat non évalué ne peut contenir de label sportif.')
        if ack and (utc_instant(ack['transaction_at_utc'])<prepared
                    or utc_instant(ack['observed_at_utc'])<prepared):
            raise ValueError('L’accusé de capture précède la préparation de la prévision.')
        registration_proof=_registration_ready(protocol,decision,root,proof=True)
        admissible = bool(ack and ack['admissible'] and utc_instant(ack['observed_at_utc']) < deadline
                          and registration_proof)
        status = result['status'] if admissible else 'missing_ack' if ack is None else (
            'unattested_capture' if ack['time_quality']=='synthetic_unattested' or not registration_proof else 'late_capture')
        output.append(dict(fixture=payload['fixture'], methods=payload['methods'], captured_at=ack['observed_at_utc'] if ack else None,
            status=status, ht=result['ht'], ft=result['ft'], actual=result['actual'], issues=result['issues'],
            timing_quality=ack['time_quality'] if ack else 'missing_commit_ack',
            admissible=admissible, forecast_id=payload['id'],
            result_revision=revisions[-1]['revision'] if revisions else None,
            result_id=result.get('id')))
    if endpoint is not None and set(endpoint['selection']) != {row['forecast_id'] for row in output}:
        raise ValueError('Les prévisions du suivi contredisent la sélection finale figée.')
    return output


def _days(protocol, rows, root, current):
    days = []
    captures = Counter(utc_instant(row['fixture']['timestamp']+'+00:00').date().isoformat() for row in rows)
    for day in _dates(protocol):
        anchor = store.anchor(protocol['id'], day.isoformat(), root=root)
        deadline = _decision(protocol, day)+timedelta(minutes=protocol['capture_grace_minutes'])
        days.append(dict(day=day.isoformat(), targets=len(anchor['targets']) if anchor else None,
                         captures=captures[day.isoformat()],
                         state='anchored' if anchor else 'awaiting' if current < deadline else 'missed'))
    return days


def _finalize_endpoints(root, current, *, clock=None):
    """The worker seals the first final selection; ordinary readers never write.

    The fixed cutoff selects result/closure receipts. The separate manifest
    records which proofs were actually present at this first sealing, so even
    a delayed receipt publication cannot change later reads of the endpoint.
    """
    sealed = 0
    for protocol in protocols(root):
        if 'followup' not in protocol or store.endpoint(protocol['id'], root=root) is not None:
            continue
        cutoff = utc_instant(protocol['followup']['result_cutoff_utc'])
        if current < cutoff:
            continue
        rows = _entries(root, protocol['id'], result_cutoff=cutoff)
        value = _seal(dict(version='causal-confirmation-endpoint-v1', protocol_id=protocol['id'],
            result_cutoff_utc=cutoff.isoformat(), prepared_at=_now(root, clock).isoformat(),
            selection={row['forecast_id']: dict(revision=row['result_revision'], result_id=row['result_id']) for row in rows},
            days=_days(protocol, rows, root, current),
            attempt_ids=[event['id'] for event in store.attempts(protocol['id'], root=root)]))
        try:
            store.endpoint(protocol['id'], value, root=root, clock=clock)
            sealed += 1
        except store.StoreError:
            # Another local reconciler may have sealed the same study first.
            # Its immutable selection wins, never a replacement snapshot.
            if store.endpoint(protocol['id'], root=root) is None:
                raise
    return sealed


def refresh_entries(root=None):
    rows = []
    for forecast in store.heads(root=root):
        result = store.results(forecast['payload']['id'], root=root)
        status = result[-1]['payload']['status'] if result else 'pending'
        rows.append(dict(status=status, fixtures=[forecast['payload']['fixture']]))
    return rows


def report(root=None):
    root = _root(root); current = _now(root)
    output = []
    for protocol in protocols(root):
        final_cutoff = utc_instant(protocol['followup']['result_cutoff_utc']) if 'followup' in protocol else None
        endpoint = store.endpoint(protocol['id'], root=root) if final_cutoff is not None else None
        # Interim views use only revisions already recorded. After the fixed
        # endpoint the exact same revision selection is used on every read;
        # later corrections remain in the journal, outside this study endpoint.
        result_cutoff = min(current, final_cutoff) if final_cutoff else current
        rows = _entries(root, protocol['id'], result_cutoff=result_cutoff, endpoint=endpoint)
        paired = [row for row in rows if row['admissible'] and row['status'] == 'settled']
        metrics = {method:candidates.metrics([row['methods'][method] for row in paired], [row['actual'] for row in paired])
                   for method in METHODS} if paired else {}
        events = store.attempts(protocol['id'], root=root)
        if endpoint is not None:
            allowed = set(endpoint['attempt_ids'])
            events = [event for event in events if event['id'] in allowed]
            if {event['id'] for event in events} != allowed:
                raise ValueError('Les événements de la sélection finale sont incomplets.')
        days = deepcopy(endpoint['days']) if endpoint is not None else _days(protocol, rows, root, current)
        first = _decision(protocol, _dates(protocol)[0])
        today = next((day for day in days if day['day'] == current.date().isoformat()), None)
        if current < first:
            phase = 'awaiting_decision'
        elif current >= utc_instant(protocol['confirmation_end']):
            phase = 'finished'
        elif today is not None and current < _decision(protocol, current.date()):
            phase = 'between_daily_windows'
        elif today is not None and current < _decision(protocol, current.date())+timedelta(minutes=30):
            phase = 'capture_window'
        else:
            phase = 'window_closed' if today and today['state'] == 'anchored' else 'window_missed'
        counts = Counter(row['status'] for row in rows)
        output.append(dict(id=protocol['id'], candidate_id=protocol['candidate_id'], registered_at=protocol['registered_at'],
            generated_at=current.isoformat(), followup=deepcopy(protocol.get('followup')),
            endpoint={key:endpoint[key] for key in ('id','prepared_at','result_cutoff_utc')} if endpoint else None,
            candidate_original_confirmation=deepcopy(protocol.get('candidate_original_confirmation')),
            result_cutoff_utc=final_cutoff.isoformat() if final_cutoff and current >= final_cutoff else None,
            confirmation_start=protocol['confirmation_start'], confirmation_end=protocol['confirmation_end'],
            decision_hour_utc=protocol['decision_hour_utc'],capture_grace_minutes=protocol['capture_grace_minutes'],
            implementation_compatible=_compatible(protocol), phase=phase,
            targets_known=sum(day['targets'] or 0 for day in days), forecasts=len(rows),
            admissible=sum(row['admissible'] for row in rows), settled=counts['settled'],pending=counts['pending'],
            unresolved=counts['unresolved'],void=counts['void'],late=counts['late_capture'],missing_ack=counts['missing_ack'],
            metrics=metrics, rows=rows, days=days, evaluated=len(paired),
            attempts=events,
            always_2nd_accuracy=sum(row['actual']=='2nd' for row in paired)/len(paired) if paired else None,
            automatic_promotion=False, predictive_improvement_demonstrated=False))
    return dict(version=VERSION, generated_at=current.isoformat(), protocols=output)


def verify(root=None):
    """Explicit full CAS/feature audit, separate from ordinary UI and queues."""
    full=store.forecasts(root=root)
    view=report(root=root)
    return dict(version=VERSION,checked_at=_now(root).isoformat(),forecasts_verified=len(full),
                protocols_verified=len(view['protocols']),active_model_changed=False,
                predictive_improvement_demonstrated=False)
