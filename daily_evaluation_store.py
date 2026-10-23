"""Immutable local inputs and reports for the retrospective daily-selection audit.

This module reads the API-Football store once. It never calls a provider, trains
models, reconciles the journal, or changes an existing daily selection. Historical
calendars reconstructed from final data are explicitly retrospective evidence.
"""

import cloud_storage
from contextlib import contextmanager
from datetime import date, datetime, timezone
from hashlib import sha256
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
from zoneinfo import ZoneInfo

import api_football as api

VERSION = 'daily-evaluation-store-v1'
PROTOCOL_VERSION = 'daily-selection-audit-protocol-v1'
SOURCE = 'API-Football'
ZONE = ZoneInfo('Africa/Kinshasa')
RUNS_NAME = 'daily_evaluation_runs'
CODE_FILES = (
    'daily_analysis_evaluation.py', 'daily_evaluation_statistics.py',
    'daily_analysis_engine.py', 'match_predictor.py', 'coupon_sizes.py',
    'league_patterns.py', 'scenario_predictor.py', 'league_model.py',
    'api_football.py', 'api_match_workspace.py', 'daily_evaluation_store.py',
)


class EvaluationStoreError(ValueError):
    """A safe, local audit-storage or input error."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _utc_now():
    return datetime.now(timezone.utc)


def _root(root=None):
    return Path(api.DATA_DIR if root is None else root)


def _date(value, name):
    try:
        if type(value) is date:
            return value
        if isinstance(value, str):
            return date.fromisoformat(value)
    except ValueError:
        pass
    raise EvaluationStoreError(f'La date {name} doit utiliser le format AAAA-MM-JJ.')


def _dates(today, start, end):
    real_today = _utc_now().astimezone(ZONE).date()
    today = real_today if today is None else _date(today, 'de référence')
    start = _date(start, 'de début')
    end = today if end is None else _date(end, 'de fin')
    if today > real_today or end > today or start >= end:
        raise EvaluationStoreError('Choisissez une période passée : début inclus, fin exclue, au plus aujourd’hui à Kinshasa.')
    return today, start, end


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        raise EvaluationStoreError(f'Le fichier local {path.name} est illisible ou invalide.') from None


def _snapshot(path):
    # One read, one parsed snapshot. A concurrent atomic provider-store update
    # cannot combine two generations of input or alter this audit's copy.
    try:
        content = path.read_bytes()
        value = json.loads(content)
        if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('fixtures'), list):
            raise ValueError()
        seen = set()
        for fixture in value['fixtures']:
            mid = api._numeric_id(fixture['fixture']['id'], 'du match')
            if mid in seen:
                raise ValueError()
            seen.add(mid)
        _json(value)  # Reject NaN/Infinity as well as unreadable envelopes.
        return value, sha256(content).hexdigest()
    except (OSError, ValueError, KeyError, TypeError):
        raise EvaluationStoreError('Le fichier local des matchs API-Football est absent ou invalide.') from None


def _code_signature():
    directory = Path(__file__).resolve().parent
    try:
        files = {name: sha256((directory/name).read_bytes()).hexdigest() for name in CODE_FILES}
    except OSError:
        raise EvaluationStoreError('Les modules de l’audit local ne sont pas tous disponibles.') from None
    return sha256(_json(files).encode()).hexdigest(), files


@cloud_storage.persisted
def _write(path, value, *, immutable=False):
    encoded = value if isinstance(value, str) else _json(value)
    name = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent,
                                         prefix='.'+path.name, delete=False) as handle:
            name = handle.name
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if immutable:
            # link is an atomic create-if-absent; unlike replace it cannot
            # rewrite a previously frozen input/report, even after a crash.
            try:
                os.link(name, path)
            except FileExistsError:
                if path.read_text(encoding='utf-8') != encoded:
                    raise EvaluationStoreError(f'Le fichier figé {path.name} ne correspond plus à cet audit.') from None
        else:
            os.replace(name, path)
    except OSError:
        raise EvaluationStoreError('Impossible d’écrire le rapport de l’audit local.') from None
    finally:
        if name is not None:
            Path(name).unlink(missing_ok=True)


@contextmanager
def _lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory/'.audit.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise EvaluationStoreError('Un audit des analyses quotidiennes est déjà en cours.') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _evaluate(raw, **kwargs):
    from daily_analysis_evaluation import evaluate
    return evaluate(raw, **kwargs)


def _paired_summary(records):
    from daily_evaluation_statistics import summarize_paired
    return summarize_paired(records)


def _protocol(today, start, end):
    return dict(version=PROTOCOL_VERSION, source=SOURCE, timezone=ZONE.key,
        today=today.isoformat(), start=start.isoformat(), end_exclusive=end.isoformat(),
        sizes=list(range(2, 21)), history_cutoff='strictly_before_first_target_UTC_day',
        replay_clock='start_of_target_day_Africa/Kinshasa',
        calendar_policy='retrospective_reconstruction_from_frozen_final_provider_states',
        model_policy='causal_replayable_clubs_probabilities_only_no_saved_trained_artifacts',
        selection_policy='daily-data-support-partitions-v1',
        interpretation='retrospective_exploratory_not_prospective_pre_match_evidence')


def _complete(directory, *, expected=None):
    marker = _read_json(directory/'completed.json')
    if not isinstance(marker, dict) or marker.get('version') != VERSION:
        raise EvaluationStoreError('Le marqueur de fin de cet audit est invalide.')
    manifest = _read_json(directory/'manifest.json')
    if not isinstance(manifest, dict) or manifest.get('version') != VERSION or manifest.get('run_id') != directory.name:
        raise EvaluationStoreError('Le manifeste de cet audit est invalide.')
    if expected is not None and manifest != expected:
        raise EvaluationStoreError('Le protocole ou le code de cet audit a changé.')
    identity = {key:manifest.get(key) for key in ('version','input_fingerprint','source_signature','protocol')}
    if (sha256(_json(identity).encode()).hexdigest() != directory.name
            or marker.get('run_id') != directory.name
            or sha256(_json(manifest.get('code_files')).encode()).hexdigest() != manifest.get('source_signature')):
        raise EvaluationStoreError('L’identité du protocole ou du code figé de cet audit a changé.')
    try:
        for name, field in (('input.json','input_sha256'), ('report.json','report_sha256'), ('review.md','review_sha256')):
            if sha256((directory/name).read_bytes()).hexdigest() != marker.get(field):
                raise EvaluationStoreError(f'Le fichier figé {name} de cet audit a changé.')
        if sha256((directory/'input.json').read_bytes()).hexdigest() != manifest.get('input_fingerprint'):
            raise EvaluationStoreError('L’empreinte des données figées ne correspond pas au manifeste.')
    except OSError:
        raise EvaluationStoreError('Un fichier figé de cet audit est absent.') from None
    report = _read_json(directory/'report.json')
    if (not isinstance(report, dict) or report.get('run_id') != directory.name
            or report.get('input_fingerprint') != manifest.get('input_fingerprint')
            or report.get('source_signature') != manifest.get('source_signature')):
        raise EvaluationStoreError('Le rapport ne correspond pas aux données figées de cet audit.')
    return report


def _review(report):
    inputs = report['inputs']
    summary = {key: report[key] for key in ('counts','coverage','metrics','paired_summary','warnings') if key in report}
    return ('# Audit rétrospectif des analyses quotidiennes\n\n'
        f"Source : API-Football. Données figées le {report['created_at']}.\n\n"
        f"Période : {inputs['start']} inclus au {inputs['end_exclusive']} exclu, journée de Kinshasa.\n\n"
        f"Empreinte des données : `{report['input_fingerprint']}`.\n\n"
        f"Empreinte du code : `{report['source_signature']}`.\n\n"
        'Les calendriers sont reconstruits depuis l’état final local. Leur présence et leurs corrections '
        'avant match ne sont pas démontrées. Cet audit ne constitue pas un nouveau test prospectif '
        'indépendant. Aucun résultat en attente n’est traité comme un échec.\n\n'
        'Les probabilités de la référence Clubs sont rejouées avec les seuls jours UTC strictement '
        'antérieurs au premier match cible. Aucun artefact CatBoost/contexte actuel n’est utilisé. '
        'Les analyses peuvent partager des matchs ; les taux de scénarios exacts dépendent de leur taille.\n\n'
        '```json\n'+json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False)+'\n```\n')


@cloud_storage.persisted
def run_audit(*, root=None, today=None, start='2022-01-01', end=None, progress=None):
    """Freeze a local input/protocol, replay it once, then cache the immutable report."""
    today, start, end = _dates(today, start, end)
    root = _root(root)
    snapshot, store_bytes_fingerprint = _snapshot(root/'fixtures.json')
    frozen_input = dict(version=1, source=SOURCE, fixtures=snapshot['fixtures'])
    input_encoded = _json(frozen_input)
    input_fingerprint = sha256(input_encoded.encode()).hexdigest()
    source_signature, code_files = _code_signature()
    protocol = _protocol(today, start, end)
    identity = dict(version=VERSION, input_fingerprint=input_fingerprint,
                    source_signature=source_signature, protocol=protocol)
    run_id = sha256(_json(identity).encode()).hexdigest()
    runs = root/RUNS_NAME
    try:
        with _lock(runs):
            directory = runs/run_id
            directory.mkdir(exist_ok=True)
            manifest_path = directory/'manifest.json'
            if manifest_path.exists():
                manifest = _read_json(manifest_path)
                if not isinstance(manifest, dict) or any(manifest.get(key) != value for key,value in identity.items()):
                    raise EvaluationStoreError('Les données ou le protocole figés de cet audit sont invalides.')
            else:
                manifest = dict(identity, run_id=run_id, created_at=_utc_now().isoformat(),
                    source_store_sha256=store_bytes_fingerprint, source_store_updated_at=snapshot.get('updated_at'),
                    raw_fixtures=len(snapshot['fixtures']), code_files=code_files)
                _write(directory/'input.json', frozen_input, immutable=True)
                _write(manifest_path, manifest, immutable=True)
            # Verify even a resumed partial audit before allowing any calculation.
            if (directory/'input.json').read_text(encoding='utf-8') != input_encoded:
                raise EvaluationStoreError('Les données figées de cet audit ont changé.')
            if (directory/'completed.json').exists():
                report = _complete(directory, expected=manifest)
                _write(runs/'latest.json', dict(version=VERSION,run_id=run_id))
                return report
            def checkpoint(*args, **kwargs):
                event = dict(status='running',updated_at=_utc_now().isoformat(),args=list(args),kwargs=kwargs)
                _write(directory/'progress.json',event)
                if progress is not None:
                    progress(*args, **kwargs)
            _write(directory/'progress.json',dict(status='running',updated_at=_utc_now().isoformat()))
            # Decode a separate copy: the evaluator cannot change the frozen input.
            raw = json.loads(input_encoded)['fixtures']
            result = _evaluate(raw,today=today,start=start.isoformat(),end=end.isoformat(),progress=checkpoint)
            if not isinstance(result,dict) or not isinstance(result.get('paired_records'),list):
                raise EvaluationStoreError('Le calcul de l’audit n’a pas produit un rapport valide.')
            report = dict(result, paired_summary=_paired_summary(result['paired_records']),
                run_id=run_id,created_at=manifest['created_at'],input_fingerprint=input_fingerprint,
                source_signature=source_signature,store_version=VERSION,
                inputs=dict(protocol,raw_fixtures=manifest['raw_fixtures'],
                    data_as_of=manifest['source_store_updated_at'],frozen_at=manifest['created_at'],
                    source_store_sha256=manifest['source_store_sha256']),
                report_path=str(directory/'report.json'))
            _write(directory/'report.json',report,immutable=True)
            _write(directory/'review.md',_review(report),immutable=True)
            completed = dict(version=VERSION,run_id=run_id,completed_at=_utc_now().isoformat(),
                **{field:sha256((directory/name).read_bytes()).hexdigest() for name,field in (
                    ('input.json','input_sha256'),('report.json','report_sha256'),('review.md','review_sha256'))})
            _write(directory/'completed.json',completed,immutable=True)
            _write(directory/'progress.json',dict(status='completed',updated_at=completed['completed_at']))
            _write(runs/'latest.json',dict(version=VERSION,run_id=run_id))
            return report
    except EvaluationStoreError:
        raise
    except (OSError,TypeError,ValueError):
        raise EvaluationStoreError('L’audit local a été interrompu ; ses données figées sont conservées.') from None


def load_latest(root=None):
    """Read a verified completed report, or None; never creates storage or calculates."""
    runs = _root(root)/RUNS_NAME
    pointer = runs/'latest.json'
    if not pointer.exists():
        return None
    value = _read_json(pointer)
    if (not isinstance(value,dict) or value.get('version')!=VERSION
            or not isinstance(value.get('run_id'),str) or not re.fullmatch('[0-9a-f]{64}',value['run_id'])):
        raise EvaluationStoreError('La référence du dernier audit est invalide.')
    return _complete(runs/value['run_id'])
