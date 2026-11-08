"""Isolated CatBoost candidates from three chronological development phases.

Confirmation inputs are deliberately absent from the public training contract.
Nothing here calls a provider, publishes a serving model or writes a forecast.
"""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import exp, fsum, isfinite, log
import os
from pathlib import Path
import re

import catboost_scenarios as ml
from causal_feature_workspace import digest
from fixture_temporal import utc_instant
from halftime_domain import CLASS_ORDER, validate_probabilities


VERSION = 'causal-candidate-v1'
_RECIPE_JSON = ('{"version":"causal-catboost-recipe-v1",'
                '"candidates":[{"depth":3,"iterations":150,"l2_leaf_reg":5},'
                '{"depth":5,"iterations":300,"l2_leaf_reg":8}],'
                '"temperatures":[0.5,0.75,1.0,1.25,1.5,2.0,3.0],'
                '"selection_metric":"multiclass_logloss","seed":20261030}')
RECIPE = json.loads(_RECIPE_JSON)
PHASES = ('train', 'tuning', 'calibration')
PROBABILITY_FLOOR = 1e-15


class CandidateError(ValueError):
    """A candidate's data contract, training or saved artifact is invalid."""


def recipe():
    """Return a fresh copy of the one fixed recipe, not a mutable shared rule."""
    return json.loads(_RECIPE_JSON)


def implementation():
    root = Path(__file__).resolve().parent
    return {name: sha256((root/name).read_bytes()).hexdigest() for name in
            ('causal_candidate.py', 'catboost_scenarios.py', 'halftime_domain.py')}


def _seal(value):
    return dict(value, id=digest(value))


def _check_seal(value):
    if not isinstance(value, dict) or value.get('id') != digest({k: v for k, v in value.items() if k != 'id'}):
        raise CandidateError('Le contenu du candidat a été modifié.')


def _hash(value):
    if not isinstance(value, str) or re.fullmatch('[a-f0-9]{64}', value) is None:
        raise CandidateError('Une empreinte du candidat est absente ou invalide.')
    return value


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
        raise CandidateError('Une mesure du candidat est absente ou invalide.')
    return value


def _ordered(examples):
    return sorted(examples, key=lambda row: (row['decision_at'], row['match']['timestamp'], int(row['match']['match_id'])))


def metrics(distributions, labels):
    """Multiclass logloss, Brier sum and favorite accuracy on the same rows."""
    if not distributions or len(distributions) != len(labels) or any(label not in CLASS_ORDER for label in labels):
        raise CandidateError('Des probabilités et labels appariés sont nécessaires.')
    probabilities = [validate_probabilities(value) for value in distributions]
    n = len(labels)
    return dict(count=n,
                logloss=fsum(-log(max(p[label], PROBABILITY_FLOOR)) for p, label in zip(probabilities, labels))/n,
                brier=fsum(fsum((p[c]-int(c == label))**2 for c in CLASS_ORDER)
                           for p, label in zip(probabilities, labels))/n,
                accuracy=sum(max(CLASS_ORDER, key=lambda c: p[c]) == label
                             for p, label in zip(probabilities, labels))/n)


def temperature(probabilities, value):
    """A fixed scalar transform; identity preserves the original distribution."""
    probabilities = validate_probabilities(probabilities)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value <= 0:
        raise CandidateError('La température doit être un nombre positif fini.')
    if value == 1:
        return dict(probabilities)
    logits = {label: log(max(probabilities[label], PROBABILITY_FLOOR))/value for label in CLASS_ORDER}
    largest = max(logits.values())
    weights = {label: exp(logits[label]-largest) for label in CLASS_ORDER}
    total = fsum(weights.values())
    return validate_probabilities({label: weights[label]/total for label in CLASS_ORDER})


def _fit(examples, params, schema, seed):
    """Reuse native pools, with this recipe's explicit seed and no publication."""
    model = ml._classifier()(loss_function='MultiClass', class_names=list(CLASS_ORDER),
                             random_seed=seed, thread_count=2, has_time=True,
                             allow_writing_files=False, verbose=False, **params)
    try:
        model.fit(ml._pool([row['features'] for row in examples], schema['columns'],
                           schema['categorical_columns'], [row['label'] for row in examples]))
    except Exception as error:
        raise CandidateError('Le candidat isolé n’a pas pu être ajusté sur ces variables.') from error
    return model


def _predict(model, examples, schema):
    return ml._predict(model, [row['features'] for row in examples],
                       schema['columns'], schema['categorical_columns'])


def _best_temperature(trials):
    # Prefer the identity transform when losses tie within numerical precision.
    best = min(row['metrics']['logloss'] for row in trials)
    tied = [row for row in trials if row['metrics']['logloss'] <= best+1e-12]
    return min(tied, key=lambda row: (abs(log(row['temperature'])), row['temperature']))


def fit_candidate(development):
    """Fit/tune/refit/calibrate using only validated development inputs.

    The final refit uses train+tuning, never calibration or confirmation.
    Temperature is fitted only on the separate calibration phase. Historical
    local receipts remain conditional evidence, including after this fit.
    """
    from causal_partitions import validate_development
    validate_development(development)
    if not development['ready']:
        raise CandidateError('Le développement n’est pas prêt : '+ '; '.join(development['blockers']))
    frozen_recipe = recipe()
    if digest(development['plan'].get('recipe')) != digest(frozen_recipe):
        raise CandidateError('La recette figée est absente ou incompatible.')
    actual_start = datetime.now(timezone.utc)
    started = actual_start.isoformat()
    if (utc_instant(development['plan']['created_at']) > actual_start
            or any(utc_instant(row['outcome_source']['observed_at_utc']) >= actual_start
                   for name in PHASES for row in development['phases'][name])):
        raise CandidateError('Le plan et tous les labels doivent être connus avant le début réel de l’ajustement.')
    schema = deepcopy(development['feature_schema'])
    phases = {name: _ordered(development['phases'][name]) for name in PHASES}
    trials, selected = [], None
    for params in frozen_recipe['candidates']:
        fitted = _fit(phases['train'], deepcopy(params), schema, frozen_recipe['seed'])
        result = metrics(_predict(fitted, phases['tuning'], schema), [row['label'] for row in phases['tuning']])
        trial = dict(params=deepcopy(params), metrics=result)
        trials.append(trial)
        if selected is None or result['logloss'] < selected['metrics']['logloss']-1e-12:
            selected = trial
    refit = _ordered(phases['train']+phases['tuning'])
    model = _fit(refit, deepcopy(selected['params']), schema, frozen_recipe['seed'])
    raw = _predict(model, phases['calibration'], schema)
    labels = [row['label'] for row in phases['calibration']]
    calibration_trials = [dict(temperature=value, metrics=metrics([temperature(p, value) for p in raw], labels))
                          for value in frozen_recipe['temperatures']]
    chosen = _best_temperature(calibration_trials)
    references = {name: {method: metrics([development['snapshots'][row['snapshot_id']]['references'][method]
                                        for row in phases[name]], [row['label'] for row in phases[name]])
                         for method in ('clubs', 'frequency')} for name in ('tuning', 'calibration')}
    inputs = dict(development_id=development['id'], plan_id=development['plan_id'],
                  partition_id=development['partition_id'], cohort_id=development['cohort_id'],
                  snapshots_sha256=digest(development['snapshots']),
                  phases={name: dict(count=len(rows), sha256=digest(rows),
                                     snapshot_ids=[row['snapshot_id'] for row in rows],
                                     labels={label: sum(row['label'] == label for row in rows) for label in CLASS_ORDER})
                          for name, rows in phases.items()})
    try:
        import catboost
        backend_version = catboost.__version__
    except (ImportError, AttributeError):
        backend_version = 'unavailable'
    report = _seal(dict(version=VERSION, kind='isolated_calibrated_candidate',
                        started_at=started, completed_at=datetime.now(timezone.utc).isoformat(),
                        inputs=inputs, plan=deepcopy(development['plan']), feature_schema=schema, recipe=frozen_recipe,
                        minimums=deepcopy(development['minimums']), trials=trials,
                        selection=dict(params=deepcopy(selected['params']), metric='multiclass_logloss',
                                       tuning_loss=selected['metrics']['logloss']),
                        final_training=dict(phases=['train', 'tuning'], count=len(refit)),
                        calibration=dict(method='scalar_temperature_grid_v1', temperature=chosen['temperature'],
                                         phase='calibration', trials=calibration_trials,
                                         raw_metrics=metrics(raw, labels), calibrated_metrics=chosen['metrics']),
                        references=references,
                        reference_clubs_versions=sorted({row['references']['clubs_version']
                                                        for row in development['snapshots'].values()}),
                        tuning_metrics_used_for_selection=True, calibration_metrics_are_in_sample=True,
                        probability_floor=PROBABILITY_FLOOR, backend_version=backend_version,
                        implementation=implementation(), temporal_quality='retrospective_conditional',
                        prospective_confirmation=False, confirmation_used=False,
                        automatic_promotion=False, active_model_changed=False,
                        predictive_improvement_demonstrated=False))
    validate_report(report)
    return model, report


def validate_report(report):
    """Check the saved recipe, denominators and role separation, not just a seal."""
    _check_seal(report)
    from causal_partitions import validate_plan
    validate_plan(report['plan'])
    if (report.get('version') != VERSION or report.get('kind') != 'isolated_calibrated_candidate'
            or digest(report.get('recipe')) != digest(recipe()) or report.get('probability_floor') != PROBABILITY_FLOOR
            or report['plan']['id'] != report['inputs']['plan_id']
            or report['minimums'] != report['plan']['minimums']
            or digest(report['recipe']) != digest(report['plan']['recipe'])
            or report.get('temporal_quality') != 'retrospective_conditional'
            or report.get('tuning_metrics_used_for_selection') is not True
            or report.get('calibration_metrics_are_in_sample') is not True
            or any(report.get(name) is not False for name in ('prospective_confirmation', 'confirmation_used',
                    'automatic_promotion', 'active_model_changed', 'predictive_improvement_demonstrated'))):
        raise CandidateError('Le candidat ne respecte pas sa recette ou sa qualification.')
    schema = report['feature_schema']
    from scenario_features import FEATURE_COLUMNS, CATEGORICAL_COLUMNS
    if schema != dict(columns=list(FEATURE_COLUMNS), categorical_columns=list(CATEGORICAL_COLUMNS), class_order=list(CLASS_ORDER)):
        raise CandidateError('Le schéma du candidat est incompatible.')
    for name in ('development_id', 'plan_id', 'partition_id', 'cohort_id', 'snapshots_sha256'):
        _hash(report['inputs'][name])
    if set(report['inputs']['phases']) != set(PHASES) or set(report['minimums']) != set(PHASES):
        raise CandidateError('Les phases de développement du candidat sont invalides.')
    seen = set()
    for name in PHASES:
        row = report['inputs']['phases'][name]
        minimum = report['minimums'][name]
        if type(minimum) is not int or minimum <= 0 or type(row['count']) is not int or row['count'] < minimum:
            raise CandidateError('Les effectifs du candidat sont insuffisants.')
        _hash(row['sha256'])
        if (len(row['snapshot_ids']) != row['count'] or len(set(row['snapshot_ids'])) != row['count']
                or set(row['labels']) != set(CLASS_ORDER) or sum(row['labels'].values()) != row['count']
                or any(type(value) is not int or value <= 0 for value in row['labels'].values())):
            raise CandidateError('Les classes ou dénominateurs du candidat sont incohérents.')
        for sid in row['snapshot_ids']:
            _hash(sid)
            if sid in seen:
                raise CandidateError('Un instantané du candidat est réutilisé entre phases.')
            seen.add(sid)
    def valid_metrics(value, count):
        if (set(value) != {'count', 'logloss', 'brier', 'accuracy'} or value['count'] != count
                or type(value['count']) is not int):
            raise CandidateError('Les mesures du candidat ont un dénominateur invalide.')
        for key in ('logloss', 'brier', 'accuracy'):
            _number(value[key])
        if value['accuracy'] > 1 or value['brier'] > 2+1e-12:
            raise CandidateError('Une mesure du candidat dépasse sa borne.')
    if (set(report['references']) != {'tuning', 'calibration'}
            or not isinstance(report['reference_clubs_versions'], list) or not report['reference_clubs_versions']
            or any(not isinstance(v, str) or not v for v in report['reference_clubs_versions'])):
        raise CandidateError('Les références appariées du candidat sont incomplètes.')
    for name in ('tuning', 'calibration'):
        if set(report['references'][name]) != {'clubs', 'frequency'}:
            raise CandidateError('Les références Clubs et fréquences doivent être appariées.')
        for value in report['references'][name].values():
            valid_metrics(value, report['inputs']['phases'][name]['count'])
    trials = report['trials']
    if len(trials) != len(recipe()['candidates']):
        raise CandidateError('Les essais de réglage sont incomplets.')
    selected = None
    for row, params in zip(trials, recipe()['candidates']):
        if digest(row['params']) != digest(params):
            raise CandidateError('Les paramètres de réglage ne correspondent pas à la recette.')
        valid_metrics(row['metrics'], report['inputs']['phases']['tuning']['count'])
        if selected is None or row['metrics']['logloss'] < selected['metrics']['logloss']-1e-12:
            selected = row
    if report['selection'] != dict(params=selected['params'], metric='multiclass_logloss', tuning_loss=selected['metrics']['logloss']):
        raise CandidateError('Le réglage retenu ne correspond pas au meilleur essai autorisé.')
    if report['final_training'] != dict(phases=['train', 'tuning'],
             count=sum(report['inputs']['phases'][name]['count'] for name in ('train', 'tuning'))):
        raise CandidateError('Le réentraînement ne peut utiliser que train et tuning.')
    calibration = report['calibration']
    if (calibration['method'] != 'scalar_temperature_grid_v1' or calibration['phase'] != 'calibration'
            or len(calibration['trials']) != len(recipe()['temperatures'])):
        raise CandidateError('La calibration doit conserver sa phase et sa grille séparées.')
    for row, value in zip(calibration['trials'], recipe()['temperatures']):
        if type(row['temperature']) is not float or row['temperature'] != value:
            raise CandidateError('La grille de calibration a été modifiée.')
        valid_metrics(row['metrics'], report['inputs']['phases']['calibration']['count'])
    chosen = _best_temperature(calibration['trials'])
    identity = next(row['metrics'] for row in calibration['trials'] if row['temperature'] == 1)
    valid_metrics(calibration['raw_metrics'], report['inputs']['phases']['calibration']['count'])
    valid_metrics(calibration['calibrated_metrics'], report['inputs']['phases']['calibration']['count'])
    if (type(calibration['temperature']) is not float or calibration['temperature'] != chosen['temperature']
            or calibration['calibrated_metrics'] != chosen['metrics']
            or calibration['raw_metrics'] != identity):
        raise CandidateError('La température retenue ne correspond pas à la calibration figée.')
    if report['implementation'] != implementation():
        raise CandidateError('Le code installé ne correspond plus aux empreintes du candidat ; sa version est incompatible.')
    for value in report['implementation'].values():
        _hash(value)
    if not (utc_instant(report['plan']['created_at']) <= utc_instant(report['started_at'])
            <= utc_instant(report['completed_at']) <= datetime.now(timezone.utc)):
        raise CandidateError('Les heures d’ajustement du candidat sont incohérentes.')


def _model_contract(model, report):
    if (list(model.feature_names_) != report['feature_schema']['columns']
            or list(map(str, model.classes_)) != list(CLASS_ORDER)
            or list(model.get_cat_feature_indices()) != [report['feature_schema']['columns'].index(name)
                                                        for name in report['feature_schema']['categorical_columns']]):
        raise CandidateError('Le modèle binaire ne correspond pas au schéma déclaré.')
    params = model.get_all_params()
    if (params.get('random_seed') != recipe()['seed'] or params.get('loss_function') != 'MultiClass'
            or any(params.get(key) != value for key, value in report['selection']['params'].items())):
        raise CandidateError('Le modèle binaire ne correspond pas aux paramètres déclarés.')


def _write_json(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        stream.flush(); os.fsync(stream.fileno())


def write_candidate(directory, model, report):
    """Save exclusively outside serving directories; COMPLETE is written last."""
    validate_report(report); _model_contract(model, report)
    directory = Path(directory)
    root = Path(__file__).resolve().parent
    if (directory.resolve().is_relative_to(Path(ml.DATA_DIR).resolve())
            or directory.resolve().is_relative_to(root/'data') and not directory.resolve().is_relative_to(root/'data/research')):
        raise CandidateError('Un candidat isolé ne peut pas être enregistré dans les modèles servis.')
    directory.mkdir(parents=True, exist_ok=False)
    model.save_model(str(directory/'candidate.cbm'), format='cbm')
    with (directory/'candidate.cbm').open('rb') as stream:
        os.fsync(stream.fileno())
    _write_json(directory/'report.json', report)
    package = _seal(dict(version='causal-candidate-bundle-v1', state='complete', candidate_id=report['id'],
                         files={name: sha256((directory/name).read_bytes()).hexdigest()
                                for name in ('candidate.cbm', 'report.json')}))
    _write_json(directory/'COMPLETE.json', package)
    return package


def load_candidate(directory):
    directory = Path(directory)
    if (directory/'COMPLETE.json').is_symlink():
        raise CandidateError('Un chemin du candidat est invalide.')
    package = json.loads((directory/'COMPLETE.json').read_text(encoding='utf-8'))
    _check_seal(package)
    if (package.get('version') != 'causal-candidate-bundle-v1' or package.get('state') != 'complete'
            or set(package.get('files', {})) != {'candidate.cbm', 'report.json'}):
        raise CandidateError('Le paquet candidat est incomplet ou incompatible.')
    for name, expected in package['files'].items():
        path = directory/name
        if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
            raise CandidateError('Un chemin du candidat est invalide.')
        if sha256(path.read_bytes()).hexdigest() != _hash(expected):
            raise CandidateError('Un fichier candidat a été altéré.')
    report = json.loads((directory/'report.json').read_text(encoding='utf-8'))
    validate_report(report)
    if package['candidate_id'] != report['id']:
        raise CandidateError('Le paquet ne correspond pas au rapport candidat.')
    model = ml._classifier()()
    model.load_model(str(directory/'candidate.cbm'), format='cbm')
    _model_contract(model, report)
    return dict(model=model, report=report, package=package)


def predict_candidate(candidate, feature_rows):
    """Explicit research inference; never capture or serve a production forecast."""
    report = candidate['report']
    validate_report(report); _model_contract(candidate['model'], report)
    schema = report['feature_schema']
    if isinstance(feature_rows, (str, bytes, dict)):
        raise CandidateError('Une liste de lignes de variables est nécessaire.')
    feature_rows = list(feature_rows)
    if any(not isinstance(row, dict) or set(row) != set(schema['columns']) for row in feature_rows):
        raise CandidateError('Les variables à prédire ne correspondent pas au schéma du candidat.')
    if not feature_rows:
        return []
    raw = ml._predict(candidate['model'], feature_rows, schema['columns'], schema['categorical_columns'])
    return [temperature(p, report['calibration']['temperature']) for p in raw]
