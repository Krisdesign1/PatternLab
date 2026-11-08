"""Immutable daily CatBoost candidates for future individual-match evidence.

One fixed recipe is pinned per competition. A changed historical snapshot can
produce one new isolated model; nothing here publishes a serving model, chooses
parameters from future results, calls a provider, or rewrites old forecasts.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import fcntl
import json
import math
import os
import re
import shutil
import tempfile

import api_football as api
import catboost_scenarios as ml
import cloud_storage
from club_identity import fixture_key
from league_model import VALUES


VERSION = 'daily-learning-models-v1'
PROTOCOL_VERSION = 'daily-individual-learning-v1'
SOURCE = 'API-Football'
MIN_HISTORY = 30
MIN_DAYS = 3
_PROJECT = Path(__file__).resolve().parent
_IMPLEMENTATION_FILES = (
    'daily_learning_models.py', 'catboost_scenarios.py', 'scenario_features.py',
    'match_predictor.py', 'club_identity.py', 'league_model.py',
    'daily_learning_tracking.py', 'daily_learning_worker.py',
)


class DailyLearningError(ValueError):
    """An isolated candidate or its temporal provenance failed validation."""


def _root(root=None):
    return Path(api.DATA_DIR if root is None else root).resolve()


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return sha256(_json(value).encode()).hexdigest()


def _file_digest(path):
    try:
        return sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        raise DailyLearningError(f'Le fichier {Path(path).name} du candidat est absent ou illisible.') from None


def _stamp(value):
    if not isinstance(value, str):
        raise DailyLearningError('Un instant UTC daté est nécessaire.')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        raise DailyLearningError('Un instant UTC avec fuseau est nécessaire.') from None


def _clock(root, now=None):
    # Application calls cannot backdate a real candidate or enrolment.
    if root == Path(api.DATA_DIR).resolve() or now is None:
        return datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise DailyLearningError('Une horloge avec fuseau est nécessaire.')
    return now.astimezone(timezone.utc)


def _league(value):
    try:
        return api._numeric_id(value, 'de compétition')
    except ValueError as error:
        raise DailyLearningError(str(error)) from None


def _candidate_id(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise DailyLearningError('Identifiant de candidat invalide.')
    return value


def _read(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError, TypeError):
        raise DailyLearningError(f'Le fichier {Path(path).name} du suivi quotidien est illisible.') from None


def _write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Publish a complete file exclusively. An interrupted JSON write must not
    # occupy the immutable name and make a later restart permanently fail.
    descriptor, temporary = tempfile.mkstemp(prefix='.daily-json-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            handle.write(_json(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _sealed(value):
    return dict(value, id=_digest(value))


def _check_seal(value):
    if value.get('id') != _digest({key: item for key, item in value.items() if key != 'id'}):
        raise DailyLearningError('Les règles du suivi quotidien ont été modifiées.')


def _implementation():
    return {name: _file_digest(_PROJECT/name) for name in _IMPLEMENTATION_FILES}


def _write_allowed():
    cloud_storage._assert_writable()
    if cloud_storage.configured():
        raise DailyLearningError('Les candidats quotidiens exigent le stockage local.')


def protocol_path(root=None):
    return _root(root)/'daily_learning'/'protocol.json'


def prepare_protocol(root=None, *, now=None):
    """Enrol one immutable procedure, using the actual clock for real data."""
    _write_allowed()
    root = _root(root)
    existing = load_protocol(root)
    if existing is not None:
        return existing
    current = _clock(root, now)
    value = _sealed(dict(version=PROTOCOL_VERSION, created_at=current.isoformat(),
        start_at=current.isoformat(), source=SOURCE, labels=list(VALUES),
        model_version=VERSION, implementation=_implementation(),
        population='First genuinely future NS fixture per protocol and API fixture ID, compared on the same individual matches. Each fixture counts once across daily versions.',
        task='1st/Égal/2nd from regulation half-time and full-time scores.',
        training='One fixed-recipe CatBoost refit on all usable results strictly before the current UTC day; whole-day causal features. No daily grid, calibration fitting or historical candidate selection.',
        recipe='First usable incumbent selection.params, pinned once per league; otherwise the first declared CatBoost candidate, pinned and labelled as an unvalidated fallback.',
        unchanged='Unchanged history, recipe and implementation reopen the same candidate; clock/date alone never make a new model or new evidence.',
        evaluation='Future first-capture evidence only. No historical fit or in-sample score is called validation.',
        primary='Paired individual class logloss of the daily candidate procedure versus same-capture league frequencies.',
        references=['clubs', 'frequency', 'incumbent'],
        pending='Missing, unresolved and void scores stay outside the common settled denominator.',
        replacement='No automatic promotion, serving change, threshold adjustment or rewritten forecast. A separate versioned decision is required.',
        retrospective_validation=False, hyperparameter_search=False,
        automatic_promotion=False, promised_accuracy=None))
    try:
        _write_new(protocol_path(root), value)
    except FileExistsError:
        return load_protocol(root)
    return value


def load_protocol(root=None):
    path = protocol_path(root)
    if not path.exists():
        return None
    value = _read(path)
    _check_seal(value)
    if (value.get('version') != PROTOCOL_VERSION or value.get('model_version') != VERSION
            or value.get('source') != SOURCE or value.get('labels') != list(VALUES)
            or value.get('implementation') != _implementation()
            or _stamp(value.get('created_at')) > _stamp(value.get('start_at'))):
        raise DailyLearningError('Le protocole quotidien est incompatible avec les modèles actuels.')
    return value


def _base(root):
    return _root(root)/'daily_learning'


def _path(league, candidate_id, root=None):
    return _base(root)/'candidates'/_league(league)/_candidate_id(candidate_id)


def _production(root):
    return Path(ml.DATA_DIR) if root == Path(api.DATA_DIR).resolve() else root/'models'


@contextmanager
def _lock(root, league):
    base = _base(root)/'candidates'/_league(league)
    base.mkdir(parents=True, exist_ok=True)
    with (base/'training.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DailyLearningError('Un candidat de cette compétition est déjà en préparation.') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _params(value):
    if (not isinstance(value, dict) or set(value) != {'depth', 'iterations', 'l2_leaf_reg'}
            or type(value['depth']) is not int or not 1 <= value['depth'] <= 12
            or type(value['iterations']) is not int or not 1 <= value['iterations'] <= 3000
            or isinstance(value['l2_leaf_reg'], bool) or not isinstance(value['l2_leaf_reg'], (int, float))
            or not math.isfinite(value['l2_leaf_reg']) or value['l2_leaf_reg'] <= 0):
        raise DailyLearningError('La recette CatBoost fixe est invalide.')
    return dict(value)


def _recipe(league, root, protocol):
    path = _base(root)/'leagues'/f'{_league(league)}.json'
    if path.exists():
        value = _read(path)
        _check_seal(value)
        if (value.get('league') != _league(league) or value.get('protocol_id') != protocol['id']
                or value.get('version') != VERSION):
            raise DailyLearningError('La recette figée ne correspond pas au protocole.')
        _params(value.get('params'))
        return value
    incumbent = ml.load_report(SOURCE, league, _production(root))
    params = _params(incumbent['selection']['params']) if incumbent else _params(dict(ml.CANDIDATES[0]))
    return _sealed(dict(version=VERSION, protocol_id=protocol['id'], league=_league(league),
        params=params, origin='incumbent_fixed_parameters' if incumbent else 'declared_fallback_not_validated',
        original_incumbent_generation=incumbent['deployment']['generation'] if incumbent else None,
        feature_version=ml._features().FEATURE_VERSION))


def _history(rows, league, cutoff):
    try:
        return ml._rows(rows, league, SOURCE, cutoff)
    except (ValueError, KeyError, TypeError) as error:
        raise DailyLearningError(str(error)) from None


def plan(rows, league, root=None, *, now=None):
    """Read-only readiness and identity; no fit, writes or provider calls."""
    root, league = _root(root), _league(league)
    current = _clock(root, now)
    protocol = load_protocol(root)
    if protocol is None:
        return dict(league=league, ready=False, changed=False, status='not_started',
                    reason='Préparer le protocole quotidien avant tout entraînement.')
    cutoff = current.date().isoformat()
    history = _history(rows, league, cutoff)
    recipe = _recipe(league, root, protocol)
    days = len({row['timestamp'][:10] for row in history})
    labels = {row['value'] for row in history}
    ready = len(history) >= MIN_HISTORY and days >= MIN_DAYS and labels == set(VALUES)
    definition = dict(version=VERSION, protocol_id=protocol['id'], league=league,
        input_fingerprint=ml._fingerprint(history), recipe_id=recipe['id'])
    candidate_id = _digest(definition)
    existing = _path(league, candidate_id, root)/'manifest.json'
    if existing.exists():
        load_candidate(league, candidate_id, root)
    reason = ('' if ready else f'Il faut {MIN_HISTORY} résultats, {MIN_DAYS} journées et les trois classes.')
    return dict(league=league, ready=ready, changed=ready and not existing.exists(),
        status='ready' if ready and not existing.exists() else 'unchanged' if ready else 'insufficient',
        reason=reason, history_matches=len(history), history_count=len(history), history_days=days,
        cutoff_exclusive=cutoff, input_fingerprint=definition['input_fingerprint'],
        history_fingerprint=definition['input_fingerprint'], candidate_id=candidate_id,
        protocol_id=protocol['id'], params=recipe['params'], recipe=recipe, definition=definition)


def _pin_incumbent(root, league, destination):
    report = ml.load_report(SOURCE, league, _production(root))
    if report is None:
        return None
    source = ml._location(SOURCE, league, _production(root))
    generation = report['deployment']['generation']
    model = source/generation/'model.cbm'
    if not model.exists() or _file_digest(model) != report['deployment']['model_sha256']:
        raise DailyLearningError('Le modèle courant à comparer est absent ou altéré.')
    destination.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(model, destination/'model.cbm')
    _write_new(destination/'report.json', report)
    if _file_digest(destination/'model.cbm') != report['deployment']['model_sha256']:
        raise DailyLearningError('La copie du modèle courant est incomplète.')
    if ml.load_report(SOURCE, league, _production(root)) != report:
        raise DailyLearningError('Le modèle courant a changé pendant sa copie.')
    return dict(generation=generation, model_sha256=report['deployment']['model_sha256'],
                report_sha256=_file_digest(destination/'report.json'))


def _fit_definition(frozen, recipe, module):
    return dict(version=VERSION, candidate_id=_digest(frozen['definition']),
        protocol_id=frozen['definition']['protocol_id'], recipe_id=recipe['id'],
        input_sha256=_digest(frozen), params=recipe['params'],
        feature_version=module.FEATURE_VERSION,
        feature_columns=list(module.FEATURE_COLUMNS), categorical_columns=list(module.CATEGORICAL_COLUMNS))


def _fit_checkpoint(path, frozen, recipe, module):
    """Verify the native artifact and its atomic, immutable training record."""
    value = _read(path/'fit'/'checkpoint.json')
    _check_seal(value)
    if (value.get('definition') != _fit_definition(frozen, recipe, module)
            or value.get('model_sha256') != _file_digest(path/'fit'/'model.cbm')
            or _stamp(value.get('trained_at')) < _stamp(frozen['frozen_at'])):
        raise DailyLearningError('L’artefact entraîné ne correspond pas à son entrée figée.')
    return value


def _fit_once(path, frozen, recipe, module, examples, root, now):
    if (path/'fit').exists():
        return _fit_checkpoint(path, frozen, recipe, module)
    model = ml._fit(examples, recipe['params'], list(module.FEATURE_COLUMNS), list(module.CATEGORICAL_COLUMNS))
    with tempfile.TemporaryDirectory(prefix='.daily-fit-', dir=path) as temporary:
        directory = Path(temporary)
        output = directory/'model.cbm'
        model.save_model(str(output), format='cbm')
        if not output.is_file() or not output.stat().st_size:
            raise DailyLearningError('Le modèle quotidien n’a pas été enregistré.')
        with output.open('rb') as handle:
            os.fsync(handle.fileno())
        checkpoint = _sealed(dict(definition=_fit_definition(frozen, recipe, module),
            model_sha256=_file_digest(output), trained_at=_clock(root, now).isoformat()))
        _write_new(directory/'checkpoint.json', checkpoint)
        # Both files become visible together. If the process stops before the
        # final manifest, the next run validates this bundle and never refits.
        os.rename(directory, path/'fit')
    return _fit_checkpoint(path, frozen, recipe, module)


def train(rows, league, root=None, *, now=None):
    """Train once on a new snapshot; completed candidates are never replaced."""
    _write_allowed()
    root, league = _root(root), _league(league)
    prepare_protocol(root, now=now)
    rows = list(rows)
    with _lock(root, league):
        prepared = plan(rows, league, root, now=now)
        if not prepared['ready']:
            raise DailyLearningError(prepared['reason'])
        candidate_id = prepared['candidate_id']
        path = _path(league, candidate_id, root)
        if (path/'manifest.json').exists():
            return load_candidate(league, candidate_id, root)
        recipe_path = _base(root)/'leagues'/f'{league}.json'
        if not recipe_path.exists():
            _write_new(recipe_path, prepared['recipe'])
        recipe = _recipe(league, root, load_protocol(root))
        history = _history(rows, league, prepared['cutoff_exclusive'])
        path.mkdir(parents=True, exist_ok=True)
        input_path = path/'input.json'
        if not input_path.exists():
            value = dict(version=VERSION, frozen_at=_clock(root, now).isoformat(),
                cutoff_exclusive=prepared['cutoff_exclusive'], definition=prepared['definition'], rows=history)
            _write_new(input_path, value)
        frozen = _read(input_path)
        if (frozen.get('definition') != prepared['definition'] or
                ml._fingerprint(frozen.get('rows', [])) != prepared['input_fingerprint']):
            raise DailyLearningError('L’instantané initial du candidat est altéré.')
        # An interrupted run is resumed from its first input, including cutoff.
        cutoff = frozen['cutoff_exclusive']
        history = frozen['rows']
        incumbent_path = path/'incumbent'
        if (path/'incumbent.json').exists():
            incumbent = _read(path/'incumbent.json').get('incumbent')
            _validate_incumbent(path, incumbent)
        else:
            if incumbent_path.exists():
                # No completed pin marker: preserve the interrupted copy for
                # diagnosis; rebuilding it would silently select a new version.
                raise DailyLearningError('La copie initiale du modèle courant est incomplète ; contrôle nécessaire.')
            incumbent = _pin_incumbent(root, league, incumbent_path)
            _write_new(path/'incumbent.json', dict(incumbent=incumbent))
        module = ml._features()
        examples = module.build_examples(history, league, provider='api-football')
        if len(examples) != len(history):
            raise DailyLearningError('Les exemples ne correspondent pas à l’instantané du candidat.')
        protocol = load_protocol(root)
        try:
            checkpoint = _fit_once(path, frozen, recipe, module, examples, root, now)
            model_hash = checkpoint['model_sha256']
            completed = _clock(root, now)
            if completed < _stamp(frozen['frozen_at']):
                raise DailyLearningError('La fin d’entraînement précède sa création.')
            value = dict(version=VERSION, status='completed', source=SOURCE, labels=list(VALUES),
                candidate_id=candidate_id, protocol_id=protocol['id'], league=league,
                definition=prepared['definition'], frozen_at=frozen['frozen_at'], completed_at=completed.isoformat(),
                cutoff_exclusive=cutoff, trained_through=history[-1]['timestamp'][:10],
                input_sha256=_file_digest(input_path), input_fingerprint=prepared['input_fingerprint'],
                history_fingerprint=prepared['input_fingerprint'], history_matches=len(history),
                history_days=len({row['timestamp'][:10] for row in history}), params=recipe['params'],
                recipe_id=recipe['id'], recipe_origin=recipe['origin'], feature_version=module.FEATURE_VERSION,
                feature_columns=list(module.FEATURE_COLUMNS), categorical_columns=list(module.CATEGORICAL_COLUMNS),
                model_sha256=model_hash, fit_checkpoint_id=checkpoint['id'], incumbent=incumbent,
                incumbent_generation=incumbent['generation'] if incumbent else None,
                implementation=protocol['implementation'], retrospective_validation=False,
                evaluation='Future individual first-capture evidence only; no fit score called validation.',
                promoted=False, serving_changed=False)
            value['manifest_sha256'] = _digest(value)
            _write_new(path/'manifest.json', value)
        except Exception:
            error_path = path/f'error-{_clock(root, now).strftime("%Y%m%dT%H%M%S%f")}.json'
            if not error_path.exists():
                _write_new(error_path, dict(status='error', message='Candidat interrompu ; aucune version servie remplacée.',
                                           at=_clock(root, now).isoformat()))
            raise
        return load_candidate(league, candidate_id, root)


def _validate_incumbent(path, incumbent):
    if incumbent is None:
        return
    if (not isinstance(incumbent, dict)
            or _file_digest(path/'incumbent'/'report.json') != incumbent.get('report_sha256')
            or _file_digest(path/'incumbent'/'model.cbm') != incumbent.get('model_sha256')):
        raise DailyLearningError('Le modèle courant figé pour comparaison est altéré.')
    report = _read(path/'incumbent'/'report.json')
    if (report.get('deployment', {}).get('generation') != incumbent.get('generation')
            or report['deployment'].get('model_sha256') != incumbent.get('model_sha256')):
        raise DailyLearningError('L’identité du modèle courant figé est incohérente.')


def load_candidate(league, candidate_id, root=None):
    root, league = _root(root), _league(league)
    protocol = load_protocol(root)
    if protocol is None:
        raise DailyLearningError('Le protocole quotidien est absent.')
    path = _path(league, candidate_id, root)
    manifest = _read(path/'manifest.json')
    if manifest.get('manifest_sha256') != _digest({key: value for key, value in manifest.items() if key != 'manifest_sha256'}):
        raise DailyLearningError('Le manifeste du candidat est altéré.')
    frozen = _read(path/'input.json')
    if (manifest.get('version') != VERSION or manifest.get('status') != 'completed'
            or manifest.get('source') != SOURCE or manifest.get('labels') != list(VALUES)
            or manifest.get('league') != league or manifest.get('candidate_id') != candidate_id
            or manifest.get('protocol_id') != protocol['id'] or manifest.get('implementation') != protocol['implementation']
            or manifest.get('promoted') is not False or manifest.get('serving_changed') is not False
            or _digest(manifest.get('definition')) != candidate_id
            or frozen.get('definition') != manifest.get('definition')
            or manifest.get('input_sha256') != _file_digest(path/'input.json')
            or manifest.get('input_fingerprint') != ml._fingerprint(frozen.get('rows', []))
            or manifest.get('history_fingerprint') != manifest.get('input_fingerprint')
            or manifest.get('history_matches') != len(frozen['rows'])
            or manifest.get('model_sha256') != _file_digest(path/'fit'/'model.cbm')
            or manifest.get('fit_checkpoint_id') != _fit_checkpoint(path, frozen, _recipe(league, root, protocol), ml._features())['id']
            or manifest.get('params') != _recipe(league, root, protocol)['params']
            or manifest.get('recipe_id') != _recipe(league, root, protocol)['id']
            or manifest.get('feature_version') != ml._features().FEATURE_VERSION
            or manifest.get('feature_columns') != list(ml._features().FEATURE_COLUMNS)
            or manifest.get('categorical_columns') != list(ml._features().CATEGORICAL_COLUMNS)
            or manifest.get('cutoff_exclusive') != frozen.get('cutoff_exclusive')
            or manifest.get('frozen_at') != frozen.get('frozen_at')
            or manifest.get('trained_through') != frozen['rows'][-1]['timestamp'][:10]
            or manifest['trained_through'] >= manifest['cutoff_exclusive']
            or _stamp(manifest.get('frozen_at')) < _stamp(protocol['start_at'])
            or _stamp(manifest.get('completed_at')) < _stamp(manifest['frozen_at'])):
        raise DailyLearningError('Le candidat quotidien ne correspond plus à son entrée figée.')
    _validate_incumbent(path, manifest.get('incumbent'))
    return manifest


def latest(league, root=None):
    """Read the latest complete version; never create a pointer or train."""
    league = _league(league)
    base = _base(root)/'candidates'/league
    # Listing generations needs only small sealed manifests. Validate the full
    # snapshot/native artifact of the selected generation once, rather than
    # reopening every historical training snapshot on each service turn.
    candidates = []
    for path in base.glob('*/manifest.json'):
        value = _read(path)
        if value.get('manifest_sha256') != _digest({key:item for key,item in value.items() if key!='manifest_sha256'}):
            raise DailyLearningError('Le manifeste du candidat est altéré.')
        if value.get('candidate_id') != path.parent.name or value.get('league') != league:
            raise DailyLearningError('Identité de candidat incohérente.')
        candidates.append(value)
    if not candidates:return None
    selected=max(candidates,key=lambda row:(row['completed_at'],row['candidate_id']))
    return load_candidate(league,selected['candidate_id'],root)


def _forecast_inputs(manifest, rows, fixtures, root, *, incumbent=False):
    frozen = load_candidate(manifest['league'], manifest['candidate_id'], root)
    if manifest != frozen:
        raise DailyLearningError('La version demandée diffère du manifeste conservé.')
    fixtures = list(fixtures)
    if not fixtures:
        return frozen, [], []
    now = _clock(_root(root))
    if _stamp(frozen['completed_at']) > now:
        raise DailyLearningError('Le candidat n’était pas terminé à cet instant.')
    minimum = now.date().isoformat()
    ids, identities = set(), set()
    for fixture in fixtures:
        try:
            if (str(fixture['league']) != frozen['league'] or fixture.get('provider') != 'api-football'
                    or _league(fixture['home_id']) == _league(fixture['away_id'])):
                raise ValueError()
            mid = api._numeric_id(fixture['match_id'], 'du match')
            stamp = ml._utc_stamp(fixture['timestamp'])
            kickoff = _stamp(stamp+'+00:00')
            if kickoff <= _stamp(frozen['completed_at']):
                raise ValueError()
            identity = fixture_key(fixture, stamp)
            if mid in ids or identity in identities:
                raise ValueError()
            ids.add(mid); identities.add(identity)
            minimum = min(minimum, stamp[:10])
        except (KeyError, TypeError, ValueError):
            raise DailyLearningError('Affiche future, unique, API-Football et de même compétition requise.') from None
    if frozen['cutoff_exclusive'] > minimum:
        raise DailyLearningError('Le modèle a été entraîné après la coupure autorisée des affiches.')
    rows = list(rows)
    # New backfilled/corrected training rows require a new candidate rather than
    # disguising a changed input as the same trained model.
    prefix = _history(rows, frozen['league'], frozen['cutoff_exclusive'])
    if ml._fingerprint(prefix) != frozen['input_fingerprint']:
        raise DailyLearningError('L’historique appris a changé ; préparer une nouvelle version candidate.')
    history = _history(rows, frozen['league'], minimum)
    features = [ml._features().feature_for_fixture(history, fixture, before=minimum) for fixture in fixtures]
    return frozen, fixtures, features


def predict(manifest, rows, fixtures, root=None):
    """Score individual fixtures with the exact isolated candidate artifact."""
    frozen, fixtures, features = _forecast_inputs(manifest, rows, fixtures, root)
    if not fixtures:
        return []
    model = ml._classifier()()
    try:
        model.load_model(str(_path(frozen['league'], frozen['candidate_id'], root)/'fit'/'model.cbm'), format='cbm')
    except Exception:
        raise DailyLearningError('Le modèle quotidien ne peut pas être chargé.') from None
    return ml._predict(model, features, frozen['feature_columns'], frozen['categorical_columns'])


def predict_incumbent(manifest, rows, fixtures, root=None):
    """Use the incumbent pinned before fitting, never today's CURRENT pointer."""
    frozen, fixtures, features = _forecast_inputs(manifest, rows, fixtures, root, incumbent=True)
    if frozen.get('incumbent') is None:
        return None
    if not fixtures:
        return []
    path = _path(frozen['league'], frozen['candidate_id'], root)/'incumbent'
    report = _read(path/'report.json')
    if (report.get('feature_version') != frozen['feature_version']
            or report.get('feature_columns') != frozen['feature_columns']
            or report.get('categorical_columns') != frozen['categorical_columns']
            or report.get('labels') != list(VALUES)):
        raise DailyLearningError('Les caractéristiques du modèle courant figé sont incompatibles.')
    before = min(_clock(_root(root)).date().isoformat(), *(ml._utc_stamp(row['timestamp'])[:10] for row in fixtures))
    if report['deployment']['trained_through'] >= before:
        raise DailyLearningError('Le modèle courant figé avait déjà vu cette période.')
    history = _history(rows, frozen['league'], report['deployment']['cutoff_exclusive'])
    if ml._fingerprint(history) != report['deployment']['fingerprint']:
        raise DailyLearningError('L’historique du modèle courant figé est corrigé ou incomplet.')
    model = ml._classifier()()
    try:
        model.load_model(str(path/'model.cbm'), format='cbm')
    except Exception:
        raise DailyLearningError('Le modèle courant figé ne peut pas être chargé.') from None
    return ml._predict(model, features, frozen['feature_columns'], frozen['categorical_columns'])
