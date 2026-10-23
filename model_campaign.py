"""Freeze one local validation campaign per API-Football competition.

No provider request or journal mutation occurs here. The first eligible input
snapshot, selected parameters and native artifacts are retained per league.
Retries use that same snapshot; completed campaigns never tune on new matches.
Existing compatible exploratory artifacts may be adopted without re-fitting.
"""

import cloud_storage
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

import api_football as api
import catboost_scenarios as ml
import contextual_validation as contextual
from coupon_sizes import validate_size
import scenario_features as features
import seasonal_validation as seasonal


VERSION = 'model-campaign-v1'
CAMPAIGN = 'fixed-validation-v1'
SOURCE = 'API-Football'
DEFAULT_SIZES = (2, 4, 6, 8)


class CampaignError(ValueError):
    """Campaign data or artifacts are inconsistent or currently locked."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _time(now=None):
    if now is None:
        return datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise CampaignError('La campagne doit préciser son heure UTC avec fuseau.')
    return now.astimezone(timezone.utc)


def _sizes(values):
    values = tuple(values)
    if not values or len(set(values)) != len(values):
        raise CampaignError('Choisissez des tailles de groupe distinctes.')
    return tuple(sorted(validate_size(size) for size in values))


def _league(value):
    try:
        return api._numeric_id(value, 'de compétition')
    except ValueError as error:
        raise CampaignError(str(error)) from None


def _paths(league, root=None):
    root = Path(root if root is not None else api.DATA_DIR).resolve()
    base = root/'model_campaigns'/CAMPAIGN/_league(league)
    # A custom root is always isolated from the application's deployed models.
    production = Path(ml.DATA_DIR) if root == Path(api.DATA_DIR).resolve() else root/'models'
    return dict(root=root, base=base, input=base/'input.json', manifest=base/'manifest.json',
                native=base/'models', context=base/'contextual_validation.json',
                production_native=production, production_context=root/f'contextual_validation_{_league(league)}.json')


def _protocol():
    return dict(feature_version=features.FEATURE_VERSION, catboost_version=ml.VERSION,
        contextual_version=contextual.VERSION, neighbors_version=contextual.neighbors.VERSION,
        descriptor_version=contextual.neighbors.DESCRIPTOR_VERSION,
        catboost_candidates=[dict(value) for value in ml.CANDIDATES], catboost_alphas=list(ml.ALPHAS),
        contextual_candidates=[dict(value) for value in contextual.CANDIDATES],
        neighbor_parameters=[dict(value) for value in contextual.PARAMETERS],
        chronological_split=dict(train=.6, validation=.2, whole_utc_days=True),
        seasonal_folds=[dict(train=list(years), validation=validation, test=test)
                        for years, validation, test in seasonal.FOLDS])


@cloud_storage.persisted
def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent,
                                         prefix='.'+path.name+'.', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(_json(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _read(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        raise CampaignError(f'Le fichier de campagne {path.name} est illisible.') from None


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _production_state(paths, league):
    return dict(catboost=_digest(ml._location(SOURCE, league, paths['production_native'])/'CURRENT.json'),
                context_neighbors=_digest(paths['production_context']))


def _guard_publication(path, previous_digest, desired):
    current = _digest(path)
    expected = hashlib.sha256(_json(desired).encode()).hexdigest()
    if current not in (previous_digest, expected):
        raise CampaignError('Un autre entraînement a modifié le modèle déployé pendant la campagne ; publication automatique refusée.')


@contextmanager
def _lock(paths):
    paths['base'].mkdir(parents=True, exist_ok=True)
    with (paths['base']/'campaign.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CampaignError('Une validation de cette compétition est déjà en cours.') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def load_manifest(league, root=None):
    path = _paths(league, root)['manifest']
    if not path.exists():
        return None
    value = _read(path)
    if (not isinstance(value, dict) or value.get('version') != VERSION or value.get('campaign') != CAMPAIGN
            or value.get('source') != SOURCE or value.get('league') != _league(league)
            or value.get('status') not in ('building', 'frozen', 'error')):
        raise CampaignError('Le manifeste de campagne est invalide ou incompatible.')
    return value


@contextmanager
def manual_publication(league, root=None):
    """Serialize legacy publishers with campaign creation and refuse a frozen input.

    The check belongs at publication time: a calculation started before the
    campaign must not replace its artifacts after finishing later.
    """
    with cloud_storage.transaction():
        paths = _paths(league, root)
        with _lock(paths):
            if paths['input'].exists() or paths['manifest'].exists():
                raise CampaignError('Cette compétition possède une campagne figée ou en préparation ; remplacement du modèle refusé.')
            yield
        cloud_storage.checkpoint()


def _history(rows, league, cutoff):
    try:
        return ml._rows(rows, league, SOURCE, cutoff)
    except (ValueError, KeyError, TypeError) as error:
        raise CampaignError(str(error)) from None


def _history_change(history, paths, manifest):
    """Distinguish new results from correction/removal of the frozen input."""
    if not manifest:
        return 'not_frozen'
    frozen = _read(paths['input'])
    if ml._fingerprint(frozen.get('rows', [])) != manifest.get('input_fingerprint'):
        raise CampaignError('L’instantané de campagne ne correspond plus à son manifeste.')
    current = {row['match_id']: row for row in history}
    original = {row['match_id']: row for row in frozen['rows']}
    if any(mid not in current or current[mid] != row for mid,row in original.items()):
        return 'changed_history'
    added = set(current)-set(original)
    if any(current[mid]['timestamp'][:10] < frozen['cutoff'] for mid in added):
        return 'changed_history'
    return 'new_results' if added else 'unchanged'


def _chronological(history):
    try:
        split = features.chronological_split([dict(match=row) for row in history])
        if {row['match']['value'] for row in split['train']} != set(ml.VALUES):
            raise ValueError('Les trois résultats sont nécessaires dans l’apprentissage.')
        blocks = ml._blocks(history)
        if any(not ml._contained(blocks, split[name]) for name in split):
            raise ValueError('Chaque période nécessite un groupe complet de quatre matchs.')
        return dict(ready=True, reason='', split={name: dict(matches=len(values),
            start=values[0]['match']['timestamp'][:10], end=values[-1]['match']['timestamp'][:10])
            for name, values in split.items()})
    except ValueError as error:
        return dict(ready=False, reason=str(error), split={})


def _prepared_seasons(root=None):
    """Wait for configured backfills before freezing their incomplete prefix."""
    provider_root = Path(root if root is not None else api.DATA_DIR)
    config_path = provider_root/'automation_config.json'
    if not config_path.exists():
        return (), set()
    config = _read(config_path)
    seasons = config.get('history_seasons', []) if isinstance(config, dict) else []
    if not isinstance(seasons, list) or any(type(year) is not int for year in seasons):
        raise CampaignError('Les saisons configurées pour la préparation sont invalides.')
    completed = {(row['competition_code'],row['season']) for row in api.load_season_imports(provider_root/'fixtures.json')}
    return tuple(seasons), completed


def _contextual(history, league, sizes):
    try:
        planned = seasonal._plan(history, league, '9999-12-31')
        # The agreed seasonal protocol needs all five actual seasons. A shorter
        # fold is never presented as an invented substitute for this comparison.
        counts = Counter(row.get('season') for row in history)
        if any(counts[year] < seasonal.MIN_SEASON_MATCHES for year in range(2022, 2027)):
            raise ValueError('La comparaison contextuelle nécessite les saisons 2022 à 2026, chacune avec au moins 30 résultats HT/FT.')
        if [fold['test_season'] for fold in planned['folds']] != [2024, 2025, 2026]:
            raise ValueError('Les trois séparations chronologiques par saison ne sont pas disponibles.')
        for train_years, validation_year, test_year in seasonal.FOLDS:
            split = seasonal._fold_rows(history, train_years, validation_year, test_year)
            for selected in split.values():
                for size in set(sizes) | {4}:
                    if not seasonal._groups(history, selected, size):
                        raise ValueError('Une taille demandée ne dispose pas d’un groupe complet dans chaque période saisonnière.')
        return dict(ready=True, reason='')
    except (ValueError, TypeError) as error:
        return dict(ready=False, reason=str(error))


def plan(rows, raw=None, sizes=DEFAULT_SIZES, root=None, now=None):
    """List every known API league, prioritizing upcoming kickoffs; read-only."""
    rows, raw, sizes, current = list(rows), list(raw or []), _sizes(sizes), _time(now)
    cutoff = current.date().isoformat()
    required_seasons, imported = _prepared_seasons(root)
    leagues, names, earliest = set(), {}, {}
    for row in rows:
        if row.get('provider') == 'api-football':
            league = _league(row['league'])
            leagues.add(league)
            names[league] = row.get('competition') or league
    for item in raw:
        try:
            league = _league(item['league']['id'])
            leagues.add(league)
            names[league] = item['league'].get('name') or league
            stamp = datetime.fromisoformat(ml._utc_stamp(item['fixture']['date'])).replace(tzinfo=timezone.utc)
            if api._status_short(item) == 'NS' and stamp > current:
                earliest[league] = min(stamp, earliest.get(league, stamp))
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    output = []
    for league in sorted(leagues, key=int):
        history = _history(rows, league, cutoff)
        native, contexts = _chronological(history), _contextual(history, league, sizes)
        manifest = load_manifest(league, root)
        change = _history_change(history, _paths(league, root), manifest) if manifest else 'not_frozen'
        missing = [year for year in required_seasons if (league,year) not in imported]
        status = 'frozen' if manifest and manifest['status'] == 'frozen' else 'ready' if native['ready'] and not missing else 'insufficient'
        output.append(dict(league=league, competition=names.get(league, league), status=status,
            history_matches=len(history), fingerprint=ml._fingerprint(history),
            upcoming_at=earliest[league].isoformat() if league in earliest else None,
            chronological_ready=native['ready'], contextual_ready=contexts['ready'], missing_seasons=missing,
            chronological=native, contextual=contexts, sizes=list(sizes),
            frozen_at=manifest.get('frozen_at') if manifest else None,
            frozen_fingerprint=manifest.get('input_fingerprint') if manifest else None,
            history_state=change, changed_history=change == 'changed_history', new_results=change == 'new_results',
            context_frozen_unavailable=bool(manifest and manifest.get('components', {}).get('context_neighbors', {}).get('status') == 'unavailable'),
            reason=('Paramètres déjà figés ; aucune nouvelle optimisation.' if status == 'frozen'
                    else native['reason'] if not native['ready'] else
                    'Saisons à importer avant de figer le modèle : '+', '.join(map(str,missing)) if missing else contexts['reason'])))
    return sorted(output, key=lambda r: (r['upcoming_at'] is None, r['upcoming_at'] or '', int(r['league'])))


def _native_report(directory, league, history, require_current=True):
    report = ml.load_report(SOURCE, league, directory)
    if report is None:
        return None
    if (report['feature_version'] != features.FEATURE_VERSION
            or report['feature_columns'] != list(features.FEATURE_COLUMNS)
            or report['categorical_columns'] != list(features.CATEGORICAL_COLUMNS)):
        raise CampaignError('Les caractéristiques du modèle CatBoost sont incompatibles.')
    generation = report['deployment']['generation']
    model = ml._location(SOURCE, league, directory)/generation/'model.cbm'
    if not model.exists() or hashlib.sha256(model.read_bytes()).hexdigest() != report['deployment']['model_sha256']:
        raise CampaignError('Le modèle natif de la campagne est absent ou invalide.')
    if require_current:
        before = report['deployment']['cutoff_exclusive']
        fitted_rows = _history(history, league, before)
        if ml._fingerprint(fitted_rows) != report['deployment']['fingerprint']:
            raise CampaignError('Le modèle existant ne correspond plus à son historique initial.')
    return report


def _context_report(path, league, history, sizes):
    if not path.exists():
        return None
    report = contextual._validate_report(_read(path), league)
    deployment = report['deployment']
    if (deployment['context_version'] != contextual.neighbors.VERSION
            or deployment['feature_version'] != features.FEATURE_VERSION
            or not set(map(str, sizes)).issubset(deployment['parameters'])):
        raise CampaignError('Les paramètres contextuels existants sont incompatibles.')
    selected = [row for row in history if row['timestamp'][:10] <= deployment['selection_through']]
    if ml._fingerprint(selected) != deployment['selection_fingerprint']:
        raise CampaignError('L’historique ayant sélectionné les paramètres contextuels a changé.')
    return report


@cloud_storage.persisted
def _copy_native(report, source_directory, target_directory, league):
    """Copy a verified immutable generation, then atomically switch its pointer."""
    source = ml._location(SOURCE, league, source_directory)
    target = ml._location(SOURCE, league, target_directory)
    generation = report['deployment']['generation']
    target.mkdir(parents=True, exist_ok=True)
    destination = target/generation
    if destination.exists():
        for name in ('model.cbm', 'report.json'):
            if (destination/name).read_bytes() != (source/generation/name).read_bytes():
                raise CampaignError('Une génération native existante diffère de l’artefact figé.')
    else:
        temporary = Path(tempfile.mkdtemp(prefix='.campaign-', dir=target))
        try:
            for name in ('model.cbm', 'report.json'):
                shutil.copyfile(source/generation/name, temporary/name)
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    # Bind to the already verified immutable generation, even if a separate
    # manual training job moves the source CURRENT pointer during this copy.
    raw_report = (source/generation/'report.json').read_bytes()
    if _json(json.loads(raw_report)) != _json(report):
        raise CampaignError('Le rapport natif a changé pendant sa copie.')
    pointer = dict(generation=generation, report_sha256=hashlib.sha256(raw_report).hexdigest())
    _write(target/'CURRENT.json', pointer)


def _input(rows, league, sizes, paths, current):
    if paths['input'].exists():
        frozen = _read(paths['input'])
        if (not isinstance(frozen, dict) or frozen.get('version') != VERSION or frozen.get('source') != SOURCE
                or frozen.get('league') != league or frozen.get('sizes') != list(sizes)
                or ml._fingerprint(frozen.get('rows', [])) != frozen.get('fingerprint')):
            raise CampaignError('Le premier instantané de campagne est invalide ou ses tailles diffèrent.')
        if frozen.get('protocol') != _protocol():
            raise CampaignError('Le protocole ou les versions ont changé ; la première campagne ne peut pas être réoptimisée silencieusement.')
        return frozen
    cutoff = current.date().isoformat()
    history = _history(rows, league, cutoff)
    frozen = dict(version=VERSION, source=SOURCE, league=league, sizes=list(sizes),
        frozen_at=current.isoformat(timespec='microseconds'), cutoff=cutoff, rows=history,
        fingerprint=ml._fingerprint(history), protocol=_protocol())
    _write(paths['input'], frozen)
    return frozen


def _comparisons(native, contextual_report, sizes, context_reason):
    if contextual_report is not None:
        return dict(status='available', kind='seasonal_exploratory', methods=list(contextual.METHODS),
            evaluated_fingerprint=contextual_report.get('fingerprint'),
            evaluated_cutoff=contextual_report.get('cutoff'), evaluated_at=contextual_report.get('generated_at'),
            sizes={str(size): dict(status='available', aggregate=contextual_report['aggregate'][str(size)],
                aggregate_complete=contextual_report['aggregate_complete'][str(size)],
                aggregate_partial=contextual_report['aggregate_partial'][str(size)]) for size in sizes},
            report='contextual_validation.json',
            interpretation='Five benchmarks share each seasonal target cohort. The deployed CatBoost artifact has its own chronological four-match report; seasonal CatBoost benchmark scores are not its prospective performance.')
    output = {str(size): dict(status='unavailable', reason=context_reason,
                             methods_missing=['context_neighbors']) for size in sizes}
    if 4 in sizes and native:
        output['4'] = dict(status='partial', methods=list(native['test']),
                          test=native['test'], targets=native['targets']['test'],
                          reason='Comparaison chronologique de quatre méthodes sur quatre matchs ; contextes indisponibles. '+context_reason)
    return dict(status='partial', kind='chronological_four_only', sizes=output,
                reason=context_reason, report='models/api-football/'+(native['league'] if native else '')+'/CURRENT.json')


@cloud_storage.persisted
def run_one(rows, league, *, sizes=DEFAULT_SIZES, root=None, now=None):
    """Freeze, validate and publish one league once; no network or journal writes.

    ``root`` is the provider data directory. With a custom root the deployment
    model directory is ``root/models``; the real provider root uses ml.DATA_DIR.
    Insufficient histories are returned without freezing an input. After a
    frozen campaign completes, later calls return its manifest unchanged.
    """
    rows, league, sizes, current = list(rows), _league(league), _sizes(sizes), _time(now)
    paths = _paths(league, root)
    with _lock(paths):
        previous = load_manifest(league, root)
        if previous and previous['status'] == 'frozen':
            return previous
        if not paths['input'].exists():
            history = _history(rows, league, current.date().isoformat())
            readiness = _chronological(history)
            if not readiness['ready']:
                return dict(version=VERSION, campaign=CAMPAIGN, source=SOURCE, league=league,
                    status='insufficient', history_matches=len(history), reason=readiness['reason'], sizes=list(sizes))
            required_seasons, imported = _prepared_seasons(root)
            missing = [year for year in required_seasons if (league,year) not in imported]
            if missing:
                return dict(version=VERSION, campaign=CAMPAIGN, source=SOURCE, league=league,
                    status='insufficient', history_matches=len(history), missing_seasons=missing, sizes=list(sizes),
                    reason='Saisons à importer avant de figer le modèle : '+', '.join(map(str,missing)))
        frozen = _input(rows, league, sizes, paths, current)
        history = frozen['rows']
        manifest = previous or dict(version=VERSION, campaign=CAMPAIGN, source=SOURCE, league=league,
            sizes=list(sizes), frozen_at=frozen['frozen_at'], cutoff=frozen['cutoff'],
            input_fingerprint=frozen['fingerprint'], history_matches=len(history),
            protocol=frozen['protocol'],
            input_file='input.json', status='building', components={}, publications={},
            production_at_start=_production_state(paths, league),
            interpretation='Retrospective/exploratory data already available. This is not a new independent test.',
            policy='First eligible input and parameters frozen per league. Later matches never trigger retuning. Journal forecasts remain unchanged.')
        manifest['status'] = 'building'
        _write(paths['manifest'], manifest)
        try:
            native = _native_report(paths['native'], league, history)
            if native is None:
                try:
                    existing = _native_report(paths['production_native'], league, history)
                except (ValueError, OSError):
                    existing = None
                if existing:
                    _copy_native(existing, paths['production_native'], paths['native'], league)
                    native, origin = existing, 'adopted_existing_exploratory'
                else:
                    native = ml.train(history, league, SOURCE, directory=paths['native'], before=frozen['cutoff'])
                    origin = 'trained_new_on_frozen_input'
                _native_report(paths['native'], league, history)
                manifest['components']['catboost'] = dict(status='available', origin=origin,
                    generation=native['deployment']['generation'], parameters=native['selection'],
                    validated_size=4, report_version=native['version'])
                _write(paths['manifest'], manifest)
            elif 'catboost' not in manifest['components']:
                manifest['components']['catboost'] = dict(status='available', origin='recovered_frozen_artifact',
                    generation=native['deployment']['generation'], parameters=native['selection'],
                    validated_size=4, report_version=native['version'])
            context_reason = ''
            contextual_report = _context_report(paths['context'], league, history, sizes)
            if contextual_report is None and manifest['components'].get('context_neighbors', {}).get('status') != 'unavailable':
                try:
                    existing_context = _context_report(paths['production_context'], league, history, sizes)
                except (ValueError, OSError):
                    existing_context = None
                if existing_context is not None:
                    contextual_report, origin = existing_context, 'adopted_existing_exploratory'
                else:
                    readiness = _contextual(history, league, sizes)
                    if readiness['ready']:
                        contextual_report = contextual.run(history, league, SOURCE, sizes=sizes)
                        contextual._validate_report(contextual_report, league)
                        origin = 'validated_new_on_frozen_input'
                    else:
                        context_reason = readiness['reason']
                        manifest['components']['context_neighbors'] = dict(status='unavailable', reason=context_reason)
                if contextual_report is not None:
                    _write(paths['context'], contextual_report)
                    manifest['components']['context_neighbors'] = dict(status='available', origin=origin,
                        generation=contextual_report['deployment']['generation'],
                        parameters=contextual_report['deployment']['parameters'],
                        selection_through=contextual_report['deployment']['selection_through'],
                        selection_fingerprint=contextual_report['deployment']['selection_fingerprint'],
                        report_version=contextual_report['version'])
                _write(paths['manifest'], manifest)
            elif contextual_report is None:
                context_reason = manifest['components']['context_neighbors']['reason']
            elif 'context_neighbors' not in manifest['components']:
                manifest['components']['context_neighbors'] = dict(status='available', origin='recovered_frozen_artifact',
                    generation=contextual_report['deployment']['generation'],
                    parameters=contextual_report['deployment']['parameters'],
                    selection_through=contextual_report['deployment']['selection_through'],
                    selection_fingerprint=contextual_report['deployment']['selection_fingerprint'],
                    report_version=contextual_report['version'])
            manifest['comparisons'] = _comparisons(native, contextual_report, sizes, context_reason)
            # Publish the exact frozen artifacts once, after validation. A crash
            # can replay the same bytes, never choose a new generation.
            if 'catboost' not in manifest['publications']:
                native_base=ml._location(SOURCE, league, paths['native'])
                native_raw=(native_base/native['deployment']['generation']/'report.json').read_bytes()
                desired_pointer=dict(generation=native['deployment']['generation'],
                                     report_sha256=hashlib.sha256(native_raw).hexdigest())
                _guard_publication(ml._location(SOURCE,league,paths['production_native'])/'CURRENT.json',
                                   manifest['production_at_start']['catboost'],desired_pointer)
                _copy_native(native, paths['native'], paths['production_native'], league)
                manifest['publications']['catboost'] = dict(generation=native['deployment']['generation'],
                    published_at=current.isoformat(), directory=str(paths['production_native']))
                _write(paths['manifest'], manifest)
            if contextual_report is not None and 'context_neighbors' not in manifest['publications']:
                _guard_publication(paths['production_context'],
                                   manifest['production_at_start']['context_neighbors'], contextual_report)
                _write(paths['production_context'], contextual_report)
                manifest['publications']['context_neighbors'] = dict(generation=contextual_report['deployment']['generation'],
                    published_at=current.isoformat(), path=str(paths['production_context']))
                _write(paths['manifest'], manifest)
            manifest.update(status='frozen', completed_at=current.isoformat())
            _write(paths['manifest'], manifest)
            return manifest
        except cloud_storage.CloudStorageError:
            raise
        except Exception as error:
            manifest.update(status='error', error='Validation ou publication interrompue ; reprise uniquement sur le premier instantané figé.')
            _write(paths['manifest'], manifest)
            if isinstance(error, (ValueError, OSError)):
                raise CampaignError(str(error)) from None
            raise CampaignError('La campagne de cette compétition n’a pas pu être terminée.') from None
