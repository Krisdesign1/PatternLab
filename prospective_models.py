"""Freeze full scenario distributions before matches for later, local scoring.

Only API-Football histories are accepted. The JSON snapshots contain enough
information to score any eventual ordered outcome without a model, a database,
or later match data. Positive probabilities remain normalized over all 3**N
scenarios; the displayed top three are never rescaled to sum to one.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from math import fsum, isclose, isfinite, prod

import catboost_scenarios as ml
import contextual_scenarios as contextual
from coupon_sizes import top_independent, validate_size
from league_model import VALUES
from league_patterns import repeated_scenarios
from match_predictor import VERSION as CLUB_VERSION, estimate as estimate_clubs
from scenario_predictor import PRIOR_WEIGHT, fit_sequences


VERSION = 'prospective-methods-v1'
DISTRIBUTION_VERSION = 'prospective-distribution-v1'
METHODS = ('frequency', 'clubs', 'combined', 'catboost', 'context_neighbors')
SOURCE = 'API-Football'
PROVIDER = 'api-football'
MIXED_VERSION = 'prospective-mixed-blocks-v1'


class SnapshotError(ValueError):
    """A frozen distribution or the selected ordered group is inconsistent."""


def _number(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise SnapshotError('Une probabilité ou un paramètre est absent ou non fini.')
    if positive and value <= 0:
        raise SnapshotError('Les probabilités doivent être strictement positives.')
    return float(value)


def _sequence(value, size):
    if not isinstance(value, (list, tuple)) or len(value) != size or any(label not in VALUES for label in value):
        raise SnapshotError('Le scénario doit conserver les résultats et l’ordre des matchs.')
    return tuple(value)


def _positions(values, size):
    if not isinstance(values, list) or len(values) != size:
        raise SnapshotError('Le nombre de distributions doit correspondre au groupe.')
    result = []
    for distribution in values:
        if not isinstance(distribution, dict) or set(distribution) != set(VALUES):
            raise SnapshotError('Les trois résultats doivent être présents à chaque position.')
        item = {label: _number(distribution[label], positive=True) for label in VALUES}
        if any(value > 1 for value in item.values()) or not isclose(fsum(item.values()), 1, rel_tol=1e-10, abs_tol=1e-12):
            raise SnapshotError('Chaque distribution par match doit totaliser un.')
        result.append(item)
    return result


def _read_fit(value, size):
    if not isinstance(value, dict) or type(value.get('n')) is not int or value['n'] < 0:
        raise SnapshotError('Effectif historique invalide.')
    rows = value.get('counts')
    if not isinstance(rows, list):
        raise SnapshotError('Les effectifs des scénarios doivent être conservés.')
    counts = {}
    for row in rows:
        if not isinstance(row, dict):
            raise SnapshotError('Effectif de scénario invalide.')
        sequence = _sequence(row.get('sequence'), size)
        count = row.get('count')
        if type(count) is not int or count <= 0 or sequence in counts:
            raise SnapshotError('Chaque scénario observé doit avoir un effectif positif unique.')
        counts[sequence] = count
    n = value['n']
    if sum(counts.values()) != n:
        raise SnapshotError('Les effectifs doivent correspondre au nombre de groupes.')
    prior = _positions(value.get('prior'), size)
    for position in range(size):
        for label in VALUES:
            expected = (sum(count for sequence, count in counts.items() if sequence[position] == label) + 1) / (n + 3)
            if not isclose(prior[position][label], expected, rel_tol=1e-10, abs_tol=1e-12):
                raise SnapshotError('Les fréquences par position contredisent les effectifs historiques.')
    return counts, n, prior


def _base_probability(fit, sequence):
    return prod(distribution[label] for distribution, label in zip(fit[2], sequence))


def _frequency_probability(fit, sequence, weight):
    return (fit[0].get(sequence, 0) + weight * _base_probability(fit, sequence)) / (fit[1] + weight)


def _normalization(fit, distributions, weight, alpha):
    unseen = (weight / (fit[1] + weight)) ** alpha
    corrections = {}
    for sequence in fit[0]:
        b = _base_probability(fit, sequence)
        if b <= 0:
            raise SnapshotError('Une probabilité historique est trop petite pour être représentée.')
        corrections[sequence] = (_frequency_probability(fit, sequence, weight) / b) ** alpha
    z = unseen + fsum(prod(p[label] for p, label in zip(distributions, sequence)) * (factor-unseen)
                       for sequence, factor in corrections.items())
    if not isfinite(z) or z <= 0 or not isfinite(unseen) or unseen <= 0:
        raise SnapshotError('La normalisation de la distribution est invalide.')
    return unseen, corrections, z


def _read_distribution(distribution, *, _component=False):
    if not isinstance(distribution, dict) or distribution.get('schema') != DISTRIBUTION_VERSION:
        raise SnapshotError('Version de distribution figée inconnue.')
    try:
        size = distribution.get('size')
        if not (_component and type(size) is int and size == 1):
            size = validate_size(size)
    except ValueError as error:
        raise SnapshotError(str(error)) from None
    kind = distribution.get('type')
    if kind not in ('frequency', 'independent', 'corrected', 'context_mixture', 'block_product'):
        raise SnapshotError('Type de distribution figée inconnu.')
    if size == 1 and kind != 'independent':
        raise SnapshotError('Un composant d’un seul match conserve une distribution indépendante explicite.')
    model = dict(size=size, type=kind)
    if kind == 'block_product':
        if _component:
            raise SnapshotError('Les produits de blocs ne peuvent pas être imbriqués.')
        components = distribution.get('components')
        if not isinstance(components, list) or not 1 <= len(components) <= size:
            raise SnapshotError('Le groupe mixte doit conserver ses composants par compétition.')
        occupied, blocks = set(), []
        for component in components:
            if not isinstance(component, dict):
                raise SnapshotError('Composant de groupe mixte invalide.')
            indices = component.get('indices')
            if (not isinstance(indices, list) or not indices
                    or any(type(index) is not int or not 0 <= index < size for index in indices)
                    or indices != sorted(set(indices))
                    or occupied.intersection(indices)):
                raise SnapshotError('Les positions des composants doivent partitionner le groupe sans doublon.')
            payload = component.get('method')
            _validate_method(payload, len(indices), _component=True)
            blocks.append(dict(indices=indices, model=_read_distribution(payload['distribution'], _component=True)))
            occupied.update(indices)
        if occupied != set(range(size)):
            raise SnapshotError('Toutes les positions du groupe mixte doivent être couvertes.')
        model['blocks'] = blocks
        return model
    if kind == 'context_mixture':
        # Both components have total mass one, so this validates the full
        # 3**N distribution analytically without enumerating its outcomes.
        model['distributions'] = _positions(distribution.get('baseline'), size)
        eta = _number(distribution.get('eta'))
        if not 0 <= eta <= .5:
            raise SnapshotError('Le poids du contexte doit être compris entre zéro et un demi.')
        rows = distribution.get('local_mass')
        if not isinstance(rows, list):
            raise SnapshotError('Les masses des scénarios voisins doivent être conservées.')
        local = {}
        for row in rows:
            if not isinstance(row, dict):
                raise SnapshotError('Masse de scénario voisin invalide.')
            sequence = _sequence(row.get('sequence'), size)
            mass = _number(row.get('probability'), positive=True)
            if sequence in local or mass > 1:
                raise SnapshotError('Chaque scénario voisin doit avoir une masse positive unique.')
            if 'observed' in row and (type(row['observed']) is not int or row['observed'] <= 0):
                raise SnapshotError('L’effectif d’un scénario voisin doit être positif.')
            local[sequence] = mass
        if (local and not isclose(fsum(local.values()), 1, rel_tol=1e-10, abs_tol=1e-12)) or (eta > 0 and not local):
            raise SnapshotError('Les masses locales doivent totaliser un avant mélange.')
        model.update(eta=eta, local=local)
        return model
    if kind != 'independent':
        model['fit'] = _read_fit(distribution.get('fit'), size)
        model['weight'] = _number(distribution.get('prior_weight'), positive=True)
    if kind != 'frequency':
        model['distributions'] = _positions(distribution.get('distributions'), size)
    if kind == 'corrected':
        alpha = _number(distribution.get('alpha'))
        if not 0 <= alpha <= 1:
            raise SnapshotError('La force de correction doit être comprise entre zéro et un.')
        unseen, corrections, z = _normalization(model['fit'], model['distributions'], model['weight'], alpha)
        saved_z = _number(distribution.get('normalization'), positive=True)
        if not isclose(saved_z, z, rel_tol=1e-9, abs_tol=1e-12):
            raise SnapshotError('La normalisation figée ne correspond pas à la distribution.')
        model.update(alpha=alpha, unseen=unseen, corrections=corrections, normalization=saved_z)
    return model


def _probability(model, sequence):
    if model['type'] == 'block_product':
        value = prod(_probability(block['model'], tuple(sequence[index] for index in block['indices']))
                     for block in model['blocks'])
    elif model['type'] == 'frequency':
        value = _frequency_probability(model['fit'], sequence, model['weight'])
    else:
        value = prod(p[label] for p, label in zip(model['distributions'], sequence))
        if model['type'] == 'context_mixture':
            value = (1-model['eta'])*value + model['eta']*model['local'].get(sequence, 0)
        if model['type'] == 'corrected':
            value *= model['corrections'].get(sequence, model['unseen']) / model['normalization']
    if not isfinite(value) or not 0 < value <= 1 + 1e-12:
        raise SnapshotError('La probabilité du scénario est nulle ou invalide.')
    return value


def probability(distribution, sequence):
    """Score any outcome from a JSON snapshot alone, without reading later data."""
    model = _read_distribution(distribution)
    return _probability(model, _sequence(sequence, model['size']))


def probability_for_sequence(method_payload, sequence):
    """Journal reconciliation entry point: evaluate the saved full distribution."""
    if not isinstance(method_payload, dict):
        raise SnapshotError('Méthode figée absente ou invalide.')
    return probability(method_payload.get('distribution'), sequence)


def _rank(model, limit=3):
    if model['type'] == 'block_product':
        candidates = [(tuple([None]*model['size']), 1.0)]
        for block in model['blocks']:
            combined = []
            for sequence, chance in candidates:
                for choice in _rank(block['model'], limit):
                    result = list(sequence)
                    for index, label in zip(block['indices'], choice['sequence']):
                        result[index] = label
                    combined.append((tuple(result), chance*choice['probability']))
            candidates = sorted(combined, key=lambda item:(-item[1], item[0]))[:limit]
        return [dict(sequence=list(sequence), probability=chance) for sequence,chance in candidates]
    if model['size'] == 1:
        return [dict(sequence=[label], probability=chance) for label,chance in sorted(
            model['distributions'][0].items(), key=lambda item:(-item[1], item[0]))[:limit]]
    positions = model['fit'][2] if model['type'] == 'frequency' else model['distributions']
    candidates = {row['sequence'] for row in top_independent(positions, limit)}
    if model['type'] == 'context_mixture':
        candidates.update(model['local'])
    elif model['type'] != 'independent':
        candidates.update(model['fit'][0])
    return [{'sequence': list(sequence), 'probability': _probability(model, sequence)}
            for sequence in sorted(candidates, key=lambda seq: (-_probability(model, seq), seq))[:limit]]


def validate_method(method_payload, size):
    """Reject damaged/non-normalized snapshots and rankings that disagree with them."""
    validate_size(size)
    return _validate_method(method_payload, size)


def _validate_method(method_payload, size, *, _component=False):
    if not isinstance(method_payload, dict) or not isinstance(method_payload.get('version'), str) or not method_payload['version']:
        raise SnapshotError('La version de la méthode doit être conservée.')
    model = _read_distribution(method_payload.get('distribution'), _component=_component)
    if model['size'] != size:
        raise SnapshotError('La méthode figée doit correspondre au nombre de matchs.')
    ranking = method_payload.get('ranking')
    if not isinstance(ranking, list) or len(ranking) != 3:
        raise SnapshotError('Les trois premiers scénarios doivent être conservés.')
    expected = _rank(model)
    for saved, actual in zip(ranking, expected):
        if not isinstance(saved, dict) or _sequence(saved.get('sequence'), size) != tuple(actual['sequence']):
            raise SnapshotError('Le classement figé ne correspond pas à la distribution complète.')
        if not isclose(_number(saved.get('probability'), positive=True), actual['probability'], rel_tol=1e-9, abs_tol=1e-12):
            raise SnapshotError('La probabilité affichée ne correspond pas à la distribution figée.')


def _json_fit(fit):
    return dict(n=fit[1], counts=[dict(sequence=list(sequence), count=count)
                                for sequence, count in sorted(fit[0].items())],
                prior=[{label: p[label] for label in VALUES} for p in fit[2]])


def _distribution(kind, size, *, fit=None, distributions=None, alpha=None, normalization=None):
    value = dict(schema=DISTRIBUTION_VERSION, type=kind, size=size)
    if fit is not None:
        value.update(fit=_json_fit(fit), prior_weight=PRIOR_WEIGHT)
    if distributions is not None:
        value['distributions'] = [{label: p[label] for label in VALUES} for p in distributions]
    if kind == 'corrected':
        value.update(alpha=alpha, normalization=normalization)
    return value


def _method(version, distribution, provenance, *, _component=False, **extra):
    model = _read_distribution(distribution, _component=_component)
    payload = dict(version=version, ranking=_rank(model), distribution=distribution, **provenance, **extra)
    _validate_method(payload, distribution['size'], _component=_component)
    # Copy into ordinary JSON types, also rejecting non-finite provenance fields.
    return json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))


def _fixtures(fixtures, source, *, allow_mixed=False):
    if source not in (SOURCE, PROVIDER):
        raise SnapshotError('Seule la source API-Football est autorisée.')
    fixtures = list(fixtures)
    validate_size(len(fixtures))
    normalized, leagues, ids, identities = [], set(), set(), set()
    for fixture in fixtures:
        try:
            row = dict(fixture)
            if row.get('provider', PROVIDER) != PROVIDER:
                raise SnapshotError('Chaque affiche doit provenir d’API-Football.')
            row.update(provider=PROVIDER, league=str(row['league']),
                       timestamp=ml._utc_stamp(row['timestamp']), match_id=str(int(row['match_id'])))
            if row['league'] in ('None', '') or int(row['match_id']) <= 0:
                raise ValueError('Invalid identity')
            home, away = row['home'], row['away']
            if not all(isinstance(name, str) and name.strip() for name in (home, away)) or home == away:
                raise ValueError('Invalid clubs')
            identity = (home, away, row['timestamp'])
            if row['match_id'] in ids or identity in identities:
                raise SnapshotError('Chaque affiche doit apparaître une seule fois dans le groupe.')
            ids.add(row['match_id'])
            identities.add(identity)
            leagues.add(row['league'])
            # Outcomes are deliberately absent from this score-free projection.
            normalized.append({key: row[key] for key in ('match_id', 'league', 'provider', 'home', 'away',
                               'timestamp', 'home_id', 'away_id', 'season') if key in row})
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            if isinstance(error, SnapshotError):
                raise
            raise SnapshotError('Affiche absente, contradictoire ou invalide.') from None
    if len(leagues) != 1 and not allow_mixed:
        raise SnapshotError('Un groupe doit conserver une seule compétition.')
    return normalized, next(iter(leagues)) if len(leagues) == 1 else 'mixed', min(row['timestamp'][:10] for row in normalized)


def _catboost(rows, fixtures, source, league, before, selected_estimate, engine):
    report = ml.load_report(source, league)
    if report is None:
        raise ml.ModelError('Aucun modèle CatBoost enregistré pour cette compétition.')
    deployment = report['deployment']
    if before <= deployment['trained_through']:
        raise ml.ModelError('Le modèle CatBoost a déjà appris sur cette période.')
    fit_rows = ml._rows(rows, league, source, deployment['cutoff_exclusive'])
    if ml._fingerprint(fit_rows) != deployment['fingerprint']:
        raise ml.ModelError('L’historique appris par CatBoost a changé ; contrôlez les données et la campagne figée.')
    if engine == 'catboost' and selected_estimate is not None:
        prediction = selected_estimate
        module = ml._features()
        if (report['feature_version'] != module.FEATURE_VERSION
                or report['feature_columns'] != list(module.FEATURE_COLUMNS)
                or report['categorical_columns'] != list(module.CATEGORICAL_COLUMNS)):
            raise ml.ModelError('Les caractéristiques CatBoost ont changé ; ce modèle exige un contrôle de compatibilité.')
        path = ml._location(source, league, None) / deployment['generation'] / 'model.cbm'
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != deployment['model_sha256']:
            raise ml.ModelError('Le fichier du modèle CatBoost est absent ou invalide.')
    else:
        prediction = ml.forecast(rows, fixtures, source)
    if (not isinstance(prediction, dict) or not prediction.get('eligible') or prediction.get('version') != ml.VERSION
            or prediction.get('generation') != deployment['generation'] or prediction.get('before') != before
            or str(prediction.get('league')) != league or prediction.get('size') != len(fixtures)
            or prediction.get('source') != SOURCE or prediction.get('alpha') != report['selection']['alpha']):
        raise ml.ModelError('La prévision CatBoost ne correspond pas au modèle et au groupe sélectionnés.')
    estimates = prediction.get('club_estimates')
    if not isinstance(estimates, list) or len(estimates) != len(fixtures):
        raise ml.ModelError('Les probabilités CatBoost par affiche sont absentes.')
    distributions = _positions([item.get('probabilities') for item in estimates], len(fixtures))
    blocks = ml._blocks(fit_rows, len(fixtures))
    fit = fit_sequences([block['sequence'] for block in blocks], len(fixtures))
    distribution = _distribution('corrected', len(fixtures), fit=fit, distributions=distributions,
                                 alpha=prediction['alpha'], normalization=prediction['normalization'])
    provenance = dict(source=SOURCE, league=league, training_before=before,
                      training_groups=len(blocks), training_matches=len(fit_rows),
                      history_fingerprint=ml._fingerprint(fit_rows),
                      trained_through=deployment['trained_through'], artifact_cutoff=deployment['cutoff_exclusive'],
                      feature_version=report['feature_version'], model_sha256=deployment['model_sha256'])
    payload = _method(ml.VERSION, distribution, provenance,
                      generation=deployment['generation'], alpha=prediction['alpha'])
    for saved, frozen in zip(prediction.get('ranking', []), payload['ranking']):
        if tuple(saved['sequence']) != tuple(frozen['sequence']) or not isclose(saved['probability'], frozen['probability'], rel_tol=1e-9, abs_tol=1e-12):
            raise ml.ModelError('Le classement CatBoost ne correspond pas à ses probabilités figées.')
    if len(prediction.get('ranking', [])) != 3:
        raise ml.ModelError('Le classement CatBoost doit conserver trois scénarios.')
    return payload


def _context_neighbors(rows, fixtures, source, league, before, selected_estimate=None, engine=None):
    # Loading a configuration and computing a forecast is permitted here only;
    # saved distributions are scored later by the pure JSON functions above.
    import context_neighbors as neighbors
    import contextual_validation as validation
    prediction = (selected_estimate if engine == 'context_neighbors' and selected_estimate is not None
                  else neighbors.forecast(rows, fixtures, source=source, limit=3))
    if not isinstance(prediction, dict) or not prediction.get('eligible'):
        reason = prediction.get('diagnostic') if isinstance(prediction, dict) else None
        raise SnapshotError(reason or 'La méthode par contexte est indisponible pour ce groupe.')
    if (prediction.get('version') != neighbors.VERSION or prediction.get('source') != SOURCE
            or prediction.get('provider') != PROVIDER
            or str(prediction.get('league')) != league or prediction.get('size') != len(fixtures)
            or prediction.get('before') != before or not isinstance(prediction.get('generation'), str)
            or not prediction['generation']):
        raise SnapshotError('La prévision par contexte ne correspond pas au groupe sélectionné.')
    selected_through = prediction.get('selection_through')
    try:
        selection_day = datetime.strptime(selected_through, '%Y-%m-%d').date().isoformat()
    except (TypeError, ValueError):
        raise SnapshotError('La date de sélection de la configuration par contexte est absente ou invalide.') from None
    if selection_day != selected_through or selected_through >= before:
        raise SnapshotError('La configuration par contexte a déjà utilisé cette période.')
    report = validation.load_report(league)
    if not report:
        raise SnapshotError('La configuration par contexte sélectionnée n’est plus disponible.')
    config = report['deployment']
    if (report.get('source') != SOURCE or str(report.get('league')) != league
            or config.get('source') != SOURCE or str(config.get('league')) != league
            or config.get('context_version') != neighbors.VERSION
            or config.get('feature_version') != ml._features().FEATURE_VERSION):
        raise SnapshotError('La version des profils ou des réglages par contexte a changé ; contrôlez leur compatibilité avec la campagne figée.')
    history = ml._rows(rows, league, source, before)
    selected_history = ml._rows(rows, league, source,
                               (datetime.fromisoformat(selection_day).date()+timedelta(days=1)).isoformat())
    expected_parameters = neighbors._params(config['parameters'].get(str(len(fixtures)), neighbors.DEFAULT_PARAMETERS))
    if (prediction['generation'] != config['generation'] or selected_through != config['selection_through']
            or prediction.get('selection_fingerprint') != config['selection_fingerprint']
            or prediction.get('selection_fingerprint') != ml._fingerprint(selected_history)
            or prediction.get('history_fingerprint') != ml._fingerprint(history)
            or prediction.get('parameters') != expected_parameters
            or prediction.get('fixture_fingerprint') != neighbors.fixture_fingerprint(fixtures)):
        raise SnapshotError('La configuration, l’historique ou les affiches ont changé depuis la prévision : relancez le classement.')
    original = prediction.get('distribution')
    if (not isinstance(original, dict) or original.get('schema') != neighbors.SCHEMA
            or original.get('source') != SOURCE or original.get('provider') != PROVIDER
            or str(original.get('league')) != league or original.get('before') != before
            or original.get('size') != len(fixtures)):
        raise SnapshotError('La distribution par contexte ne correspond pas à sa provenance.')
    distribution = dict(schema=DISTRIBUTION_VERSION, type='context_mixture', size=len(fixtures),
                        baseline=original.get('baseline'), local_mass=original.get('local_mass'),
                        eta=original.get('eta'))
    provenance = dict(source=SOURCE, league=league, training_before=before,
                      training_groups=prediction.get('training_groups'), training_matches=len(history),
                      history_fingerprint=ml._fingerprint(history), selection_through=selected_through,
                      context_history_fingerprint=prediction.get('history_fingerprint'),
                      selection_fingerprint=prediction.get('selection_fingerprint'),
                      fixture_fingerprint=prediction['fixture_fingerprint'])
    metadata = {key: prediction.get(key, original.get(key)) for key in
                ('support', 'parameters', 'neighbors', 'diagnostics', 'descriptor_version', 'validated_size')}
    payload = _method(neighbors.VERSION, distribution, provenance,
                      generation=prediction['generation'], **metadata)
    ranking = prediction.get('ranking')
    if not isinstance(ranking, list) or len(ranking) != 3:
        raise SnapshotError('Le classement par contexte doit conserver trois scénarios.')
    for saved, frozen in zip(ranking, payload['ranking']):
        if (not isinstance(saved, dict) or _sequence(saved.get('sequence'), len(fixtures)) != tuple(frozen['sequence'])
                or not isclose(_number(saved.get('probability'), positive=True), frozen['probability'], rel_tol=1e-9, abs_tol=1e-12)):
            raise SnapshotError('Le classement par contexte ne correspond pas à la distribution figée.')
    return payload


def build_methods(rows, fixtures, source=SOURCE, estimate=None, engine=None):
    """Freeze comparable methods for one ordered group; never train automatically.

    ``estimate`` may reuse the selected CatBoost forecast when ``engine`` is
    ``catboost``. Otherwise one CatBoost inference is attempted if a saved model
    exists. A saved context configuration is also attempted independently of the
    selected engine. Unavailable methods carry explicit reasons and never impersonate a
    different algorithm. The journal owns the future-kickoff/write-once checks.
    """
    fixtures, league, before = _fixtures(fixtures, source)
    source = SOURCE
    rows = list(rows)
    try:
        history = ml._rows(rows, league, source, before)
    except ml.ModelError as error:
        raise SnapshotError(str(error)) from None
    result = repeated_scenarios(history, len(fixtures)).get(league,
        dict(size=len(fixtures), matches=[], blocks=[], ranking=[], pending=[], simultaneous=False))
    methods, unavailable = {}, {}
    prepared, model = contextual._prepare(result, fixtures)
    if not prepared['eligible']:
        reason = prepared['diagnostic'] or 'Historique insuffisant pour comparer ces méthodes.'
        unavailable.update({name: reason for name in METHODS[:3]})
    else:
        fit, distributions = model['fit'], model['distributions']
        provenance = dict(source=SOURCE, league=league, training_before=before,
                          training_groups=fit[1], training_matches=len(history),
                          history_fingerprint=ml._fingerprint(history))
        methods['frequency'] = _method('league-frequency-v1',
            _distribution('frequency', len(fixtures), fit=fit), provenance)
        methods['clubs'] = _method(CLUB_VERSION,
            _distribution('independent', len(fixtures), distributions=distributions), provenance)
        methods['combined'] = _method(contextual.VERSION,
            _distribution('corrected', len(fixtures), fit=fit, distributions=distributions,
                          alpha=.5, normalization=model['normalization']), provenance, alpha=.5)
    try:
        methods['catboost'] = _catboost(rows, fixtures, source, league, before, estimate, engine)
    except (ml.ModelError, SnapshotError, OSError, KeyError, TypeError, ValueError) as error:
        unavailable['catboost'] = str(error) if isinstance(error, (ml.ModelError, SnapshotError)) else (
            'Le modèle CatBoost local est incomplet ou indisponible.')
    try:
        methods['context_neighbors'] = _context_neighbors(rows, fixtures, source, league, before, estimate, engine)
    except (ImportError, OSError, KeyError, TypeError, ValueError, AttributeError) as error:
        unavailable['context_neighbors'] = str(error) if isinstance(error, ValueError) else (
            'La configuration par contexte locale est absente ou indisponible.')
    return dict(methods=methods, unavailable=unavailable)


def _mixed_component(rows, fixtures, league, before):
    """One league at the mixed group's common cutoff; never use later members."""
    history = ml._rows(rows, league, SOURCE, before)
    size = len(fixtures)
    provenance = dict(source=SOURCE, league=league, training_before=before,
                      history_fingerprint=ml._fingerprint(history), training_matches=len(history))
    if size == 1:
        estimate = estimate_clubs(history, league, fixtures[0]['home'], fixtures[0]['away'], before)
        reason = 'Une seule affiche de cette compétition : seules les probabilités indépendantes des clubs sont composées.'
        unavailable = {name:reason for name in METHODS if name != 'clubs'}
        if not estimate['eligible']:
            unavailable['clubs'] = 'Au moins 30 résultats de cette compétition sur trois jours antérieurs sont requis.'
            return dict(methods={}, unavailable=unavailable)
        payload = _method(CLUB_VERSION, _distribution('independent', 1, distributions=[estimate['probabilities']]),
                          dict(provenance, training_groups=0), _component=True)
        return dict(methods={'clubs':payload}, unavailable=unavailable)
    result = repeated_scenarios(history, size).get(league,
        dict(size=size, matches=[], blocks=[], ranking=[], pending=[], simultaneous=False))
    prepared, model = contextual._prepare(result, fixtures, before=before)
    methods, unavailable = {}, {}
    if prepared['eligible']:
        fit, distributions = model['fit'], model['distributions']
        provenance['training_groups'] = fit[1]
        methods['frequency'] = _method('league-frequency-v1',_distribution('frequency',size,fit=fit),provenance)
        methods['clubs'] = _method(CLUB_VERSION,_distribution('independent',size,distributions=distributions),provenance)
        methods['combined'] = _method(contextual.VERSION,_distribution('corrected',size,fit=fit,
            distributions=distributions,alpha=.5,normalization=model['normalization']),provenance,alpha=.5)
    else:
        unavailable.update({name:prepared['diagnostic'] for name in METHODS[:3]})
    # Existing trained engines bind their own fixture-first UTC day. If a
    # later block would advance that cutoff, do not pretend it is causal for
    # the whole mixed group or silently substitute a different model.
    if min(fixture['timestamp'][:10] for fixture in fixtures) != before:
        unavailable.update({name:'Ce composant commence après le premier jour UTC du groupe ; le moteur enregistré ne prend pas en charge cette coupure commune.'
                            for name in ('catboost','context_neighbors')})
    else:
        for name, operation in (('catboost',lambda:_catboost(rows,fixtures,SOURCE,league,before,None,None)),
                                ('context_neighbors',lambda:_context_neighbors(rows,fixtures,SOURCE,league,before))):
            try:
                methods[name] = operation()
            except (ValueError,OSError,KeyError,TypeError,AttributeError,ImportError) as error:
                unavailable[name] = str(error) if isinstance(error,ValueError) else 'Moteur local indisponible pour ce composant.'
    return dict(methods=methods, unavailable=unavailable)


def build_mixed_methods(rows, fixtures, source=SOURCE):
    """Freeze products of independent league blocks, with one common UTC cutoff.

    Corrections remain inside their own league block. No repeated pattern or
    dependence between competitions is learned. A singleton component supplies
    only the explicitly independent clubs method. No model is fitted here.
    """
    fixtures, league, before = _fixtures(fixtures, source, allow_mixed=True)
    if fixtures != sorted(fixtures,key=lambda row:(row['timestamp'],int(row['match_id']))):
        raise SnapshotError('Le groupe mixte doit conserver l’ordre chronologique et les identifiants numériques.')
    grouped = {}
    for index,fixture in enumerate(fixtures):
        try:
            for key in ('league','home_id','away_id'):
                value = fixture[key]
                if isinstance(value,bool) or not str(value).isascii() or not str(value).isdecimal() or int(value)<=0:
                    raise ValueError()
            if str(fixture['home_id']) == str(fixture['away_id']):raise ValueError()
        except (KeyError,ValueError,TypeError):
            raise SnapshotError('Les identifiants de compétition et des deux clubs sont requis pour chaque affiche mixte.') from None
        grouped.setdefault(fixture['league'],[]).append((index,fixture))
    leagues = sorted(grouped,key=int)
    rows = list(rows)
    parts = {}
    for code in leagues:
        members = grouped[code]
        try:
            parts[code] = _mixed_component(rows,[fixture for _,fixture in members],code,before)
        except (ml.ModelError,ValueError,KeyError,TypeError) as error:
            raise SnapshotError(f'Historique de la compétition {code} invalide pour le groupe mixte : {error}') from None
    methods, unavailable = {}, {}
    for name in METHODS:
        missing = {code:parts[code]['unavailable'].get(name,'Méthode absente de ce composant.')
                   for code in leagues if name not in parts[code]['methods']}
        if missing:
            unavailable[name] = ' ; '.join(f'Compétition {code} : {reason}' for code,reason in missing.items())
            continue
        components = [dict(indices=[index for index,_ in grouped[code]],league=code,
                           method=parts[code]['methods'][name]) for code in leagues]
        provenance = dict(source=SOURCE,provider=PROVIDER,league='mixed',leagues=leagues,group_kind='mixed',
            training_before=before,fixtures=fixtures,assumes_block_independence=True,
            correction_scope='within_each_league_only',
            component_versions={code:dict(version=parts[code]['methods'][name]['version'],
                generation=parts[code]['methods'][name].get('generation')) for code in leagues})
        distribution = dict(schema=DISTRIBUTION_VERSION,type='block_product',size=len(fixtures),components=components)
        methods[name] = _method(MIXED_VERSION,distribution,provenance,
            generation=hashlib.sha256(json.dumps(provenance['component_versions'],sort_keys=True).encode()).hexdigest())
    return dict(methods=methods,unavailable=unavailable,source=SOURCE,league='mixed',leagues=leagues,
                before=before,size=len(fixtures),assumes_block_independence=True)
