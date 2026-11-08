"""Explicit UTC development partitions; never trains or promotes a model.

Import timestamps support conditional retrospective reconstruction only. A
fourth period named confirmation cannot upgrade that evidence to prospective.
"""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

import causal_feature_artifacts as artifacts
from causal_feature_workspace import VERSION as COHORT_VERSION, digest
from fixture_temporal import utc_instant
from halftime_domain import CLASS_ORDER as VALUES


VERSION = 'causal-partitions-v1'
PHASES = ('train', 'tuning', 'calibration', 'confirmation')
DEVELOPMENT = PHASES[:3]
MINIMUMS = dict(train=160, tuning=60, calibration=60)


def _seal(value):
    value['id'] = digest(value)
    return value


def _verify_seal(value):
    if not isinstance(value, dict) or value.get('id') != digest({k: v for k, v in value.items() if k != 'id'}):
        raise ValueError('Le manifeste a été modifié ou est incomplet.')


def implementation():
    root = Path(__file__).resolve().parent
    return {name: sha256((root/name).read_bytes()).hexdigest() for name in (
        'causal_partitions.py', 'causal_candidate.py', 'prematch_feature_snapshot.py',
        'causal_feature_workspace.py', 'causal_feature_artifacts.py')}


def _windows(windows):
    if not isinstance(windows, dict) or set(windows) != set(PHASES):
        raise ValueError('Quatre périodes sont nécessaires : train, tuning, calibration, confirmation.')
    output = {}
    last = None
    for name in PHASES:
        row = windows[name]
        if not isinstance(row, dict) or set(row) != {'start', 'end'}:
            raise ValueError('Chaque période exige un début et une fin explicites.')
        start, end = utc_instant(row['start']), utc_instant(row['end'])
        if start >= end or any((v.hour, v.minute, v.second, v.microsecond) != (0, 0, 0, 0) for v in (start, end)):
            raise ValueError('Les périodes doivent couvrir des jours UTC entiers, avec début < fin.')
        if last is not None and start != last:
            raise ValueError('Les quatre périodes doivent être contiguës, sans chevauchement ni trou.')
        output[name] = dict(start=start.isoformat(), end=end.isoformat())
        last = end
    return output


def _minimums(value):
    if not isinstance(value, dict) or set(value) != set(DEVELOPMENT) or any(type(v) is not int or v < 1 for v in value.values()):
        raise ValueError('Les effectifs minimaux des trois périodes doivent être des entiers positifs.')
    return dict(value)


def make_plan(windows, *, minimums=None):
    from causal_candidate import recipe
    return _seal(dict(version=VERSION, kind='frozen_chronological_plan', windows=_windows(windows),
        minimums=_minimums(MINIMUMS if minimums is None else minimums), recipe=recipe(),
        created_at=datetime.now(timezone.utc).isoformat(), implementation=implementation(),
        hypothesis='Check the fixed direct three-class CatBoost recipe and independent temperature calibration on causal inputs.',
        primary_metric='paired multiclass logloss against saved Clubs and league frequency references',
        boundaries='half-open UTC days; decision and kickoff in the same period',
        temporal_quality='retrospective_conditional', prospective_confirmation=False,
        active_model_changed=False, automatic_promotion=False))


def validate_plan(plan):
    from causal_candidate import recipe
    _verify_seal(plan)
    if (plan.get('version') != VERSION or plan.get('kind') != 'frozen_chronological_plan'
            or plan.get('windows') != _windows(plan['windows'])
            or plan.get('minimums') != _minimums(plan['minimums']) or digest(plan.get('recipe')) != digest(recipe())
            or plan.get('implementation') != implementation()
            or plan.get('temporal_quality') != 'retrospective_conditional'
            or plan.get('prospective_confirmation') is not False
            or plan.get('active_model_changed') is not False or plan.get('automatic_promotion') is not False):
        raise ValueError('Plan incompatible : périodes, recette, code ou qualification modifiés.')
    if utc_instant(plan['created_at']) > datetime.now(timezone.utc):
        raise ValueError('La date de création du plan ne peut pas être future.')


def _phase(plan, stamp):
    stamp = utc_instant(stamp)
    return next((name for name in PHASES if utc_instant(plan['windows'][name]['start']) <= stamp
                 < utc_instant(plan['windows'][name]['end'])), None)


def _schema(snapshots):
    from scenario_features import FEATURE_COLUMNS, CATEGORICAL_COLUMNS
    schema = dict(columns=list(FEATURE_COLUMNS), categorical_columns=list(CATEGORICAL_COLUMNS), class_order=list(VALUES))
    if any(row['feature_schema'] != schema for row in snapshots.values()):
        raise ValueError('Tous les instantanés doivent partager le même schéma de variables.')
    return schema


def _blockers(phases, minimums):
    output = []
    for name in DEVELOPMENT:
        if phases[name]['usable_labels'] < minimums[name]:
            output.append(f"{name}: {phases[name]['usable_labels']}/{minimums[name]} labels admissibles")
        missing = [label for label in VALUES if phases[name]['classes'][label] == 0]
        if missing:
            output.append(f"{name}: classes absentes : {', '.join(missing)}")
    return output


def _validate_bundle(bundle):
    artifacts.validate_cohort(bundle['manifest'], bundle['snapshots'])
    now = datetime.now(timezone.utc)
    if any(utc_instant(row['decision_at']) > now for row in bundle['manifest']['requests']):
        raise ValueError('Une décision future ne peut pas être reconstruite depuis un paquet historique.')
    if bundle.get('outcomes') is not None:
        artifacts.validate_outcomes(bundle['outcomes'], bundle['manifest'], bundle['snapshots'])
        if utc_instant(bundle['outcomes']['outcome_at']) > now:
            raise ValueError('La lecture des résultats ne peut pas être datée dans le futur.')


def prepare(bundle, plan):
    """Partition frozen inputs first; never expose confirmation labels in output."""
    validate_plan(plan)
    _validate_bundle(bundle)
    schema = _schema(bundle['snapshots'])
    by_match = {row['match_id']: row for row in bundle['manifest']['entries']}
    source_exclusions = {row['match_id']: row for row in bundle['manifest']['exclusions']}
    labels = {row['snapshot_id']: row for row in (bundle.get('outcomes') or {}).get('examples', [])}
    phases = {name: dict(requested=0, included=0, usable_labels=0,
                        classes={v: 0 for v in VALUES} if name in DEVELOPMENT else None,
                        entries=[], exclusions=[]) for name in PHASES}
    outside = []
    for request in bundle['manifest']['requests']:
        mid = request['match_id']
        name = _phase(plan, request['decision_at'])
        if name is None:
            outside.append(dict(**request, reason='decision_outside_windows'))
            continue
        phase = phases[name]
        phase['requested'] += 1
        if mid in source_exclusions:
            phase['exclusions'].append(dict(**request, stage='input', reason=source_exclusions[mid]['reason']))
            continue
        entry = by_match[mid]
        snapshot = bundle['snapshots'][entry['snapshot_id']]
        kickoff = snapshot['fixture']['timestamp'] + '+00:00'
        if _phase(plan, kickoff) != name:
            phase['exclusions'].append(dict(**request, stage='input', reason='kickoff_crosses_partition'))
            continue
        phase['included'] += 1
        summary = dict(**request, snapshot_id=entry['snapshot_id'], kickoff_utc=kickoff)
        if name == 'confirmation':
            summary['label_status'] = 'withheld'
        else:
            label = labels.get(entry['snapshot_id'])
            if label is None:
                reason = 'label_unavailable'
            elif utc_instant(label['outcome_source']['observed_at_utc']) >= utc_instant(plan['windows'][name]['end']):
                reason = 'label_not_available_before_next_period'
            else:
                reason = None
                phase['usable_labels'] += 1
                phase['classes'][label['label']] += 1
            summary['label_status'] = 'available' if reason is None else reason
            if reason:
                phase['exclusions'].append(dict(**request, stage='label', reason=reason))
        phase['entries'].append(summary)
    blockers = _blockers(phases, plan['minimums'])
    return _seal(dict(version=VERSION, kind='chronological_feature_partitions', plan_id=plan['id'], plan=deepcopy(plan),
        cohort_id=bundle['manifest']['id'], outcomes_id=(bundle.get('outcomes') or {}).get('id'),
        feature_schema=schema, phases=phases, outside_windows=outside,
        requested=bundle['manifest']['requested'], ready=not blockers, blockers=blockers,
        temporal_quality='retrospective_conditional', prospective_confirmation=False,
        confirmation_labels_used=False, active_model_changed=False))


def validate_partition(partition, bundle, plan):
    _verify_seal(partition)
    if partition != prepare(bundle, plan):
        raise ValueError('Le découpage ne correspond pas aux demandes, périodes ou sources figées.')


def development_inputs(partition, bundle, plan=None):
    """Return only development examples; no confirmation fixture or label."""
    plan = plan or partition.get('plan')
    if plan is None:
        raise ValueError('Le plan figé est nécessaire pour préparer le développement.')
    validate_partition(partition, bundle, plan)
    labels = {row['snapshot_id']: row for row in (bundle.get('outcomes') or {}).get('examples', [])}
    phases, snapshots = {}, {}
    for name in DEVELOPMENT:
        phases[name] = []
        for row in partition['phases'][name]['entries']:
            if row['label_status'] == 'available':
                sid = row['snapshot_id']
                phases[name].append(deepcopy(labels[sid]))
                snapshots[sid] = deepcopy(bundle['snapshots'][sid])
    value = _seal(dict(version='causal-development-v1', plan=deepcopy(plan), plan_id=plan['id'],
        partition_id=partition['id'], cohort_id=bundle['manifest']['id'],
        feature_schema=deepcopy(partition['feature_schema']), phases=phases, snapshots=snapshots,
        outcome_at=(bundle.get('outcomes') or {}).get('outcome_at'), minimums=deepcopy(plan['minimums']),
        ready=partition['ready'], blockers=deepcopy(partition['blockers']),
        temporal_quality='retrospective_conditional', prospective_confirmation=False, active_model_changed=False))
    validate_development(value)
    return value


def validate_development(value):
    from prematch_feature_snapshot import validate
    _verify_seal(value)
    validate_plan(value['plan'])
    expected = {'version', 'plan', 'plan_id', 'partition_id', 'cohort_id', 'feature_schema', 'phases',
                'snapshots', 'outcome_at', 'minimums', 'ready', 'blockers', 'temporal_quality',
                'prospective_confirmation', 'active_model_changed', 'id'}
    if (set(value) != expected or value.get('version') != 'causal-development-v1' or value.get('plan_id') != value['plan']['id']
            or set(value['phases']) != set(DEVELOPMENT)
            or value.get('minimums') != value['plan']['minimums']
            or value.get('temporal_quality') != 'retrospective_conditional'
            or value.get('prospective_confirmation') is not False or value.get('active_model_changed') is not False
            or 'confirmation' in value):
        raise ValueError('Le développement ne peut contenir de confirmation ni changer de qualification.')
    for name in ('partition_id', 'cohort_id', 'plan_id'):
        artifacts._identity(value[name])
    if value['outcome_at'] is not None and utc_instant(value['outcome_at']) > datetime.now(timezone.utc):
        raise ValueError('La lecture des résultats de développement ne peut pas être future.')
    seen, snapshot_ids, counts = set(), set(), {}
    for name in DEVELOPMENT:
        rows = value['phases'][name]
        counts[name] = dict(usable_labels=len(rows), classes={v: 0 for v in VALUES})
        order = []
        for row in rows:
            sid = row['snapshot_id']
            snapshot = validate(value['snapshots'][sid])
            mid = snapshot['fixture']['match_id']
            if (sid != snapshot['id'] or mid in seen or 'target_observation' not in snapshot
                    or _phase(value['plan'], row['decision_at']) != name
                    or _phase(value['plan'], snapshot['fixture']['timestamp']+'+00:00') != name
                    or utc_instant(row['outcome_source']['observed_at_utc']) >= utc_instant(value['plan']['windows'][name]['end'])):
                raise ValueError('Un exemple franchit une frontière, arrive trop tard ou est répété.')
            # Reuse the full lot03 result contract, including labels, identity,
            # scores, source hashes and score-free feature parity.
            manifest = dict(id=value['cohort_id'], entries=[dict(snapshot_id=sid)])
            outcomes = _seal(dict(version=COHORT_VERSION, kind='labelled_feature_examples',
                cohort_id=value['cohort_id'], outcome_at=value['outcome_at'], examples=[row], exclusions=[],
                evaluated=1, not_evaluable=0, temporal_quality='retrospective_conditional',
                automatic_model_training=False, active_model_changed=False, prospective_confirmation=False))
            artifacts.validate_outcomes(outcomes, manifest, {sid: snapshot})
            seen.add(mid); snapshot_ids.add(sid)
            counts[name]['classes'][row['label']] += 1
            order.append((utc_instant(row['decision_at']), int(mid)))
        if order != sorted(order):
            raise ValueError('Les exemples doivent rester dans leur ordre chronologique.')
    if snapshot_ids != set(value['snapshots']) or value['feature_schema'] != _schema(value['snapshots']):
        raise ValueError('Les instantanés de développement sont incomplets ou surnuméraires.')
    blockers = _blockers(counts, value['minimums'])
    if value['blockers'] != blockers or value['ready'] is not (not blockers):
        raise ValueError('La disponibilité du développement est incohérente.')


def write_partition_bundle(directory, bundle, plan, partition):
    validate_partition(partition, bundle, plan)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    package = artifacts.write_bundle(directory/'inputs', bundle['manifest'], bundle['snapshots'], bundle.get('outcomes'))
    artifacts._write(directory/'plan.json', plan)
    artifacts._write(directory/'partition.json', partition)
    complete = _seal(dict(version=VERSION, state='complete', plan_id=plan['id'], partition_id=partition['id'],
        input_package_id=package['id'], files={'plan.json': digest(plan), 'partition.json': digest(partition)}))
    artifacts._write(directory/'COMPLETE.json', complete)
    return complete


def load_partition_bundle(directory):
    directory = Path(directory)
    for name in ('COMPLETE.json', 'plan.json', 'partition.json', 'inputs'):
        path = directory/name
        if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError('Chemin de paquet invalide.')
    complete = json.loads((directory/'COMPLETE.json').read_text(encoding='utf-8'))
    _verify_seal(complete)
    if (complete.get('version') != VERSION or complete.get('state') != 'complete'
            or set(complete.get('files', {})) != {'plan.json', 'partition.json'}):
        raise ValueError('Le paquet de partitions est incomplet ou incompatible.')
    plan = json.loads((directory/'plan.json').read_text(encoding='utf-8'))
    partition = json.loads((directory/'partition.json').read_text(encoding='utf-8'))
    if complete['files'] != {'plan.json': digest(plan), 'partition.json': digest(partition)}:
        raise ValueError('Les fichiers de partitions ont été altérés.')
    bundle = artifacts.load_bundle(directory/'inputs')
    if (complete['input_package_id'] != bundle['package']['id'] or complete['plan_id'] != plan['id']
            or complete['partition_id'] != partition['id']):
        raise ValueError('Le paquet ne correspond pas aux entrées figées.')
    validate_partition(partition, bundle, plan)
    return dict(plan=plan, partition=partition, bundle=bundle,
                development=development_inputs(partition, bundle, plan))
