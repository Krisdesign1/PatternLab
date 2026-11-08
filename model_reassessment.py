"""Immutable local candidates, separate from serving and the first campaign.

The experiment definition and input are saved before fitting. Seasonal tests
are separate benchmarks with different tuning from the native candidate. They
do not evaluate it on new unseen matches. Nothing here promotes a model.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

import api_football as api
import catboost_scenarios as ml
import cloud_storage
import model_campaign as campaign
import seasonal_validation as seasonal


VERSION = 'model-reassessment-v1'
SOURCE = 'API-Football'
DEFAULT_SIZES = (2, 4, 6, 8)


class ReassessmentError(ValueError):
    """An isolated experiment is unavailable, corrupted or already running."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _time(now=None):
    value = now or datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ReassessmentError('Précisez une date avec son fuseau horaire.')
    value = value.astimezone(timezone.utc)
    if value.date() > datetime.now(timezone.utc).date():
        raise ReassessmentError('Une réévaluation ne peut pas utiliser une date future.')
    return value


def _league(value):
    try:
        return api._numeric_id(value, 'de compétition')
    except ValueError as error:
        raise ReassessmentError(str(error)) from None


def _sizes(values):
    values = tuple(values)
    if not values or any(type(v) is not int for v in values) or len(set(values)) != len(values):
        raise ReassessmentError('Choisissez des tailles de groupe distinctes, entre 2 et 20.')
    return tuple(sorted(ml.validate_size(value) for value in values))


def _paths(league, root=None):
    root = Path(root if root is not None else api.DATA_DIR).resolve()
    base = root/'model_reassessments'/_league(league)
    models = Path(ml.DATA_DIR) if root == Path(api.DATA_DIR).resolve() else root/'models'
    return base, models


def _run_path(league, run_id, root=None):
    if not isinstance(run_id, str) or not re.fullmatch('[0-9a-f]{64}', run_id):
        raise ReassessmentError('Identifiant de réévaluation invalide.')
    return _paths(league, root)[0]/run_id


def _read(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        raise ReassessmentError(f'Le fichier {path.name} de réévaluation est illisible.') from None


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent,
                                         prefix='.'+path.name, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(_json(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


@contextmanager
def _lock(base):
    base.mkdir(parents=True, exist_ok=True)
    with (base/'experiment.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReassessmentError('Une réévaluation de cette compétition est déjà en cours.') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _protocol(sizes):
    # File digests prevent silently resuming an interrupted experiment with a
    # changed implementation, even when a developer forgot to bump a version.
    names = ('catboost_scenarios.py', 'scenario_features.py', 'match_predictor.py',
             'seasonal_validation.py', 'scenario_predictor.py', 'league_patterns.py',
             'probability_calibration.py', 'model_reassessment.py')
    directory = Path(__file__).resolve().parent
    implementation = {name: hashlib.sha256((directory/name).read_bytes()).hexdigest()
                      for name in names if (directory/name).exists()}
    return dict(version=VERSION, source=SOURCE, sizes=list(sizes),
        candidates=[dict(p) for p in ml.CANDIDATES], alphas=list(ml.ALPHAS),
        seasonal_candidates=[dict(p) for p in seasonal.CANDIDATES],
        seasonal_alphas=list(seasonal.ALPHAS), seasonal_windows=list(seasonal.WINDOWS),
        seasonal_folds=[dict(train=list(years), validation=val, test=test)
                        for years, val, test in seasonal.FOLDS],
        seasonal_implementation=seasonal._implementation(), implementation=implementation,
        selection='Validation only. Test results never select parameters or a serving model.',
        native_size=4, seasonal_sizes=list(sizes),
        native_selection='Native candidate: parameters and alpha selected jointly on four-match validation; all-history recurrence only.',
        seasonal_selection='native: shared joint params/alpha selection on four-match validation; global chronological group anchors, all training recurrence; same frozen alpha on other sizes.',
        evaluation_procedure='native',
        calibration='Diagnostic reliability bins, Brier and logloss only; no calibration transform fitted.',
        interpretation='Retrospective/exploratory. Historical data may already have been examined.',
        replacement=dict(automatic=False, serving_unchanged=True,
            state='awaiting_new_pre_match_evidence',
            requirements=[
                'Identify exact candidate, incumbent and Clubs versions before future matches.',
                'Record their forecasts before kickoff on the same matches, without replacing old forecasts.',
                'Keep group sizes separate and account for shared fixtures and UTC weeks.',
                'Reserve evaluation rules before observing future outcomes; retrospective gains are insufficient.',
                'Review probabilistic loss, calibration, sample size and temporal stability together.',
                'A separate, explicit versioned decision is required before any serving change.'],
            original_prospective_protocol='unchanged; descriptive, never an automatic promotion rule'))


def _prepare(rows, league, source, sizes, root, now):
    if source != SOURCE:
        raise ReassessmentError('Seule la source API-Football est autorisée.')
    league, sizes, clock = _league(league), _sizes(sizes), _time(now)
    cutoff = clock.date().isoformat()
    history = ml._rows(list(rows), league, SOURCE, cutoff)
    native = campaign._chronological(history)
    season_plan = seasonal.plan(history, league, SOURCE, before=cutoff)
    _, production = _paths(league, root)
    incumbent = ml.load_report(SOURCE, league, production)
    generation = incumbent['deployment']['generation'] if incumbent else None
    new_matches, history_changed = None, False
    if incumbent:
        deployment = incumbent['deployment']
        prefix = ml._rows(history, league, SOURCE, deployment['cutoff_exclusive'])
        history_changed = ml._fingerprint(prefix) != deployment['fingerprint']
        new_matches = sum(row['timestamp'][:10] >= deployment['cutoff_exclusive'] for row in history)
    ready = native['ready'] and season_plan['suitable']
    reasons = ([native['reason']] if not native['ready'] else [])
    if not season_plan['suitable']:
        reasons.extend(season_plan['diagnostics'])
    output = dict(version=VERSION, source=SOURCE, league=league, sizes=list(sizes),
        history_matches=len(history), input_fingerprint=ml._fingerprint(history),
        cutoff=cutoff, native_ready=native['ready'], seasonal_ready=season_plan['suitable'],
        seasonal_folds=season_plan['folds'], ready=ready,
        reason=' '.join(reasons), status='ready' if ready else 'insufficient',
        incumbent_generation=generation, incumbent_history_changed=history_changed, new_matches=new_matches)
    return history, output, clock


def plan(rows, league, source=SOURCE, sizes=DEFAULT_SIZES, root=None, now=None):
    """Read-only readiness; never fit, request the provider, or create files."""
    return _prepare(rows, league, source, sizes, root, now)[1]


def _manifest(league, run_id, root=None):
    path = _run_path(league, run_id, root)
    value = _read(path/'manifest.json')
    if (not isinstance(value, dict) or value.get('version') != VERSION or value.get('source') != SOURCE
            or value.get('league') != _league(league) or value.get('run_id') != run_id
            or value.get('status') not in ('building', 'completed', 'error')
            or not isinstance(value.get('definition'), dict) or _digest(value['definition']) != run_id):
        raise ReassessmentError('Le manifeste de réévaluation est invalide.')
    definition = value['definition']
    protocol = definition.get('protocol', {})
    if (definition.get('source') != SOURCE or definition.get('league') != value['league']
            or value.get('sizes') != protocol.get('sizes')
            or value.get('input_fingerprint') != definition.get('input_fingerprint')
            or value.get('incumbent_generation') != definition.get('incumbent_generation')
            or value.get('policy') != protocol.get('replacement') or value.get('promoted') is not False):
        raise ReassessmentError('Les métadonnées ne correspondent plus aux règles de réévaluation.')
    return value


def list_runs(league, root=None):
    """Read small manifests only; never import CatBoost or the frozen histories."""
    base, _ = _paths(league, root)
    result = [_manifest(league, path.parent.name, root) for path in base.glob('*/manifest.json')]
    return sorted(result, key=lambda item: (item['frozen_at'], item['run_id']), reverse=True)


def load_run(league, run_id, root=None):
    manifest = _manifest(league, run_id, root)
    path = _run_path(league, run_id, root)
    frozen = _read(path/'input.json')
    if (_digest(frozen) != manifest.get('input_sha256')
            or frozen.get('definition') != manifest['definition']
            or frozen.get('cutoff') != manifest.get('cutoff')
            or frozen.get('frozen_at') != manifest.get('frozen_at')
            or len(frozen.get('rows', [])) != manifest.get('history_matches')
            or manifest.get('promoted') is not False):
        raise ReassessmentError('L’entrée ou les métadonnées de réévaluation sont altérées.')
    if manifest['status'] != 'completed':
        return dict(manifest, report=None)
    report = _read(path/'report.json')
    if (_digest(report) != manifest.get('report_sha256') or report.get('run_id') != run_id
            or report.get('source') != SOURCE or report.get('league') != _league(league)
            or report.get('policy') != manifest['definition'].get('protocol')
            or report.get('input_fingerprint') != manifest['definition'].get('input_fingerprint')
            or report.get('input_sha256') != manifest['input_sha256']
            or report.get('cutoff') != manifest['cutoff'] or report.get('frozen_at') != manifest['frozen_at']
            or report.get('promoted') is not False):
        raise ReassessmentError('Le rapport de réévaluation ne correspond plus à son empreinte.')
    native = ml.load_report(SOURCE, league, path/'candidate_models')
    if (native != report.get('native') or native['deployment']['generation'] != manifest.get('candidate_generation')):
        raise ReassessmentError('L’artefact du candidat ne correspond plus au rapport.')
    model_path = ml._location(SOURCE, league, path/'candidate_models')/native['deployment']['generation']/'model.cbm'
    if not model_path.exists() or hashlib.sha256(model_path.read_bytes()).hexdigest() != native['deployment']['model_sha256']:
        raise ReassessmentError('Le modèle natif du candidat est absent ou altéré.')
    return dict(manifest, report=report)


def run(rows, league, source=SOURCE, sizes=DEFAULT_SIZES, root=None, now=None, progress=None):
    """Build or reopen exactly one isolated, reproducible candidate experiment."""
    cloud_storage._assert_writable()
    if cloud_storage.configured():
        raise ReassessmentError('La réévaluation des candidats exige le mode de stockage local.')
    history, planned, clock = _prepare(rows, league, source, sizes, root, now)
    if not planned['ready']:
        raise ReassessmentError(planned['reason'] or 'Historique insuffisant pour la réévaluation.')
    league, sizes = planned['league'], tuple(planned['sizes'])
    protocol = _protocol(sizes)
    # The day/clock is deliberately excluded: unchanged inputs must reopen the
    # same experiment instead of presenting a repeated test as new evidence.
    definition = dict(version=VERSION, league=league, source=SOURCE,
        input_fingerprint=planned['input_fingerprint'], protocol=protocol,
        incumbent_generation=planned['incumbent_generation'])
    run_id = _digest(definition)
    base, _ = _paths(league, root)
    path = _run_path(league, run_id, root)
    with _lock(base):
        if (path/'manifest.json').exists():
            existing = load_run(league, run_id, root)
            if existing['status'] == 'completed':
                return existing
        if not (path/'input.json').exists():
            frozen = dict(definition=definition, rows=history, cutoff=planned['cutoff'], frozen_at=clock.isoformat())
            _write(path/'input.json', frozen)
        else:
            frozen = _read(path/'input.json')
            if frozen.get('definition') != definition or ml._fingerprint(frozen.get('rows', [])) != planned['input_fingerprint']:
                raise ReassessmentError('L’instantané initial de réévaluation est altéré ; reprise refusée.')
        manifest = dict(version=VERSION, source=SOURCE, league=league, run_id=run_id, status='building',
            definition=definition,
            input_sha256=_digest(frozen),
            frozen_at=frozen['frozen_at'], cutoff=frozen['cutoff'], sizes=list(sizes),
            history_matches=len(history), input_fingerprint=planned['input_fingerprint'],
            incumbent_generation=planned['incumbent_generation'], candidate_generation=None,
            promoted=False, policy=protocol['replacement'])
        _write(path/'manifest.json', manifest)
        try:
            if progress:
                progress(.02, 'Entraînement du candidat dans son répertoire séparé…')
            candidate_dir = path/'candidate_models'
            native = ml.load_report(SOURCE, league, candidate_dir)
            if native is None:
                native = ml.train(frozen['rows'], league, SOURCE, directory=candidate_dir, before=frozen['cutoff'])
            if (native['deployment']['fingerprint'] != planned['input_fingerprint']
                    or native['deployment']['cutoff_exclusive'] != frozen['cutoff']):
                raise ReassessmentError('Le candidat ne correspond plus à l’historique figé.')
            evaluation = seasonal.run(frozen['rows'], league, SOURCE, sizes=sizes, before=frozen['cutoff'], procedure='native',
                progress=(lambda fraction, message: progress(.2+.79*fraction, message)) if progress else None)
            report = dict(version=VERSION, source=SOURCE, league=league, run_id=run_id,
                frozen_at=frozen['frozen_at'], cutoff=frozen['cutoff'],
                input_sha256=manifest['input_sha256'],
                generated_at=datetime.now(timezone.utc).isoformat(),
                input_fingerprint=planned['input_fingerprint'], sizes=list(sizes),
                candidate_generation=native['deployment']['generation'],
                incumbent_generation=planned['incumbent_generation'],
                incumbent_history_changed=planned['incumbent_history_changed'],
                policy=protocol, native=native, seasonal=evaluation, promoted=False,
                interpretation='Native test evaluates the native candidate training procedure on historical four-match groups. '
                    'Seasonal folds use the same native selection rule and grouping, with seasonal time boundaries. '
                    'They evaluate this procedure, not future performance of the final all-history candidate. '
                    'Calibration is diagnostic only. Historical results cannot authorize replacement. '
                    'No future candidate forecasts have yet been collected by this experiment.')
            _write(path/'report.json', report)
            manifest.update(status='completed', report_sha256=_digest(report),
                candidate_generation=native['deployment']['generation'], completed_at=report['generated_at'])
            _write(path/'manifest.json', manifest)
            result = load_run(league, run_id, root)
        except Exception:
            manifest.update(status='error', error='Réévaluation interrompue. Le premier instantané est conservé ; aucun modèle courant n’a été remplacé.')
            _write(path/'manifest.json', manifest)
            raise
        # A UI notification failure after a verified commit must not downgrade
        # completed evidence to error or cause the same test to be recomputed.
        if progress:
            try:
                progress(1.0, 'Candidat évalué et conservé ; modèle courant inchangé.')
            except Exception:
                pass
        return result
