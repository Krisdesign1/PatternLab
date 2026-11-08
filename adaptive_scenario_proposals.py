"""Freeze all explicitly computed prematch proposals, with separate evidence.

Qualification never blocks a method or group size. Legacy threshold selections
remain readable with their original meaning; reading never recreates choices.
"""
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from time import monotonic
import json
from math import isfinite, isclose, fsum

from adaptive_scenario_calibration import VERSION as CALIBRATION_VERSION, context_key, digest, stamp
from adaptive_scenario_selection import VERSION as SELECTION_VERSION, iter_selected

LEGACY_VERSION = 'adaptive-scenario-proposals-v1'
VERSION = 'computed-scenario-proposals-v2'
POLICY_VERSION = 'all-computed-proposals-v1'
POLICY_DIR = Path(__file__).resolve().parent/'data/api_football/scenario_policy'
ARTIFACT_PATH = POLICY_DIR/'calibration.json'
ENABLED_PATH = POLICY_DIR/'ENABLED'
# Resource protection fails the WHOLE affected method, never a saved prefix.
MAX_CAPTURE_BYTES = 384 * 1024
CAPTURE_SECONDS = 5.0


def is_enabled(data_dir=None):
    target = Path(data_dir)/'scenario_policy/ENABLED' if data_dir is not None else ENABLED_PATH
    return target.is_file()


def _copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _check_digest(value):
    if not isinstance(value, Mapping) or value.get('sha256') != digest({k:v for k,v in value.items() if k != 'sha256'}):
        raise ValueError('Empreinte de la sélection ou de sa calibration invalide.')


def _identity(fixtures):
    import api_football as api
    from coupon_sizes import validate_size
    fixtures = list(fixtures)
    validate_size(len(fixtures))
    ids = [api._numeric_id(row['match_id'], 'match_id') for row in fixtures]
    if len(set(ids)) != len(ids):
        raise ValueError('Les affiches de la sélection doivent être distinctes.')
    return ids


def validate_artifact(artifact):
    _check_digest(artifact)
    if artifact.get('schema') != CALIBRATION_VERSION or not isinstance(artifact.get('contexts'), dict):
        raise ValueError('Version de la calibration inconnue.')
    stamp(artifact['created_at'])
    for key, row in artifact['contexts'].items():
        if not isinstance(key, str) or not isinstance(row, dict) or row.get('status') not in ('qualified','not_qualified','unavailable'):
            raise ValueError('Un contexte de calibration est invalide.')
        if row['status'] == 'qualified':
            tau = row.get('threshold')
            if isinstance(tau, bool) or not isinstance(tau, (int,float)) or not isfinite(tau) or not 0 < tau <= 1:
                raise ValueError('Seuil qualifié invalide.')
            for name, minimum in (('tuning_groups',30),('test_groups',20),('tuning_days',3),('test_days',3)):
                if isinstance(row.get(name), bool) or not isinstance(row.get(name), int) or row[name] < minimum:
                    raise ValueError('Effectifs de qualification insuffisants.')
            measured, reference = row.get('test') or {}, row.get('reference_top3') or {}
            if (measured.get('hits',0) <= 0 or measured.get('f05',-1) < reference.get('f05',2)
                    or measured.get('precision',-1) < reference.get('precision',2)):
                raise ValueError('La calibration contredit ses critères de qualification.')
    return artifact


@lru_cache(maxsize=2)
def _read_artifact(path, signature):
    # Signature is a cache key, so a replaced artifact cannot use stale bytes.
    content = Path(path).read_bytes()
    if len(content) > 4*1024*1024:
        raise ValueError('Fichier de calibration anormalement volumineux.')
    return validate_artifact(json.loads(content))


def load_artifact(path=None):
    target = Path(path or ARTIFACT_PATH)
    stat = target.stat()
    return _copy(_read_artifact(str(target.resolve()), (stat.st_ino,stat.st_mtime_ns,stat.st_size)))


def _reason(error):
    # No paths, credentials or raw exception strings enter public snapshots.
    if isinstance(error, (TimeoutError,MemoryError)):
        return 'Sélection trop volumineuse pour être conservée intégralement : aucun préfixe enregistré.'
    return 'Calibration absente ou invalide : aucun scénario retenu par cette politique.'


def legacy_capture(methods, fixtures, *, now, artifact=None, artifact_path=None, check_time=None):
    """Freeze every outcome passing a previously qualified threshold, or none.

    Core distributions/rankings are preserved independently. All probabilities
    retain their absolute mass. This function never manufactures a threshold
    for a method/size/version not qualified by the supplied calibration.
    """
    current = stamp(now)
    fixtures = list(fixtures)
    ids = _identity(fixtures)
    if not isinstance(methods, Mapping) or not methods:
        raise ValueError('Les méthodes de référence doivent être conservées.')
    # The journal also performs provider preflight and a final clock check.
    for fixture in fixtures:
        value = datetime.fromisoformat(fixture['timestamp'].replace('Z','+00:00'))
        value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
        if value <= current:
            raise ValueError('Les scénarios doivent être sélectionnés avant le coup d’envoi.')
    selected, unavailable = {}, {}
    calibration = None
    try:
        calibration = validate_artifact(artifact) if artifact is not None else load_artifact(artifact_path)
        if stamp(calibration['created_at']) > current:
            raise ValueError('Calibration future.')
    except (ValueError,TypeError,KeyError,OSError,OverflowError):
        calibration = None
    deadline, byte_count = monotonic()+CAPTURE_SECONDS, 0
    def check():
        if check_time is not None: check_time()
        if monotonic() >= deadline:
            raise TimeoutError('Capture interrompue.')
    for name, method in methods.items():
        if calibration is None:
            unavailable[name] = _reason(ValueError())
            continue
        key = context_key(name,method,len(ids))
        qualified = calibration['contexts'].get(key)
        if not qualified or qualified.get('status') != 'qualified':
            unavailable[name] = (qualified or {}).get('reason') or 'Aucun seuil qualifié pour cette méthode, version et taille de groupe.'
            continue
        ranking, method_bytes = [], 0
        try:
            tau = qualified['threshold']
            for row in iter_selected(method,tau,check_time=check):
                # Bound serialized payload size, not the number of proposals.
                method_bytes += len(json.dumps(row,separators=(',',':'),ensure_ascii=False).encode())+1
                if byte_count+method_bytes > MAX_CAPTURE_BYTES:
                    raise MemoryError('Le jeu complet dépasse la capacité de sauvegarde.')
                ranking.append(row)
            if not ranking:
                unavailable[name] = 'Aucun scénario n’atteint le seuil qualifié pour cette analyse.'
                continue
            if len(ranking) == 3**len(ids):
                unavailable[name] = 'Le seuil retiendrait toutes les issues possibles : absence de sélection informative.'
                continue
            selected[name] = dict(ranking=ranking,threshold=tau,calibration_key=key,
                calibration=dict(tuning=qualified['tuning'],test=qualified['test'],
                    reference_top3=qualified['reference_top3'],
                    tuning_groups=qualified['tuning_groups'],test_groups=qualified['test_groups'],
                    tuning_days=qualified['tuning_days'],test_days=qualified['test_days'],
                    test_first_day=qualified['test_first_day'],
                    tuning_labels_before=qualified['tuning_labels_before']))
            byte_count += method_bytes
        except (ValueError,TypeError,KeyError,OverflowError,TimeoutError,MemoryError) as error:
            unavailable[name] = _reason(error)
    envelope = dict(schema=LEGACY_VERSION,selection_at=current.isoformat(),source_methods_digest=digest(methods),
        ordered_match_ids=ids,methods=selected,unavailable=unavailable,
        policy=dict(version=SELECTION_VERSION,criterion='probability_at_least_qualified_threshold',
            number_of_scenarios='determined_by_probabilities',normalization='absolute',
            no_qualified_threshold='abstain'),
        artifact_sha256=calibration['sha256'] if calibration is not None else None,
        artifact_created_at=calibration['created_at'] if calibration is not None else None)
    envelope['sha256'] = digest(envelope)
    return _copy(envelope)


def _validate_legacy(envelope, core_methods, fixtures, created_at=None):
    """Cheap saved integrity/shape validation; never reselect on a read path.

    Completeness is attested by capture's completed stream. Source/policy/list
    digests prevent an accidentally edited or truncated list being accepted.
    The outer immutable archive separately hashes this entire envelope.
    """
    _check_digest(envelope)
    if (envelope.get('schema') != LEGACY_VERSION or envelope.get('source_methods_digest') != digest(core_methods)
            or envelope.get('ordered_match_ids') != _identity(fixtures)
            or envelope.get('policy',{}).get('version') != SELECTION_VERSION):
        raise ValueError('La sélection ne correspond plus à ses méthodes ou affiches enregistrées.')
    selected_at = stamp(envelope['selection_at'])
    for fixture in fixtures:
        kickoff = datetime.fromisoformat(fixture['timestamp'].replace('Z','+00:00'))
        kickoff = kickoff.replace(tzinfo=timezone.utc) if kickoff.tzinfo is None else kickoff
        if selected_at >= kickoff:
            raise ValueError('La sélection enregistrée doit précéder les coups d’envoi.')
    if created_at is not None and selected_at > stamp(created_at):
        raise ValueError('La sélection ne peut pas suivre son enregistrement.')
    if envelope.get('artifact_created_at') is not None and stamp(envelope['artifact_created_at']) > selected_at:
        raise ValueError('Calibration indisponible au moment de la sélection.')
    methods, unavailable = envelope.get('methods'),envelope.get('unavailable')
    if (not isinstance(methods,dict) or not isinstance(unavailable,dict) or set(methods)&set(unavailable)
            or set(methods)|set(unavailable) != set(core_methods)
            or any(not isinstance(reason,str) or not reason.strip() for reason in unavailable.values())):
        raise ValueError('Chaque méthode doit conserver ses choix ou sa raison d’abstention.')
    for name, payload in methods.items():
        tau, ranking = payload.get('threshold'),payload.get('ranking')
        if (isinstance(tau,bool) or not isinstance(tau,(int,float)) or not isfinite(tau) or not 0<tau<=1
                or payload.get('calibration_key') != context_key(name,core_methods[name],len(fixtures))
                or not isinstance(ranking,list) or not ranking or len(ranking)>=3**len(fixtures)
                or not envelope.get('artifact_sha256')):
            raise ValueError('Critère ou propositions de sélection invalides.')
        seen, previous = set(),float('inf')
        chances=[]
        for item in ranking:
            sequence,chance = item.get('sequence'),item.get('probability')
            if (not isinstance(sequence,list) or len(sequence)!=len(fixtures)
                    or any(label not in ('1st','Égal','2nd') for label in sequence)
                    or tuple(sequence) in seen or isinstance(chance,bool) or not isinstance(chance,(int,float))
                    or not isfinite(chance) or not tau<=chance<=1 or chance>previous):
                raise ValueError('Liste sélectionnée invalide, répétée ou désordonnée.')
            seen.add(tuple(sequence));previous=chance;chances.append(chance)
        if fsum(chances)>1+1e-9:
            raise ValueError('Masse des scénarios sélectionnés invalide.')
    return envelope


def _evidence(calibration, name, method, size):
    """Describe the available historical comparison without selecting choices."""
    key = context_key(name, method, size)
    row = (calibration or {}).get('contexts', {}).get(key)
    if row is None:
        return dict(status='unavailable', calibration_key=key,
            reason='Comparaison historique indisponible pour cette méthode et cette taille ; les propositions sont conservées.')
    value = dict(status=row['status'], calibration_key=key,
        reason=('Une ancienne politique à seuil a passé une comparaison historique ; ce résultat ne valide pas cet ensemble de propositions.'
                if row['status'] == 'qualified' else
                'Aucun critère qualifié sur cette comparaison historique ; les propositions sont conservées.'))
    value['calibration'] = {field: deepcopy(row[field]) for field in (
        'threshold', 'tuning', 'test', 'reference_top3', 'tuning_groups', 'test_groups',
        'tuning_days', 'test_days', 'test_first_day', 'tuning_labels_before') if field in row}
    return value


def capture(methods, fixtures, *, now, artifact=None, artifact_path=None, check_time=None):
    """Keep the complete supplied rankings for every method, for sizes 2–20.

    No historical threshold is applied. The producer's actual proposals are
    retained verbatim, not replaced by all mathematical distribution outcomes.
    A technical failure raises before archival; it never saves a partial set.
    """
    from scenario_proposal_archive import validate_methods
    current = stamp(now)
    fixtures = list(fixtures)
    ids = _identity(fixtures)
    for fixture in fixtures:
        kickoff = datetime.fromisoformat(fixture['timestamp'].replace('Z', '+00:00'))
        kickoff = kickoff.replace(tzinfo=timezone.utc) if kickoff.tzinfo is None else kickoff
        if kickoff <= current:
            raise ValueError('Les scénarios doivent être enregistrés avant le coup d’envoi.')
    # Validate every explicit row once, without generating or fitting a model.
    saved = validate_methods(methods, len(ids))
    calibration = None
    try:
        candidate = validate_artifact(artifact) if artifact is not None else load_artifact(artifact_path)
        if stamp(candidate['created_at']) <= current:
            calibration = candidate
    except (ValueError, TypeError, KeyError, OSError, OverflowError):
        pass
    selected = {}
    for name, method in saved.items():
        if check_time is not None:
            check_time()
        selected[name] = dict(ranking=deepcopy(method['ranking']),
            evidence=_evidence(calibration, name, method, len(ids)))
    envelope = dict(schema=VERSION, selection_at=current.isoformat(),
        source_methods_digest=digest(methods), ordered_match_ids=ids,
        methods=selected, unavailable={}, policy=dict(version=POLICY_VERSION,
            criterion='all_explicit_computed_rankings', normalization='absolute',
            qualification='information_only', group_sizes='2_to_20'),
        artifact_sha256=calibration['sha256'] if calibration is not None else None,
        artifact_created_at=calibration['created_at'] if calibration is not None else None)
    envelope['sha256'] = digest(envelope)
    if len(json.dumps(envelope, ensure_ascii=False).encode()) > MAX_CAPTURE_BYTES:
        raise ValueError('Le jeu complet dépasse la capacité de sauvegarde ; aucun scénario n’a été tronqué.')
    return _copy(envelope)


def validate(envelope, core_methods, fixtures, created_at=None):
    """Verify a saved list; never recalculate proposals from later information."""
    if isinstance(envelope, Mapping) and envelope.get('schema') == LEGACY_VERSION:
        return _validate_legacy(envelope, core_methods, fixtures, created_at)
    _check_digest(envelope)
    if (envelope.get('schema') != VERSION
            or envelope.get('source_methods_digest') != digest(core_methods)
            or envelope.get('ordered_match_ids') != _identity(fixtures)
            or envelope.get('policy') != dict(version=POLICY_VERSION,
                criterion='all_explicit_computed_rankings', normalization='absolute',
                qualification='information_only', group_sizes='2_to_20')
            or envelope.get('unavailable') != {}
            or not isinstance(envelope.get('methods'), dict)
            or set(envelope['methods']) != set(core_methods)):
        raise ValueError('Les propositions ne correspondent plus au calcul enregistré.')
    selected_at = stamp(envelope['selection_at'])
    for fixture in fixtures:
        kickoff = datetime.fromisoformat(fixture['timestamp'].replace('Z', '+00:00'))
        kickoff = kickoff.replace(tzinfo=timezone.utc) if kickoff.tzinfo is None else kickoff
        if selected_at >= kickoff:
            raise ValueError('Les propositions enregistrées doivent précéder les coups d’envoi.')
    if created_at is not None and selected_at > stamp(created_at):
        raise ValueError('Les propositions ne peuvent pas suivre leur enregistrement.')
    if envelope.get('artifact_created_at') is not None and stamp(envelope['artifact_created_at']) > selected_at:
        raise ValueError('Comparaison historique postérieure au calcul.')
    from scenario_proposal_archive import _ranking
    for name, method in envelope['methods'].items():
        _ranking(method, len(fixtures))
        if digest(method['ranking']) != digest(core_methods[name]['ranking']):
            raise ValueError('La liste enregistrée ne conserve pas toutes les propositions calculées.')
        evidence = method.get('evidence')
        if (not isinstance(evidence, dict)
                or evidence.get('status') not in ('qualified', 'not_qualified', 'unavailable')
                or not isinstance(evidence.get('reason'), str) or not evidence['reason'].strip()
                or evidence.get('calibration_key') != context_key(name, core_methods[name], len(fixtures))):
            raise ValueError('Le niveau de preuve séparé est invalide.')
    return envelope


def effective_methods(entry_or_envelope, core_methods=None):
    """Return the exact saved proposals, preserving legacy policy semantics."""
    if core_methods is None:
        entry = entry_or_envelope
        core_methods = entry.get('methods') or entry.get('comparison_methods') or {}
        envelope = entry.get('proposal_selection')
        if envelope is None:
            return deepcopy(core_methods)
        validate(envelope, core_methods, entry['fixtures'], entry.get('created_at'))
    else:
        envelope = entry_or_envelope
        _check_digest(envelope)
        if envelope.get('source_methods_digest') != digest(core_methods):
            raise ValueError('Méthodes différentes des propositions sauvegardées.')
    output = {}
    for name, value in envelope['methods'].items():
        if envelope['schema'] == LEGACY_VERSION:
            policy = dict(version=SELECTION_VERSION, threshold=value['threshold'],
                calibration_key=value['calibration_key'], artifact_sha256=envelope['artifact_sha256'])
        elif envelope['schema'] == VERSION:
            policy = dict(version=POLICY_VERSION, evidence=deepcopy(value['evidence']))
        else:
            raise ValueError('Version des propositions inconnue.')
        output[name] = dict(deepcopy(core_methods[name]), ranking=deepcopy(value['ranking']),
            reference_ranking=deepcopy(list(core_methods[name]['ranking'][:3])), proposal_policy=policy)
    return output
