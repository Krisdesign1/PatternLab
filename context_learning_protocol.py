"""Frozen evaluation contract for score and historical goal-reaction candidates.

This module performs no provider requests, fitting, serving or publication.
Receipts establish local availability, never provider publication-time proof.
The past diagnostic phase is descriptive; independent confirmation is future.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import re


VERSION = 'context-learning-protocol-v1'
VALUES = ('1st', 'Égal', '2nd')
PHASES = ('train', 'tuning', 'calibration', 'descriptive', 'confirmation')
FIT_PHASES = PHASES[:3]
MINIMUMS = dict(train=100, tuning=30, calibration=30, descriptive=30)
DEFAULT_WINDOWS = {
    'train': dict(start='2026-10-27T00:00:00Z', end='2026-11-01T00:00:00Z'),
    'tuning': dict(start='2026-11-01T00:00:00Z', end='2026-11-03T00:00:00Z'),
    'calibration': dict(start='2026-11-03T00:00:00Z', end='2026-11-05T00:00:00Z'),
    'descriptive': dict(start='2026-11-05T00:00:00Z', end='2026-11-07T00:00:00Z'),
    'confirmation': dict(start='2026-11-08T00:00:00Z', end='2026-11-29T00:00:00Z'),
}
RECIPE = dict(version='context-goal-reaction-recipe-v1', seed=20261107,
    heads=['scores_only', 'scores_coverage', 'scores_coverage_goal_reactions'],
    candidates=[dict(depth=3, iterations=150, l2_leaf_reg=8),
                dict(depth=4, iterations=250, l2_leaf_reg=8)],
    temperatures=[0.75, 1.0, 1.25, 1.5, 2.0], thread_count=2,
    class_order=list(VALUES), class_balancing=False,
    tuning_metric='paired_multiclass_logloss', calibration_phase='calibration',
    same_fixture_population=True, selection_probability_threshold=None)
_RECIPE_JSON = json.dumps(RECIPE, sort_keys=True, ensure_ascii=False)
_CONFIRMATION_JSON = json.dumps(dict(minimum_resolved_fixtures=500,minimum_utc_days=21,
    comparisons=['frequency', 'scores_only', 'scores_coverage'],
    primary_metric='paired_multiclass_logloss',uncertainty='UTC-day blocks; club dependence also reported',
    confidence_level=0.95,automatic_promotion=False),sort_keys=True)


def recipe():
    return json.loads(_RECIPE_JSON)


def _now():
    return datetime.now(timezone.utc)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode()).hexdigest()


def _seal(value):
    return dict(value, id=digest(value))


def _sealed(value):
    if not isinstance(value, dict) or value.get('id') != digest({k:v for k,v in value.items() if k != 'id'}):
        raise ValueError('Modified or incomplete immutable manifest.')


def _instant(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
        if not isinstance(result, datetime) or result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError, AttributeError):
        raise ValueError('An explicit timezone-aware instant is required.') from None


def _hash(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _id(value):
    if type(value) not in (int, str) or not str(value).isascii() or not str(value).isdecimal() or int(value) <= 0:
        raise ValueError('A positive API fixture identifier is required.')
    return str(int(value))


def _windows(value):
    if not isinstance(value, dict) or set(value) != set(PHASES):
        raise ValueError('Five explicit chronological phases are required.')
    result, previous_end = {}, None
    for phase in PHASES:
        row=value[phase]
        if not isinstance(row, dict) or set(row) != {'start', 'end'}:
            raise ValueError('Each phase needs start and end only.')
        start,end=_instant(row['start']),_instant(row['end'])
        if start >= end or previous_end is not None and start < previous_end:
            raise ValueError('Phases must be ordered, nonempty and nonoverlapping.')
        if any((s.hour,s.minute,s.second,s.microsecond) != (0,0,0,0) for s in (start,end)):
            raise ValueError('Phase boundaries must be UTC midnights.')
        result[phase]=dict(start=start.isoformat(),end=end.isoformat());previous_end=end
    return result


def make_protocol(windows=None, *, minimums=None, created_at=None):
    """Freeze a new protocol now; never backdate a prospective declaration."""
    now=_now();created=now if created_at is None else _instant(created_at)
    if abs((now-created).total_seconds()) > 1:
        raise ValueError('A new protocol cannot be backdated or future-dated.')
    periods=_windows(DEFAULT_WINDOWS if windows is None else windows)
    if _instant(periods['descriptive']['end']) > created or _instant(periods['confirmation']['start']) <= now:
        raise ValueError('Descriptive development must be past and confirmation must begin in the future.')
    mins=deepcopy(MINIMUMS if minimums is None else minimums)
    if (not isinstance(mins,dict) or set(mins) != set(PHASES[:-1])
            or any(type(v) is not int or v <= 0 for v in mins.values())):
        raise ValueError('Four positive technical minimums are required.')
    return _seal(dict(version=VERSION,created_at=created.isoformat(),windows=periods,minimums=mins,
        implementation_sha256=sha256(Path(__file__).read_bytes()).hexdigest(),recipe=recipe(),
        decision_minutes_before_kickoff=60, target_status_at_decision='NS',
        missing_context_policy='retain missingness and coverage; no imputed event or injury',
        historical_evidence='strict_local_receipts; previously explored periods are descriptive',
        confirmation=json.loads(_CONFIRMATION_JSON),
        serving_model_changed=False,automatic_promotion=False,provider_publication_time_proven=False))


def validate_protocol(plan):
    _sealed(plan)
    if (plan.get('version') != VERSION or plan.get('windows') != _windows(plan['windows'])
            or plan.get('recipe') != recipe() or plan.get('automatic_promotion') is not False
            or plan.get('serving_model_changed') is not False or plan.get('provider_publication_time_proven') is not False
            or plan.get('decision_minutes_before_kickoff') != 60 or plan.get('target_status_at_decision') != 'NS'
            or plan.get('implementation_sha256') != sha256(Path(__file__).read_bytes()).hexdigest()):
        raise ValueError('Incompatible protocol, recipe, implementation or publication contract.')
    created=_instant(plan['created_at'])
    if (created > _now() or _instant(plan['windows']['descriptive']['end']) > created
            or _instant(plan['windows']['confirmation']['start']) <= created):
        raise ValueError('Invalid protocol declaration chronology.')
    mins=plan.get('minimums')
    if not isinstance(mins,dict) or set(mins) != set(PHASES[:-1]) or any(type(v) is not int or v <= 0 for v in mins.values()):
        raise ValueError('Invalid technical minimums.')
    confirmation=plan.get('confirmation',{})
    if confirmation != json.loads(_CONFIRMATION_JSON):
        raise ValueError('Future confirmation cannot be weakened silently.')


def phase_for(plan, decision_at):
    stamp=_instant(decision_at)
    return next((name for name in PHASES if _instant(plan['windows'][name]['start']) <= stamp
                 < _instant(plan['windows'][name]['end'])),None)


def make_evidence_manifest(sources):
    """Seal individual receipt references and compact historical aggregates.

    Aggregates attest a caller-verified source set through a digest and time
    bounds. This pure module does not independently authenticate that ledger.
    """
    if not isinstance(sources,dict) or any(not _hash(key) or not isinstance(value,dict) for key,value in sources.items()):
        raise ValueError('Sources must be keyed by SHA-256 reference identifiers.')
    return _seal(dict(version='context-learning-evidence-v1',sources=deepcopy(sources),
        provider_publication_time_proven=False,source_ledger_independently_authenticated=False))


def _validate_evidence(evidence):
    _sealed(evidence)
    if (evidence.get('version') != 'context-learning-evidence-v1'
            or evidence.get('provider_publication_time_proven') is not False
            or evidence.get('source_ledger_independently_authenticated') is not False
            or not isinstance(evidence.get('sources'),dict)
            or any(not _hash(key) or not isinstance(value,dict) for key,value in evidence['sources'].items())):
        raise ValueError('Invalid source evidence manifest.')


def _row_issues(plan,row,evidence):
    issues=[]
    try:
        mid=_id(row['match_id']);decision=_instant(row['decision_at']);kickoff=_instant(row['kickoff_utc'])
        if kickoff-decision != timedelta(minutes=60):issues.append('decision_not_H_minus_60')
        if _instant(row['target_observed_at']) >= decision:issues.append('target_not_received_before_decision')
        if row.get('target_status_at_decision') != 'NS':issues.append('target_not_NS_at_decision')
        if row.get('quarantined') is not False:issues.append('target_quality_not_explicitly_accepted')
        phase=phase_for(plan,decision)
        if phase is None:issues.append('decision_outside_windows')
        elif phase_for(plan,kickoff) != phase:issues.append('kickoff_crosses_partition')
        ids=row['feature_source_ids']
        if not isinstance(ids,list) or len(set(ids)) != len(ids) or any(not _hash(key) for key in ids):
            raise ValueError('Invalid feature source references')
        if row.get('feature_evidence_id') != evidence['id']:issues.append('feature_evidence_mismatch')
        times=[]
        for key in ids:
            source=evidence['sources'].get(key)
            if source is None:issues.append('feature_source_unresolved');continue
            if (source.get('provider') != 'api-football' or source.get('provenance') != 'local_import_recorded'
                    or source.get('quarantined') is not False or source.get('eligible') is not True):
                issues.append('feature_source_quality_or_provenance_invalid')
            observed=_instant(source['observed_at']);times.append(observed)
            if observed >= decision:issues.append('feature_not_received_before_decision')
            if source.get('kind') == 'historical_aggregate':
                past=_instant(source['max_kickoff_utc'])
                if (type(source.get('source_count')) is not int or source['source_count'] <= 0
                        or not _hash(source.get('source_ids_sha256'))
                        or type(source.get('max_sequence')) is not int or source['max_sequence'] <= 0
                        or not isinstance(source.get('source_store_uuid'),str)
                        or re.fullmatch('[0-9a-f]{32}',source['source_store_uuid']) is None
                        or not isinstance(source.get('excluded_fixture_ids'),list)
                        or mid not in [_id(v) for v in source.get('excluded_fixture_ids',[])]):
                    issues.append('aggregate_source_scope_invalid')
            else:
                if _id(source['fixture_id']) == mid:issues.append('target_fixture_used_in_features')
                past=_instant(source['kickoff_utc'])
            if past >= decision or observed <= past:issues.append('historical_source_chronology_invalid')
        latest=row.get('features_latest_observed_at')
        if (times and (latest is None or _instant(latest) != max(times))) or (not times and latest is not None):
            issues.append('latest_feature_receipt_mismatch')
        if phase == 'confirmation':
            if row.get('label') is not None or row.get('label_observed_at') is not None:
                issues.append('confirmation_label_supplied')
        elif phase is not None:
            if row.get('label') not in VALUES or row.get('label_status') != 'FT':issues.append('regulation_FT_label_required')
            label_at=_instant(row['label_observed_at'])
            # A label used in a phase must be received before that phase ends;
            # actual model fitting is later and receives only three fit phases.
            cutoff=_instant(plan['windows'][phase]['end'])
            if label_at <= kickoff or label_at >= cutoff or label_at >= _instant(plan['created_at']):
                issues.append('label_not_available_before_phase_cutoff')
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError):
        issues.append('malformed_or_unknown_temporal_evidence')
    return sorted(set(issues))


def row_issues(plan,row,*,evidence):
    validate_protocol(plan);_validate_evidence(evidence)
    return _row_issues(plan,row,evidence)


def partition_rows(plan,rows,*,evidence):
    """Partition the same unique fixtures for all heads; hide future labels."""
    validate_protocol(plan);_validate_evidence(evidence)
    phases={name:[] for name in PHASES};excluded=[];seen=set()
    for row in rows:
        issues=_row_issues(plan,row,evidence)
        try:mid=_id(row['match_id'])
        except (KeyError,TypeError,ValueError):mid=None
        if mid is not None and mid in seen:
            raise ValueError('Duplicate target fixture: one match cannot occupy several partitions.')
        if mid is not None:seen.add(mid)
        if issues:
            excluded.append(dict(match_id=mid,reasons=sorted(set(issues))));continue
        phase=phase_for(plan,row['decision_at']);clean=deepcopy(row)
        if phase == 'confirmation':
            allowed={'match_id','decision_at','kickoff_utc','target_observed_at','target_status_at_decision',
                'quarantined','feature_source_ids','feature_evidence_id','features_latest_observed_at',
                'features','feature_schema_id'}
            clean={key:value for key,value in clean.items() if key in allowed}
        phases[phase].append(clean)
    counts={phase:len(value) for phase,value in phases.items()}
    blockers=[]
    for phase in FIT_PHASES:
        if counts[phase] < plan['minimums'][phase]:blockers.append(phase+':insufficient_examples')
        if set(row['label'] for row in phases[phase]) != set(VALUES):blockers.append(phase+':missing_classes')
    return _seal(dict(version='context-learning-partitions-v1',protocol_id=plan['id'],evidence_id=evidence['id'],
        phases=phases,counts=counts,exclusions=excluded,technical_training_ready=not blockers,
        blockers=blockers,descriptive_evaluation_ready=counts['descriptive'] >= plan['minimums']['descriptive'],
        historical_evaluation_independent=False,confirmation_labels_used=False,
        automatic_promotion=False,serving_model_changed=False))


def development_rows(plan,rows,*,evidence):
    result=partition_rows(plan,rows,evidence=evidence)
    return dict(protocol_id=plan['id'],evidence_id=evidence['id'],
        phases={name:result['phases'][name] for name in FIT_PHASES},
        ready=result['technical_training_ready'],blockers=result['blockers'],
        confirmation_labels_used=False,automatic_promotion=False)


def freeze_candidate(plan,candidate_id,artifact_sha256,*,frozen_at=None):
    """Lock a fitted candidate before future confirmation; never publish it."""
    validate_protocol(plan);now=_now();frozen=now if frozen_at is None else _instant(frozen_at)
    if (not _hash(candidate_id) or not isinstance(artifact_sha256,dict) or not artifact_sha256
            or any(not isinstance(k,str) or not k or not _hash(v) for k,v in artifact_sha256.items())):
        raise ValueError('Immutable candidate and artifact SHA-256 identifiers are required.')
    if (abs((now-frozen).total_seconds()) > 1 or frozen < _instant(plan['created_at'])
            or frozen >= _instant(plan['windows']['confirmation']['start'])):
        raise ValueError('Candidate must be frozen now, after declaration and before future confirmation.')
    return _seal(dict(version='context-learning-candidate-lock-v1',protocol_id=plan['id'],
        candidate_id=candidate_id,artifact_sha256=deepcopy(artifact_sha256),frozen_at=frozen.isoformat(),
        confirmation_window=deepcopy(plan['windows']['confirmation']),automatic_promotion=False,
        serving_model_changed=False,confirmation_evaluated=False))
