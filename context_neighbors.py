"""Causal, contextual neighbors of ordered API-Football match groups.

Fixed bounded descriptors compare clubs' previous half-goal profiles in the
same home/away positions. Neighbor outcomes never enter those descriptors.
The local empirical distribution is conservatively mixed with the existing
club baseline; exact top-k ranking never enumerates all 3**20 outcomes.
"""
from collections import Counter, OrderedDict, defaultdict
from datetime import date, timedelta
import hashlib
import importlib
import json
from math import fsum, isclose, isfinite, prod

import api_football as api
import catboost_scenarios as ml
from coupon_sizes import top_independent, validate_size
from league_model import VALUES, outcome, score
from match_predictor import estimate
import scenario_features as features


VERSION = 'context-neighbors-v1'
SCHEMA = 'context-neighbors-distribution-v1'
DESCRIPTOR_VERSION = 'context-neighbors-descriptor-v1'
SOURCE = 'API-Football'
PROVIDER = 'api-football'
MIN_NEIGHBORS = 12
MIN_ESS = 8
SHRINK = 20
IDENTITY_PENALTY = .15
K_VALUES = (20, 50)
HALF_LIVES = (None, 365)
MIX_VALUES = (0.0, .1, .25, .5)
DEFAULT_PARAMETERS = dict(k=20, half_life_days=None, mix=0.0)
# Goal means are capped at four per club/half, league totals at six. Counts,
# rest, and rates use fixed scales; neither test outcomes nor fitted extrema
# determine distances. Zero-history support counts remain explicit features.
NUMERIC_SCALES = tuple(
    [(f'{profile}_last{window}_{field}', 4.0)
     for profile, window in (('home_role', 10), ('away_role', 10),
                             ('home_overall', 5), ('away_overall', 5))
     for field in ('ht_for', 'ht_against', 'sh_for', 'sh_against')]
    + [(f'{profile}_last{window}_rate_{label}', 1.0)
       for profile, window in (('home_role', 10), ('away_role', 10),
                               ('home_overall', 5), ('away_overall', 5))
       for label in ('1st', 'equal', '2nd')]
    + [(f'{profile}_{field}', scale)
       for profile in ('home_role', 'away_role')
       for field, scale in (('count', 40.0), ('days_rest', 30.0), ('last10_count', 10.0))]
    + [(f'league_last10_rate_{label}', 1.0) for label in ('1st', 'equal', '2nd')]
    + [('league_last10_ht_total', 6.0), ('league_last10_sh_total', 6.0)])


class ContextError(ValueError):
    """An inconsistent group, distribution, or unvalidated configuration."""


class _Catalog(list):
    """A JSON-list catalog with a bounded, disposable distance cache."""
    def __init__(self, entries):
        super().__init__(entries)
        self._distance_cache = OrderedDict()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ContextError('Une caractéristique ou une probabilité est absente ou non finie.')
    return float(value)


def _id(value):
    try:
        return api._numeric_id(value, 'du groupe ou du club')
    except ValueError as error:
        raise ContextError(str(error)) from None


def _day(value):
    try:
        return date.fromisoformat(value).isoformat()
    except (ValueError, TypeError):
        raise ContextError('Jour UTC du groupe absent ou invalide.') from None


def _params(params):
    if not isinstance(params, dict):
        raise ContextError('Les paramètres des voisins doivent être précisés.')
    k, half_life, mix = params.get('k'), params.get('half_life_days'), params.get('mix')
    if type(k) is not int or k not in K_VALUES:
        raise ContextError('Le nombre de voisins doit être 20 ou 50.')
    if half_life is not None and (type(half_life) is not int or half_life not in HALF_LIVES):
        raise ContextError('La demi-vie doit être absente ou égale à 365 jours.')
    mix = _number(mix)
    if mix not in MIX_VALUES:
        raise ContextError('La force de mélange doit être choisie parmi les valeurs validées.')
    return dict(k=k, half_life_days=half_life, mix=mix)


def _positions(values, size):
    if not isinstance(values, (list, tuple)) or len(values) != size:
        raise ContextError('Une distribution par match est nécessaire.')
    result = []
    for value in values:
        if not isinstance(value, dict) or set(value) != set(VALUES):
            raise ContextError('Chaque match doit conserver les trois résultats possibles.')
        row = {label: _number(value[label]) for label in VALUES}
        if any(not 0 < p <= 1 for p in row.values()) or not isclose(fsum(row.values()), 1, rel_tol=1e-10, abs_tol=1e-12):
            raise ContextError('Les probabilités des clubs doivent être positives et totaliser un.')
        result.append(row)
    return result


def _sequence(sequence, size):
    if not isinstance(sequence, (tuple, list)) or len(sequence) != size or any(v not in VALUES for v in sequence):
        raise ContextError('Le scénario doit conserver la taille et l’ordre du groupe.')
    return tuple(sequence)


def _group(context):
    try:
        matches = context['block']['matches']
        validate_size(len(matches))
        before = _day(context['before'])
        normalized, ids, leagues = [], set(), set()
        for raw in matches:
            if raw.get('provider') != PROVIDER:
                raise ContextError('Seuls les groupes API-Football sont comparables.')
            row = {key: raw[key] for key in ('home', 'away', 'match_id', 'league', 'home_id', 'away_id', 'timestamp')}
            for key in ('match_id', 'league', 'home_id', 'away_id'):
                row[key] = _id(row[key])
            row['timestamp'] = ml._utc_stamp(row['timestamp'])
            if row['match_id'] in ids or row['home_id'] == row['away_id']:
                raise ContextError('Les matchs et les deux clubs de chaque affiche doivent être distincts.')
            if not all(isinstance(row[k], str) and row[k].strip() for k in ('home', 'away')) or row['home'] == row['away']:
                raise ContextError('Deux clubs confirmés sont nécessaires.')
            ids.add(row['match_id'])
            leagues.add(row['league'])
            normalized.append(dict(row, provider=PROVIDER))
        if len(leagues) != 1:
            raise ContextError('Une seule compétition est autorisée par groupe.')
        if normalized != sorted(normalized, key=lambda r: (r['timestamp'], int(r['match_id']))):
            raise ContextError('Le groupe doit conserver l’ordre chronologique et les identifiants numériques.')
        if before != min(row['timestamp'][:10] for row in normalized):
            raise ContextError('Toutes les caractéristiques doivent précéder le premier jour UTC du groupe.')
        return normalized, leagues.pop(), before
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        if isinstance(error, ContextError):
            raise
        raise ContextError('Contexte ou identités du groupe absents ou invalides.') from None


def describe_group(context):
    """Describe a group's pre-first-day profiles without reading its outcomes."""
    matches, league, _ = _group(context)
    values = context.get('features')
    if not isinstance(values, list) or len(values) != len(matches):
        raise ContextError('Les profils antérieurs de chaque affiche sont nécessaires.')
    positions = []
    for row, item in zip(matches, values):
        try:
            if (item['provider'] != PROVIDER or str(item['league']) != league
                    or _id(item['home_id']) != row['home_id'] or _id(item['away_id']) != row['away_id']):
                raise ContextError('Les profils ne correspondent pas aux clubs et à leur rôle.')
            vector = [min(1.0, max(0.0, _number(item[column])/scale)) for column, scale in NUMERIC_SCALES]
        except (KeyError, TypeError):
            raise ContextError('Une caractéristique des profils de clubs est absente.') from None
        positions.append(dict(numeric=vector, home_id=row['home_id'], away_id=row['away_id']))
    return dict(schema=DESCRIPTOR_VERSION, source=SOURCE, provider=PROVIDER,
                league=league, size=len(matches), positions=positions)


def fixture_fingerprint(fixtures):
    """Bind a saved prediction to the ordered UTC fixtures and club identities."""
    fixtures = list(fixtures)
    try:
        before = min(ml._utc_stamp(row['timestamp'])[:10] for row in fixtures)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ContextError('Les affiches à identifier sont absentes ou invalides.') from None
    normalized, _, _ = _group(dict(block=dict(matches=fixtures), before=before))
    return hashlib.sha256(_json(normalized).encode()).hexdigest()


def fit_catalog(contexts):
    """Freeze historical descriptors and their completed outcomes, per size/league."""
    entries, identities, seen = [], set(), set()
    for context in contexts:
        descriptor = describe_group(context)
        matches, league, before = _group(context)
        identity = (league, len(matches))
        identities.add(identity)
        if len(identities) > 1:
            raise ContextError('Un catalogue doit conserver une seule compétition et une seule taille.')
        sequence = _sequence(context['block']['sequence'], len(matches))
        original = context['block']['matches']
        try:
            if sequence != tuple(outcome(score(row['ht']), score(row['ft'])) for row in original):
                raise ContextError('Les résultats historiques ne correspondent pas aux scores réglementaires.')
        except (KeyError, ValueError):
            raise ContextError('Un catalogue nécessite des résultats historiques HT/FT valides.') from None
        ids = [r['match_id'] for r in matches]
        key = tuple(ids)
        if key in seen:
            raise ContextError('Un groupe historique ne peut apparaître deux fois.')
        seen.add(key)
        seasons = {row.get('season') for row in original}
        if len(seasons) != 1 or type(next(iter(seasons))) is not int:
            raise ContextError('Chaque groupe historique doit rester dans sa saison réelle.')
        entries.append(dict(descriptor=descriptor, sequence=list(sequence), match_ids=ids,
                            first_day=before, end_day=max(r['timestamp'][:10] for r in matches),
                            season=next(iter(seasons)), matches=matches))
    return _Catalog(sorted(entries, key=lambda r: (r['first_day'], tuple(map(int, r['match_ids'])))))


def _distance(left, right):
    values, identities = [], []
    for a, b in zip(left['positions'], right['positions']):
        values.extend(abs(x-y) for x, y in zip(a['numeric'], b['numeric']))
        identities.extend((a['home_id'] != b['home_id'], a['away_id'] != b['away_id']))
    return fsum(values)/len(values) + IDENTITY_PENALTY*sum(identities)/len(identities)


def _distances(catalog, descriptor, matches, before):
    key = _json([descriptor, before, [row['match_id'] for row in matches]])
    cache = getattr(catalog, '_distance_cache', None)
    if cache is not None and key in cache:
        cache.move_to_end(key)
        return cache[key]
    target_ids = {row['match_id'] for row in matches}
    result = []
    for entry in catalog:
        previous = entry['descriptor']
        if (previous.get('schema') != DESCRIPTOR_VERSION or previous.get('provider') != PROVIDER
                or previous.get('source') != SOURCE
                or previous.get('league') != descriptor['league'] or previous.get('size') != descriptor['size']):
            raise ContextError('Le catalogue doit conserver la même source, compétition et taille.')
        end_day = _day(entry['end_day'])
        if end_day >= before or target_ids.intersection(entry['match_ids']):
            continue
        age = (date.fromisoformat(before)-date.fromisoformat(end_day)).days
        distance = _distance(previous, descriptor)
        result.append((distance, age, entry))
    result.sort(key=lambda item: (item[0], -int(item[2]['end_day'].replace('-', '')),
                                  tuple(map(int, item[2]['match_ids']))))
    if cache is not None:
        cache[key] = result
        if len(cache) > 512:
            cache.popitem(last=False)
    return result


def distribution(catalog, target, params):
    """Mix a club baseline with causally earlier, similarly situated groups."""
    params = _params(params)
    descriptor = describe_group(target)
    matches, league, before = _group(target)
    baseline = _positions(target.get('clubs'), len(matches))
    candidates = _distances(catalog, descriptor, matches, before)
    chosen = candidates[:params['k']]
    weighted = []
    for distance, age, entry in chosen:
        decay = 1.0 if params['half_life_days'] is None else 2.0**(-age/params['half_life_days'])
        weight = decay/(1+distance)
        if weight > 0:
            weighted.append((weight, distance, age, entry))
    total = fsum(row[0] for row in weighted)
    squares = fsum(row[0]**2 for row in weighted)
    ess = total*total/squares if squares > 0 else 0.0
    eligible = len(weighted) >= MIN_NEIGHBORS and ess >= MIN_ESS
    eta = params['mix']*ess/(ess+SHRINK) if eligible else 0.0
    counts, masses, neighbors = Counter(), defaultdict(list), []
    for weight, distance, age, entry in weighted:
        sequence = tuple(entry['sequence'])
        counts[sequence] += 1
        masses[sequence].append(weight)
        neighbors.append(dict(sequence=list(sequence), match_ids=list(entry['match_ids']),
            first_day=entry['first_day'], end_day=entry['end_day'], season=entry['season'],
            matches=entry['matches'], distance=distance, age_days=age,
            weight=weight/total, normalized_weight=weight/total, raw_weight=weight))
    support = dict(neighbors=len(weighted), ess=ess, eligible=eligible, candidates=len(candidates),
                   mean_distance=fsum(r['weight']*r['distance'] for r in neighbors) if neighbors else None)
    value = dict(schema=SCHEMA, source=SOURCE, provider=PROVIDER, league=league,
        size=len(matches), before=before, baseline=baseline,
        local_mass=[dict(sequence=list(sequence), probability=min(1.0, fsum(weights)/total), observed=counts[sequence])
                    for sequence, weights in sorted(masses.items())],
        eta=eta, support=support, neighbors=neighbors, parameters=params)
    value['ranking'] = rank(value)
    return value


def _read(value):
    if not isinstance(value, dict) or value.get('schema') != SCHEMA:
        raise ContextError('Distribution de voisins absente ou incompatible.')
    size = validate_size(value.get('size'))
    if value.get('source') != SOURCE or value.get('provider') != PROVIDER:
        raise ContextError('La distribution doit provenir d’API-Football.')
    baseline = _positions(value.get('baseline'), size)
    eta = _number(value.get('eta'))
    if not 0 <= eta <= .5:
        raise ContextError('Le mélange des distributions est invalide.')
    if not isinstance(value.get('local_mass'), list):
        raise ContextError('Les fréquences locales doivent être conservées.')
    local, observed = {}, {}
    for entry in value['local_mass']:
        sequence = _sequence(entry.get('sequence'), size)
        p = _number(entry.get('probability'))
        if sequence in local or not 0 < p <= 1+1e-12:
            raise ContextError('Les fréquences locales doivent être positives et uniques.')
        local[sequence] = min(1.0, p)
        observed[sequence] = entry.get('observed', 0)
    if local and not isclose(fsum(local.values()), 1, rel_tol=1e-10, abs_tol=1e-12) or not local and eta:
        raise ContextError('Les fréquences locales doivent totaliser un avant le mélange.')
    return size, baseline, eta, local, observed


def probability_for(value, sequence):
    """Evaluate a frozen JSON mixture, without the catalog or any later results."""
    size, baseline, eta, local, _ = _read(value)
    sequence = _sequence(sequence, size)
    return (1-eta)*prod(p[v] for p, v in zip(baseline, sequence)) + eta*local.get(sequence, 0.0)


def rank(value, limit=3):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ContextError('Limite de classement invalide.')
    _, baseline, eta, local, observed = _read(value)
    candidates = set(local)
    candidates.update(row['sequence'] for row in top_independent(baseline, limit))
    result = []
    n = value.get('support', {}).get('neighbors', 0)
    for sequence in candidates:
        q = prod(p[v] for p, v in zip(baseline, sequence))
        p = (1-eta)*q + eta*local.get(sequence, 0.0)
        result.append(dict(sequence=list(sequence), probability=p, club_probability=q,
                           frequency_probability=local.get(sequence, 0.0), observed=observed.get(sequence, 0),
                           expected=n*q, chance_probability=q))
    return sorted(result, key=lambda r: (-r['probability'], r['sequence']))[:limit]


def forecast(rows, fixtures, source=SOURCE, limit=3):
    """Use a saved, validation-chosen configuration; never fit or publish models."""
    if source != SOURCE:
        raise ContextError('Seule la source API-Football est autorisée.')
    rows = list(rows)
    fixtures = list(fixtures)
    validate_size(len(fixtures))
    try:
        before = min(ml._utc_stamp(row['timestamp'])[:10] for row in fixtures)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ContextError('Les dates des affiches sont absentes ou invalides.') from None
    target_stub = dict(block=dict(matches=fixtures), before=before)
    normalized, league, before = _group(target_stub)
    validation = importlib.import_module('contextual_validation')
    report = validation.load_report(league)
    if not report:
        raise ContextError('Évaluez d’abord les contextes de cette compétition pour disposer de réglages validés.')
    try:
        deployment = report['deployment']
        if (report.get('source') != SOURCE or str(report.get('league')) != league
                or deployment.get('source') != SOURCE or str(deployment.get('league')) != league
                or deployment.get('context_version') != VERSION
                or deployment.get('feature_version') != features.FEATURE_VERSION):
            raise ContextError('La version des profils ou du modèle de contexte a changé ; contrôlez sa compatibilité avec la campagne figée.')
        selection_through = _day(deployment['selection_through'])
        if before <= selection_through:
            raise ContextError('Cette période a servi à choisir les paramètres : choisissez des matchs après la validation.')
        cutoff = (date.fromisoformat(selection_through)+timedelta(days=1)).isoformat()
        selected_history = ml._rows(rows, league, SOURCE, cutoff)
        selection_fingerprint = ml._fingerprint(selected_history)
        if selection_fingerprint != deployment['selection_fingerprint']:
            raise ContextError('Les résultats ayant choisi les paramètres ont changé ; contrôlez les données et la campagne figée.')
        by_size = deployment['parameters']
        validated = str(len(fixtures)) in by_size
        parameters = _params(by_size[str(len(fixtures))] if validated else DEFAULT_PARAMETERS)
        generation = deployment['generation']
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, ContextError):
            raise
        raise ContextError('Les réglages de contexte enregistrés sont incomplets ou invalides.') from None
    history = ml._rows(rows, league, SOURCE, before)
    if any(type(row.get('season')) is not int for row in history):
        raise ContextError('La saison réelle de chaque résultat historique est nécessaire.')
    seasonal = importlib.import_module('seasonal_validation')
    groups = seasonal._groups(history, history, len(fixtures))
    contexts = seasonal._contexts(history, {len(fixtures): groups})[len(fixtures)] if history else []
    catalog = fit_catalog(contexts)
    estimates = [estimate(history, league, row['home'], row['away'], before) for row in normalized]
    target_features = [features.feature_for_fixture(history, row, before=before) for row in fixtures]
    target = dict(block=dict(matches=fixtures), before=before, features=target_features,
                  clubs=[item['probabilities'] for item in estimates])
    value = distribution(catalog, target, parameters)
    diagnostics = []
    if not validated:
        diagnostics.append('Cette taille n’a pas été évaluée : correction des voisins désactivée, référence des clubs seule.')
    if not value['support']['eligible']:
        diagnostics.append('Support local insuffisant : au moins 12 voisins et un effectif effectif de 8 sont requis ; référence des clubs seule.')
    elif parameters['mix'] == 0:
        diagnostics.append('La validation a retenu un mélange nul : les voisins restent descriptifs.')
    eligible = all(item['eligible'] for item in estimates)
    if not eligible:
        diagnostics.append('Au moins 30 matchs de cette compétition sur trois jours antérieurs sont nécessaires.')
    personalized = sum(item['personalized'] for item in estimates)
    return dict(version=VERSION, source=SOURCE, provider=PROVIDER, method='Contextes comparables',
        eligible=eligible, league=league, size=len(fixtures), before=before, training_before=before,
        training_groups=len(catalog), training_matches=len(history), parameters=parameters,
        generation=generation, selection_through=selection_through, selection_fingerprint=selection_fingerprint,
        fixture_fingerprint=fixture_fingerprint(fixtures),
        history_fingerprint=ml._fingerprint(history), descriptor_version=DESCRIPTOR_VERSION,
        club_estimates=estimates, personalized_count=personalized, personalized_matches=personalized,
        neighbors=value['neighbors'], support=value['support'], eta=value['eta'],
        ranking=rank(value, limit) if eligible else [], distribution=value,
        normalization=1.0, latest=max((row['timestamp'] for row in history), default=None),
        validated_size=validated, diagnostics=diagnostics, diagnostic=' '.join(diagnostics))
