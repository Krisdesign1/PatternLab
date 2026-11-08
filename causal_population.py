"""Resumable local target census, frozen before labels or feature preparation.

Daily UTC decisions select the retained NS provider version at that instant.
There is no network, model fitting, active-store write or automatic promotion.
"""
from collections import Counter
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile

import api_football as api
import api_match_workspace as calendar
import causal_feature_artifacts as artifacts
import causal_feature_workspace as features
import fixture_import_policy as imports
import fixture_temporal_store as temporal
from fixture_temporal import utc_instant
from local_recovery import _backup_database, fingerprint


VERSION = 'causal-population-v1'


def _sealed(value):
    return dict(value, id=features.digest(value))


def _read(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    artifacts._seal(value)
    return value


def _atomic(path, value):
    """Progress is replaceable; scientific inputs and results use exclusive IO."""
    path = Path(path)
    temporary = path.with_name(path.name+'.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


def _write_once(path, value):
    """Publish one completed file exclusively; interrupted temp files are ignored."""
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix='.pending-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
            stream.flush(); os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.link(temporary, path)  # Fails if the destination already exists.
    finally:
        temporary.unlink(missing_ok=True)


def implementation():
    result = features.implementation()
    result['causal_population.py'] = fingerprint(Path(__file__))
    return result


def make_policy(start, end, *, hour_utc=8, outcome_at=None):
    start, end = date.fromisoformat(start), date.fromisoformat(end)
    if type(hour_utc) is not int or not 0 <= hour_utc < 24 or start >= end:
        raise ValueError('Une plage de jours croissante et une heure UTC entière de 0 à 23 sont nécessaires.')
    decisions = []
    for offset in range((end-start).days):
        day = start+timedelta(days=offset)
        decision = datetime.combine(day, time(hour_utc), timezone.utc)
        if decision > datetime.now(timezone.utc):
            raise ValueError('Le recensement historique ne peut pas inclure une décision future.')
        decisions.append(dict(day=day.isoformat(), decision_at=decision.isoformat()))
    outcome = utc_instant(outcome_at).isoformat() if outcome_at is not None else None
    if outcome is not None and (utc_instant(outcome) <= utc_instant(decisions[-1]['decision_at'])
                                or utc_instant(outcome) > datetime.now(timezone.utc)):
        raise ValueError('La lecture des labels doit suivre les décisions et ne pas être future.')
    return _sealed(dict(version=VERSION, kind='fixed_daily_utc_population',
        start=start.isoformat(), end_exclusive=end.isoformat(), hour_utc=hour_utc, decisions=decisions,
        outcome_at=outcome, rule='retained NS at T; kickoff in same UTC day and strictly after T; first decision per fixture',
        leagues='all', historical_support_filter=False, result_filter=False,
        temporal_quality='retrospective_conditional', prospective_confirmation=False))


def validate_policy(policy):
    artifacts._seal(policy)
    expected = make_policy(policy['start'], policy['end_exclusive'], hour_utc=policy['hour_utc'], outcome_at=policy['outcome_at'])
    if policy != expected:
        raise ValueError('La politique de population a été modifiée ou est incompatible.')


def census_day(state, day, *, already_selected=()):
    """No normalization of final scores or outcome join selects the targets."""
    decision = utc_instant(state['as_of_utc'])
    if decision.date().isoformat() != day or state.get('temporal_quality') != 'retrospective_conditional':
        raise ValueError('La reconstruction ne correspond pas à la journée du recensement.')
    targets, exclusions, reasons = [], [], Counter()
    for source in state['selected']:
        mid, raw = source['match_id'], source['raw']
        try:
            kickoff = calendar._stamp(raw)
        except (ValueError, KeyError, TypeError, AttributeError):
            exclusions.append(dict(match_id=mid, reason='invalid_kickoff_at_decision'))
            reasons['invalid_kickoff_at_decision'] += 1
            continue
        if kickoff.date().isoformat() != day:
            reasons['other_utc_day'] += 1
            continue
        reason = None
        if imports.quality_error(raw):
            reason = 'target_quarantined'
        elif api._status_short(raw) != 'NS':
            reason = 'not_NS_at_decision'
        elif kickoff <= decision:
            reason = 'kickoff_not_after_decision'
        elif mid in already_selected:
            reason = 'fixture_already_selected_on_earlier_day'
        else:
            try:
                fixture = calendar.fixture_from_raw(raw)
            except (ValueError, KeyError, TypeError, AttributeError):
                reason = 'invalid_target_metadata'
        if reason:
            reasons[reason] += 1
            exclusions.append(dict(match_id=mid, reason=reason))
        else:
            targets.append(dict(match_id=mid, decision_at=decision.isoformat(), fixture=fixture,
                                target_ref=features._ref(source)))
    targets.sort(key=lambda row: (row['fixture']['timestamp'], int(row['match_id'])))
    return dict(day=day, decision_at=decision.isoformat(), targets=targets, exclusions=exclusions,
        selected_versions=len(state['selected']), candidate_count=len(targets),
        excluded_selected_counts=dict(sorted(reasons.items())),
        temporal_exclusions=deepcopy(state['temporal_exclusions']),
        untracked_current_inventory=state['untracked_inventory_count'])


def _destination(directory):
    directory = Path(directory).resolve()
    root = Path(__file__).resolve().parent
    if directory.is_relative_to(root/'data') and not directory.is_relative_to(root/'data/research'):
        raise ValueError('La préparation doit rester en dehors des données actives, dans un dossier de recherche.')
    return directory


def _check_job(directory, *, source=False):
    directory = _destination(directory)
    for name in ('job.json', 'source.sqlite3', 'population.json', 'results', 'bundle', 'bundle/snapshots',
                 'summary.json', 'COMPLETE.json', 'progress.json', 'verification.json'):
        path = directory/name
        if path.is_symlink() or not path.resolve().is_relative_to(directory):
            raise ValueError('Chemin de préparation invalide.')
    job = _read(directory/'job.json')
    validate_policy(job['policy'])
    if (job.get('version') != VERSION or job.get('implementation') != implementation()
            or job.get('prospective_confirmation') is not False):
        raise ValueError('La préparation est incompatible avec le code installé ; conserver son ancienne version.')
    if source and fingerprint(directory/'source.sqlite3') != job['source_sha256']:
        raise ValueError('La copie de source figée a été modifiée.')
    return directory, job


def start(database, directory, policy):
    """Freeze a consistent SQLite copy and the policy; no census/fit yet."""
    validate_policy(policy)
    directory = _destination(directory)
    directory.mkdir(parents=True, exist_ok=False)
    source = api.active_store(Path(database))
    if source is None:
        raise ValueError('Le recensement reprenable exige une base SQLite API-Football active.')
    _backup_database(source, directory/'source.sqlite3')
    (directory/'results').mkdir()
    job = _sealed(dict(version=VERSION, policy=deepcopy(policy), source_sha256=fingerprint(directory/'source.sqlite3'),
        implementation=implementation(), created_at=datetime.now(timezone.utc).isoformat(),
        temporal_quality='retrospective_conditional', prospective_confirmation=False, active_model_changed=False))
    _write_once(directory/'job.json', job)
    _progress(directory, job, 'awaiting_census', 0, 0)
    return job


def _progress(directory, job, state, processed, total):
    _atomic(directory/'progress.json', _sealed(dict(version=VERSION, job_id=job['id'], state=state,
        processed=processed, total=total, updated_at=datetime.now(timezone.utc).isoformat(),
        active_model_changed=False, prospective_confirmation=False)))


def _population(directory, job):
    path = directory/'population.json'
    if path.exists():
        population = _read(path)
        if population['job_id'] != job['id']:
            raise ValueError('Le recensement ne correspond pas à la préparation.')
        return population
    population = _build_population(directory, job)
    _write_once(path, population)
    _progress(directory, job, 'preparing_features', 0, len(population['requests']))
    return population


def _build_population(directory, job):
    days, selected = [], set()
    # Freeze ALL decision populations before any snapshot or later label join.
    for decision in job['policy']['decisions']:
        state = temporal.state_as_of(directory/'source.sqlite3', decision['decision_at'])
        day = census_day(state, decision['day'], already_selected=selected)
        selected.update(row['match_id'] for row in day['targets'])
        days.append(day)
    requests = [dict(match_id=row['match_id'], decision_at=row['decision_at']) for day in days for row in day['targets']]
    requests.sort(key=lambda row: (row['decision_at'], int(row['match_id'])))
    population = _sealed(dict(version=VERSION, job_id=job['id'], days=days, requests=requests,
        requested=len(requests), unique_fixtures=len(selected), selection_used_outcomes=False,
        temporal_quality='retrospective_conditional', prospective_confirmation=False))
    return population


def _key(request):
    return features.digest(request)


def _result(directory, request):
    path = directory/'results'/(_key(request)+'.json')
    if path.is_symlink():
        raise ValueError('Chemin de résultat partiel invalide.')
    if not path.exists():
        return None
    value = _read(path)
    if value['request'] != request or value.get('kind') not in ('snapshot', 'exclusion'):
        raise ValueError('Un résultat partiel ne correspond pas à sa demande.')
    if value['kind'] == 'snapshot':
        from prematch_feature_snapshot import validate
        snapshot = validate(value['snapshot'])
        if snapshot['fixture']['match_id'] != request['match_id'] or snapshot['decision_at'] != request['decision_at']:
            raise ValueError('L’instantané partiel ne correspond pas à sa cible.')
    return value


def run(directory, *, batch_size=50):
    """Advance a work tranche; subsequent calls resume without changing policy."""
    if type(batch_size) is not int or batch_size < 1:
        raise ValueError('La taille de lot doit être un entier positif.')
    directory, job = _check_job(directory, source=True)
    if (directory/'COMPLETE.json').exists():
        return _read(directory/'summary.json')
    population = _population(directory, job)
    requests = population['requests']
    completed = { _key(row): value for row in requests if (value := _result(directory, row)) is not None }
    prepared, at, advanced = None, None, 0
    for request in requests:
        if _key(request) in completed:
            continue
        if advanced >= batch_size:
            break
        if at != request['decision_at']:
            prepared = features.FeaturePreparation(temporal.state_as_of(directory/'source.sqlite3', request['decision_at']))
            at = request['decision_at']
        try:
            snapshot, context = prepared.snapshot(request['match_id'])
            value = dict(request=request, kind='snapshot', snapshot=snapshot,
                         history_quality_exclusions=context['history_quality_exclusions'])
        except features.PreparationError as error:
            value = dict(request=request, kind='exclusion', reason=error.code, detail=str(error))
        value = _sealed(value)
        _write_once(directory/'results'/(_key(request)+'.json'), value)
        completed[_key(request)] = value
        advanced += 1
        _progress(directory, job, 'preparing_features', len(completed), len(requests))
    if len(completed) != len(requests):
        return status(directory)
    entries, snapshots, exclusions = [], {}, []
    for request in requests:
        result = completed[_key(request)]
        if result['kind'] == 'snapshot':
            snapshot = result['snapshot']; snapshots[snapshot['id']] = snapshot
            entries.append(dict(**request, snapshot_id=snapshot['id'], history_quality_exclusions=result['history_quality_exclusions']))
        else:
            exclusions.append(dict(**request, reason=result['reason'], detail=result['detail']))
    manifest = _sealed(dict(version=features.VERSION, kind='feature_inputs_only', requests=requests,
        entries=entries, exclusions=exclusions, requested=len(requests), included=len(entries), excluded=len(exclusions),
        unique_fixtures=len(entries), implementation=features.implementation(), population_manifest_id=population['id'],
        population='all tracked eligible NS fixtures under frozen daily UTC policy; no outcome or support selection',
        temporal_quality='retrospective_conditional', prospective_confirmation=False,
        automatic_model_training=False, active_model_changed=False))
    outcomes = features.attach_outcomes(directory/'source.sqlite3', manifest, snapshots,
                  outcome_at=job['policy']['outcome_at']) if job['policy']['outcome_at'] is not None else None
    # A crash during final file emission can leave an incomplete bundle. Do not
    # overwrite it. Reuse exclusive partial files only after checking equality.
    _write_final_bundle(directory/'bundle', manifest, snapshots, outcomes)
    bundle = artifacts.load_bundle(directory/'bundle')
    summary = _summary(population, bundle)
    _exclusive_or_equal(directory/'summary.json', summary)
    complete = _sealed(dict(version=VERSION, state='complete', job_id=job['id'],
        population_id=population['id'], package_id=bundle['package']['id'], summary_id=summary['id']))
    _exclusive_or_equal(directory/'COMPLETE.json', complete)
    _progress(directory, job, 'prepared', len(requests), len(requests))
    return summary


def _exclusive_or_equal(path, value):
    if path.is_symlink():
        raise ValueError('Chemin de fichier final invalide.')
    if path.exists():
        if json.loads(path.read_text(encoding='utf-8')) != value:
            raise ValueError('Un fichier final partiel diffère de la reprise calculée.')
    else:
        _write_once(path, value)


def _write_final_bundle(directory, manifest, snapshots, outcomes):
    for path in (directory, directory/'snapshots'):
        if path.is_symlink() or not path.resolve().is_relative_to(directory.parent.resolve()):
            raise ValueError('Chemin de paquet final invalide.')
    directory.mkdir(exist_ok=True); (directory/'snapshots').mkdir(exist_ok=True)
    files = {'cohort.json': manifest}
    files.update({'snapshots/'+sid+'.json': row for sid, row in snapshots.items()})
    if outcomes is not None:
        files['outcomes.json'] = outcomes
    for name, value in files.items():
        _exclusive_or_equal(directory/name, value)
    complete = _sealed(dict(version='causal-feature-bundle-v1', state='complete', cohort_id=manifest['id'],
                            files={name: features.digest(value) for name, value in files.items()}))
    _exclusive_or_equal(directory/'COMPLETE.json', complete)


def _summary(population, bundle):
    outcomes = bundle['outcomes'] or dict(examples=[], exclusions=[])
    labelled = {row['snapshot_id']: row for row in outcomes['examples']}
    days = []
    for census in population['days']:
        entries = [row for row in bundle['manifest']['entries'] if row['decision_at'] == census['decision_at']]
        ids = {row['snapshot_id'] for row in entries}
        supports = [bundle['snapshots'][sid]['references'] for sid in ids]
        labels = [row for sid, row in labelled.items() if sid in ids]
        days.append(dict(day=census['day'], decision_at=census['decision_at'], targets=census['candidate_count'],
            snapshots=len(ids), labelled=len(labels), result_exclusions=len(ids)-len(labels),
            personalized=sum(row['personalized'] for row in supports),
            historical_rows=sum(row['training'] for row in supports),
            classes=dict(Counter(row['label'] for row in labels))))
    return _sealed(dict(version=VERSION, state='prepared', population_id=population['id'],
        cohort_id=bundle['manifest']['id'], requested=population['requested'], snapshots=bundle['manifest']['included'],
        feature_exclusions=bundle['manifest']['excluded'], labelled=len(labelled), days=days,
        temporal_quality='retrospective_conditional', prospective_confirmation=False,
        active_model_changed=False, predictive_improvement_demonstrated=False))


def status(directory):
    """Small progress files only; no source DB or full snapshot matrix read."""
    directory, job = _check_job(directory)
    progress = _read(directory/'progress.json')
    if progress.get('job_id') != job['id'] or progress.get('version') != VERSION:
        raise ValueError('La progression ne correspond pas à la préparation.')
    result = dict(progress=progress, policy=job['policy'])
    if (directory/'summary.json').exists() and (directory/'COMPLETE.json').exists():
        summary = _read(directory/'summary.json'); complete = _read(directory/'COMPLETE.json')
        if complete['job_id'] != job['id'] or complete['summary_id'] != summary['id']:
            raise ValueError('Le bilan ne correspond pas au marqueur final.')
        result['summary'] = summary
        if (directory/'verification.json').exists():
            verification = _read(directory/'verification.json')
            if (verification.get('state') != 'verified' or verification.get('job_id') != job['id']
                    or verification.get('package_id') != complete['package_id']
                    or verification.get('summary_id') != summary['id']):
                raise ValueError('La vérification ne correspond pas au vivier préparé.')
            result['verification'] = verification
    return result


def verify(directory):
    directory, job = _check_job(directory, source=True)
    population = _read(directory/'population.json')
    complete = _read(directory/'COMPLETE.json')
    summary = _read(directory/'summary.json')
    bundle = artifacts.load_bundle(directory/'bundle')
    if (population != _build_population(directory, job) or complete.get('version') != VERSION
            or complete.get('state') != 'complete' or complete['job_id'] != job['id']
            or complete['population_id'] != population['id'] or complete['package_id'] != bundle['package']['id']
            or bundle['manifest'].get('population_manifest_id') != population['id']
            or summary != _summary(population, bundle) or complete['summary_id'] != summary['id']
            or population['requests'] != bundle['manifest']['requests']):
        raise ValueError('Les dénominateurs ou empreintes du vivier sont incohérents.')
    # The cohort must also correspond to the full fixed source, not merely to
    # internally consistent inputs hand-edited after the census.
    at, prepared = None, None
    entries = {row['match_id']: row for row in bundle['manifest']['entries']}
    excluded = {row['match_id']: row for row in bundle['manifest']['exclusions']}
    for request in population['requests']:
        if at != request['decision_at']:
            prepared = features.FeaturePreparation(temporal.state_as_of(directory/'source.sqlite3', request['decision_at']))
            at = request['decision_at']
        try:
            snapshot, context = prepared.snapshot(request['match_id'])
        except features.PreparationError as error:
            if excluded.get(request['match_id']) != dict(**request, reason=error.code, detail=str(error)):
                raise ValueError('Une exclusion diffère de la source figée.')
        else:
            row = entries.get(request['match_id'])
            if (row is None or bundle['snapshots'][row['snapshot_id']] != snapshot
                    or row['history_quality_exclusions'] != context['history_quality_exclusions']):
                raise ValueError('Un instantané diffère de la source figée.')
    if job['policy']['outcome_at'] is not None:
        outcomes = features.attach_outcomes(directory/'source.sqlite3', bundle['manifest'], bundle['snapshots'],
                                           outcome_at=job['policy']['outcome_at'])
        if outcomes != bundle['outcomes']:
            raise ValueError('Les labels conservés diffèrent de la source figée.')
    verification = _sealed(dict(version=VERSION, state='verified', job_id=job['id'],
        population_id=population['id'], summary_id=summary['id'], package_id=bundle['package']['id'],
        source_sha256=job['source_sha256'], checked_at=datetime.now(timezone.utc).isoformat(),
        prospective_confirmation=False, active_model_changed=False))
    _atomic(directory/'verification.json', verification)
    return dict(job=job, population=population, bundle=bundle, summary=summary)
