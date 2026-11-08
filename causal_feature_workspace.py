"""Explicit temporal feature preparation, shared by service and cohort tools.

No fit, model publication, API request or forecast journal write occurs here.
The current production pipeline remains separate until a versioned validation.
"""
from copy import deepcopy
from datetime import datetime,timezone
from hashlib import sha256
import json
from pathlib import Path

import api_football as api
import api_match_workspace as calendar
import fixture_import_policy as imports
from fixture_temporal import utc_instant
from fixture_temporal_store import state_as_of


VERSION='causal-feature-workspace-v1'


class PreparationError(ValueError):
    def __init__(self,code,message):
        self.code=code
        super().__init__(message)


def digest(value):
    return sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',', ':'),allow_nan=False).encode()).hexdigest()


def implementation():
    root=Path(__file__).resolve().parent
    names=('halftime_domain.py','scenario_features.py','match_predictor.py','club_identity.py',
           'prematch_feature_snapshot.py','causal_feature_workspace.py','fixture_temporal.py',
           'fixture_temporal_store.py','fixture_import_policy.py','fixture_store.py',
           'league_model.py','causal_feature_artifacts.py','api_football.py','api_match_workspace.py')
    return {name:sha256((root/name).read_bytes()).hexdigest() for name in names}


def _ref(source,*,normalized=None):
    value={key:source[key] for key in ('match_id','observation_id','observed_at_utc','time_kind')}
    value['normalized_sha256' if normalized is not None else 'raw_sha256']=digest(
        normalized if normalized is not None else source['raw'])
    return value


class FeaturePreparation:
    """One fixed source state, with a shared normalized history per league/day."""
    def __init__(self,state):
        self.state=deepcopy(state)
        self.selected={source['match_id']:source for source in self.state['selected']}
        self.by_league={}
        for mid,item in self.selected.items():
            try:
                league=api._numeric_id(item['raw']['league']['id'],'de compétition')
            except (ValueError,TypeError,KeyError,AttributeError):
                continue
            self.by_league.setdefault(league,[]).append((mid,item))
        self.history_cache={}

    def history(self,league,cutoff):
        key=(league,cutoff)
        if key not in self.history_cache:
            history,refs,excluded=[],[],[]
            for _,item in self.by_league.get(league,[]):
                old=item['raw']
                try:
                    if calendar._stamp(old).date().isoformat()>=cutoff:
                        continue
                except (ValueError,TypeError,KeyError,AttributeError):
                    continue
                normalized,audit=api._normalize_matches([old])
                if not normalized:
                    excluded.extend(audit['issues'])
                    continue
                row=normalized[0]
                if utc_instant(item['observed_at_utc'])<calendar._stamp(old):
                    excluded.append(dict(match_id=item['match_id'],reason='premature_final_observation'))
                    continue
                history.append(row);refs.append(_ref(item,normalized=row))
            self.history_cache[key]=(history,refs,excluded)
        return self.history_cache[key]

    def snapshot(self,match_id):
        return _from_prepared(self,match_id)


def from_state(state,match_id):
    return FeaturePreparation(state).snapshot(match_id)


def _from_prepared(prepared,match_id):
    """Build one snapshot using only one reconstructed source state at T."""
    from prematch_feature_snapshot import build
    mid=api._numeric_id(match_id,'du match')
    state=prepared.state
    decision=utc_instant(state['as_of_utc'])
    if (state.get('temporal_quality')!='retrospective_conditional'
            or state.get('prospective_confirmation') is not False):
        raise PreparationError('invalid_source_state','La qualification temporelle de la source est invalide.')
    selected=prepared.selected
    source=selected.get(mid)
    if source is None:
        reason=next((item['reason'] for item in state['temporal_exclusions'] if item.get('match_id')==mid),
                    'untracked_target_availability')
        if reason in ('not_observed_before_cutoff','untracked_target_availability','untracked_availability'):
            reason='target_availability_unproven'
        raise PreparationError(reason,'Aucune affiche exploitable n’est attestée avant la décision choisie.')
    raw=source['raw']
    if imports.quality_error(raw):
        raise PreparationError('target_quarantined','L’affiche était en quarantaine à la décision choisie.')
    if api._status_short(raw)!='NS':
        raise PreparationError('target_not_upcoming','L’affiche n’était pas confirmée à venir à cette décision.')
    fixture=calendar.fixture_from_raw(raw)
    if calendar._stamp(raw)<=decision:
        raise PreparationError('decision_after_kickoff','La décision doit précéder le coup d’envoi alors annoncé.')
    cutoff=min(decision.date(),calendar._stamp(raw).date()).isoformat()
    # The target is later than decision and therefore later than the exclusive
    # history day; it cannot enter this shared league history.
    history,refs,excluded=prepared.history(fixture['league'],cutoff)
    try:
        snapshot=build(history,fixture,decision_at=decision,source_refs=refs,target_ref=_ref(source),target_raw=raw)
    except ValueError as error:
        raise PreparationError('invalid_feature_input',str(error)) from None
    context=dict(source_store=deepcopy(state['source_store']),untracked_inventory_count=state['untracked_inventory_count'],
                 history_quality_exclusions=deepcopy(excluded),temporal_exclusions=deepcopy(state['temporal_exclusions']),
                 provenance='local_import_recorded; not provider publication or database commit time')
    return snapshot,context


def prepare_snapshot(path,match_id,*,decision_at):
    """Inference and cohort callers use this exact public preparation path."""
    decision=utc_instant(decision_at)
    if decision>datetime.now(timezone.utc):
        raise PreparationError('future_decision','L’instant de décision ne peut pas être dans le futur.')
    return from_state(state_as_of(path,decision),match_id)


def _request(value):
    if not isinstance(value,dict) or set(value)!={'match_id','decision_at'}:
        raise ValueError('Chaque demande doit contenir uniquement match_id et decision_at.')
    mid=api._numeric_id(value['match_id'],'du match')
    at=utc_instant(value['decision_at'])
    if at>datetime.now(timezone.utc):
        raise ValueError('Les demandes de décision future ne sont pas encore reconstructibles.')
    return dict(match_id=mid,decision_at=at.isoformat())


def prepare_cohort(path,requests):
    """One immutable input per requested fixture, independently of later labels.

    Only explicit requests define this population. This is not an automatically
    recovered historical eligible universe or a new independent test period.
    """
    requests=[_request(item) for item in requests]
    if not requests:
        raise ValueError('Au moins une rencontre est nécessaire.')
    if len({item['match_id'] for item in requests})!=len(requests):
        raise ValueError('Une rencontre ne peut compter deux fois dans la cohorte.')
    requests.sort(key=lambda item:(item['decision_at'],int(item['match_id'])))
    entries,snapshots,exclusions=[],{},[]
    cached_at,cached=None,None
    for request in requests:
        if cached_at!=request['decision_at']:
            cached=FeaturePreparation(state_as_of(path,request['decision_at']));cached_at=request['decision_at']
        try:
            snapshot,context=cached.snapshot(request['match_id'])
            snapshots[snapshot['id']]=snapshot
            # Current revision/inventory counters are read diagnostics, not
            # historical facts. They must not alter a past cohort's identity
            # when only later observations are appended to the source store.
            entries.append(dict(**request,snapshot_id=snapshot['id'],
                                history_quality_exclusions=context['history_quality_exclusions']))
        except PreparationError as error:
            exclusions.append(dict(**request,reason=error.code,detail=str(error)))
    manifest=dict(version=VERSION,kind='feature_inputs_only',requests=requests,entries=entries,exclusions=exclusions,
                  requested=len(requests),included=len(entries),excluded=len(exclusions),
                  unique_fixtures=len(entries),implementation=implementation(),
                  temporal_quality='retrospective_conditional',prospective_confirmation=False,
                  population='explicit requested fixture IDs and decisions; not a reconstructed eligible universe',
                  automatic_model_training=False,active_model_changed=False)
    manifest['id']=digest(manifest)
    return manifest,snapshots


def attach_outcomes(path,manifest,snapshots,*,outcome_at):
    """Add labels AFTER the feature inputs are fixed; no feature is recomputed."""
    from prematch_feature_snapshot import validate
    from causal_feature_artifacts import validate_cohort
    at=utc_instant(outcome_at)
    if at>datetime.now(timezone.utc):
        raise ValueError('L’heure de lecture des résultats ne peut pas être future.')
    validate_cohort(manifest,snapshots)
    ids=[entry['match_id'] for entry in manifest['entries']]
    state=state_as_of(path,at,ids=ids)
    selected={source['match_id']:source for source in state['selected']}
    examples,exclusions=[],[]
    for entry in manifest['entries']:
        mid=entry['match_id'];snapshot=validate(snapshots[entry['snapshot_id']])
        reason=None
        if at<=utc_instant(entry['decision_at']):
            raise ValueError('La lecture des résultats doit suivre toutes les décisions de la cohorte.')
        source=selected.get(mid)
        if source is None:
            reason='result_unavailable_at_cutoff'
        else:
            raw=source['raw']
            try:
                normalized=api.normalize_fixture(raw)
                fixture=snapshot['fixture']
                fields=('match_id','league','home_id','away_id','timestamp')
                if any(str(normalized[key])!=str(fixture[key]) for key in fields):
                    reason='identity_or_schedule_changed'
                elif calendar._stamp(raw)>=at or utc_instant(source['observed_at_utc'])<calendar._stamp(raw):
                    reason='result_before_kickoff'
            except (ValueError,TypeError,KeyError,AttributeError):
                reason='result_not_evaluable'
        if reason:
            exclusions.append(dict(match_id=mid,snapshot_id=entry['snapshot_id'],reason=reason))
            continue
        examples.append(dict(snapshot_id=snapshot['id'],match=deepcopy(snapshot['fixture']),
                             decision_at=snapshot['decision_at'],features=deepcopy(snapshot['features']),
                             label=normalized['value'],outcome_source=_ref(source,normalized=normalized),
                             outcome_row=deepcopy(normalized),ht=normalized['ht'],ft=normalized['ft']))
    result=dict(version=VERSION,kind='labelled_feature_examples',cohort_id=manifest['id'],
                outcome_at=at.isoformat(),examples=examples,exclusions=exclusions,
                evaluated=len(examples),not_evaluable=len(exclusions),
                temporal_quality='retrospective_conditional',prospective_confirmation=False,
                split_status='unpartitioned; not an untouched validation or confirmation set',
                automatic_model_training=False,active_model_changed=False)
    result['id']=digest(result)
    return result
