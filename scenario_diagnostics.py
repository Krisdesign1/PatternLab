"""Explain an existing scenario estimate, using its saved fields only.

This module does not predict, read histories, load artifacts, or score a new
scenario. In particular, ``saved_methods`` is authoritative: current club
profiles must never be attached to an older frozen journal forecast.
Probabilities shown per fixture are the base probabilities before any joint
scenario correction; they are not marginals of the corrected distribution.
"""
from math import fsum, isclose, isfinite


SCHEMA = 'scenario-diagnostics-v1'
VALUES = ('1st', 'Égal', '2nd')
ENGINES = {'statistical': 'combined', 'combined': 'combined', 'clubs': 'clubs',
           'frequency': 'frequency', 'catboost': 'catboost',
           'context_neighbors': 'context_neighbors'}


def _number(value, name, low=0, high=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError(f'{name} absent ou non fini.')
    if value < low or high is not None and value > high:
        raise ValueError(f'{name} hors plage.')
    return float(value)


def _probability(value):
    return min(1.0, _number(value, 'Probabilité', high=1+1e-12))


def _positions(values, size):
    if values is None:
        return None
    if not isinstance(values, (list, tuple)) or len(values) != size:
        raise ValueError('Les probabilités doivent conserver les positions du groupe.')
    return [_probabilities(value) for value in values]


def _probabilities(value):
    if not isinstance(value, dict) or set(value) != set(VALUES):
        raise ValueError('Les probabilités doivent contenir exactement 1st, Égal et 2nd.')
    result = {label: _probability(value[label]) for label in VALUES}
    if not isclose(fsum(result.values()), 1, rel_tol=1e-10, abs_tol=1e-12):
        raise ValueError('Les probabilités par affiche doivent totaliser un.')
    return result


def _sequence(value, size):
    if not isinstance(value, (list, tuple)) or len(value) != size or any(v not in VALUES for v in value):
        raise ValueError('Le scénario doit conserver les résultats et les positions du groupe.')
    return list(value)


def _size(value):
    if type(value) is not int or not 2 <= value <= 20:
        raise ValueError('La taille du groupe doit être comprise entre 2 et 20.')
    return value


def _profile(value):
    if value is None:
        return None
    if not isinstance(value, dict) or type(value.get('matches')) is not int or value['matches'] < 0:
        raise ValueError('Effectif du profil de club invalide.')
    result = {'matches': value['matches']}
    for name in ('scored', 'conceded'):
        if name not in value:
            continue
        pair = value[name]
        if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                or any(type(count) is not int or count < 0 for count in pair)):
            raise ValueError('Les buts du profil doivent conserver les deux mi-temps.')
        result[name] = list(pair)
    return result


def _ranking(value, size):
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError('Classement du scénario invalide.')
    result, previous, seen = [], float('inf'), set()
    for row in value:
        if not isinstance(row, dict):
            raise ValueError('Ligne de classement invalide.')
        sequence, probability = _sequence(row.get('sequence'), size), _probability(row.get('probability'))
        if tuple(sequence) in seen or probability > previous+1e-12:
            raise ValueError('Le classement fourni contient un doublon ou ne suit pas les probabilités.')
        result.append({'sequence': sequence, 'probability': probability})
        previous = probability
        seen.add(tuple(sequence))
    if fsum(row['probability'] for row in result) > 1+1e-12:
        raise ValueError('La somme des probabilités du classement dépasse un.')
    return result


def _choice(probabilities):
    if probabilities is None:
        return None, []
    maximum = max(probabilities.values())
    choices = sorted(label for label in VALUES if probabilities[label] == maximum)
    return choices[0], choices


def _support(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError('Support des voisins invalide.')
    result = {}
    for name in ('neighbors', 'candidates'):
        if name in value:
            if type(value[name]) is not int or value[name] < 0:
                raise ValueError('Nombre de voisins invalide.')
            result[name] = value[name]
    for name in ('ess', 'mean_distance'):
        if value.get(name) is not None:
            result[name] = _number(value[name], 'Support des voisins')
    if 'eligible' in value:
        if type(value['eligible']) is not bool:
            raise ValueError('Éligibilité du support invalide.')
        result['eligible'] = value['eligible']
    return result


def analyze(estimate, *, engine='statistical', saved_methods=None):
    """Describe supplied live or frozen fields without using later match data.

    ``estimate`` can include ``fixtures`` to label its ordered rows. To explain
    a journal entry, pass only its saved fixtures/size here and its method map as
    ``saved_methods``. Old snapshots often retain probabilities but no league
    baseline or club profiles; those fields remain ``None``, never zero.

    ``assumes_independence`` describes the final distribution, whereas
    ``product_baseline`` describes only its independent base component.
    """
    if engine not in ENGINES:
        raise ValueError('Moteur de scénario inconnu.')
    if not isinstance(estimate, dict):
        raise ValueError('Estimation de scénario absente.')
    method, frozen = ENGINES[engine], saved_methods is not None
    if frozen:
        if not isinstance(saved_methods, dict) or not isinstance(saved_methods.get(method), dict):
            raise ValueError('La méthode sélectionnée est absente de cet instantané.')
        payload = saved_methods[method]
    else:
        payload = estimate
    distribution = payload.get('distribution') or {}
    if not isinstance(distribution, dict):
        raise ValueError('Distribution sauvegardée invalide.')
    details = payload.get('club_estimates')
    fixtures = estimate.get('fixtures')
    if details is not None and not isinstance(details, (list, tuple)):
        raise ValueError('Les profils doivent conserver les positions du groupe.')
    if fixtures is not None and not isinstance(fixtures, (list, tuple)):
        raise ValueError('Les affiches doivent conserver les positions du groupe.')
    ranking_input = payload.get('ranking')
    if ranking_input is not None and not isinstance(ranking_input, (list, tuple)):
        raise ValueError('Classement du scénario invalide.')
    sizes = [value for value in (estimate.get('size'), payload.get('size'), distribution.get('size')) if value is not None]
    sizes += [len(value) for value in (details, fixtures) if value is not None]
    if ranking_input:
        first = ranking_input[0]
        if not isinstance(first, dict) or not isinstance(first.get('sequence'), (list, tuple)):
            raise ValueError('Premier scénario invalide.')
        sizes.append(len(first['sequence']))
    size = _size(sizes[0]) if sizes else None
    if any(_size(value) != size for value in sizes):
        raise ValueError('Le nombre d’affiches, de profils et de résultats doit correspondre.')
    ranking = _ranking(ranking_input, size) if size is not None else []
    kind = distribution.get('type')
    if kind is None:
        kind = ('context_mixture' if method == 'context_neighbors' else
                'frequency' if method == 'frequency' else
                'independent' if method == 'clubs' else 'corrected')
    if kind not in ('frequency', 'independent', 'corrected', 'context_mixture'):
        raise ValueError('Type de distribution inconnu.')
    alpha = payload.get('alpha', distribution.get('alpha'))
    alpha = _number(alpha, 'Force de correction', high=1) if alpha is not None else None
    eta = payload.get('eta', distribution.get('eta'))
    eta = _number(eta, 'Poids des voisins', high=.5) if eta is not None else None
    support = _support(payload.get('support', distribution.get('support')))

    positions, position_source = None, None
    if details is not None:
        if any(not isinstance(item, dict) for item in details):
            raise ValueError('Profil par affiche invalide.')
        if all(item.get('probabilities') is not None for item in details):
            positions = _positions([item['probabilities'] for item in details], size)
            position_source = 'catboost' if method == 'catboost' else 'clubs'
        elif any(item.get('probabilities') is not None for item in details):
            raise ValueError('Les probabilités par affiche sont partiellement absentes.')
    if positions is None and size is not None:
        fit = distribution.get('fit', {})
        if kind == 'frequency' and not isinstance(fit, dict):
            raise ValueError('Fréquences sauvegardées invalides.')
        saved_positions = (distribution.get('baseline') if kind == 'context_mixture'
                           else fit.get('prior') if kind == 'frequency'
                           else distribution.get('distributions'))
        positions = _positions(saved_positions, size)
        if positions is not None:
            position_source = 'historical_position_prior' if kind == 'frequency' else ('catboost' if method == 'catboost' else 'clubs')

    rows, missing = [], []
    for index in range(size or 0):
        item = details[index] if details is not None else {}
        fixture = fixtures[index] if fixtures is not None else {}
        if not isinstance(fixture, dict):
            raise ValueError('Affiche invalide.')
        p = positions[index] if positions is not None else None
        baseline = _probabilities(item['baseline']) if item.get('baseline') is not None else None
        home_profile, away_profile = _profile(item.get('home_profile')), _profile(item.get('away_profile'))
        personalized = item.get('personalized')
        if personalized is not None and type(personalized) is not bool:
            raise ValueError('Indicateur de personnalisation invalide.')
        first_choice, choices = _choice(p)
        baseline_first, _ = _choice(baseline)
        row = dict(position=index+1, match_id=fixture.get('match_id', item.get('match_id')),
                   home=fixture.get('home', item.get('home')), away=fixture.get('away', item.get('away')),
                   timestamp=fixture.get('timestamp', item.get('timestamp')),
                   probabilities=p, probability_source=position_source, baseline=baseline,
                   delta_points={label: 100*(p[label]-baseline[label]) for label in VALUES} if p is not None and baseline is not None else None,
                   first_choice=first_choice, first_choices=choices, baseline_first_choice=baseline_first,
                   personalized=personalized, home_profile=home_profile, away_profile=away_profile,
                   home_matches=home_profile['matches'] if home_profile else None,
                   away_matches=away_profile['matches'] if away_profile else None)
        known_counts = [profile['matches'] for profile in (home_profile, away_profile) if profile is not None]
        row['limited_history'] = True if any(count < 3 for count in known_counts) else (False if len(known_counts) == 2 else None)
        rows.append(row)
    known_rows = sum(row['personalized'] is not None for row in rows)
    count = sum(row['personalized'] is True for row in rows) if size is not None and known_rows == size else None
    if count is None and not frozen:
        count = payload.get('personalized_count', payload.get('personalized_matches'))
        if count is not None and (type(count) is not int or size is None or not 0 <= count <= size):
            raise ValueError('Effectif personnalisé invalide.')
    personalization = dict(known=count is not None, known_rows=known_rows, total=size,
                           personalized=count, share=count/size if count is not None else None)
    leader = ranking[0]['sequence'] if ranking else None
    probability = ranking[0]['probability'] if ranking else None
    product_baseline = positions is not None and kind != 'frequency'
    independent = (kind == 'independent' or kind == 'corrected' and alpha == 0
                   or kind == 'context_mixture' and eta == 0)
    if kind == 'context_mixture' and eta is None:
        independent = None
    marginal_leader = [row['first_choice'] for row in rows] if positions is not None else None
    comparison, explanation = [], []
    if leader is not None:
        explanation.append('Le classement fourni place « ' + ' → '.join(leader) + ' » en tête avec '
                           f'{100*probability:.2f} % pour le scénario complet.')
        if probability < 1:
            explanation.append(f'Les autres scénarios conservent {100*(1-probability):.2f} % de la probabilité. '
                               'Le premier choix ne garantit pas sa réalisation.')
        else:
            explanation.append('Même une estimation de 100 % décrit ce modèle et ne garantit pas le résultat réel.')
    else:
        missing.append('ranking')
        explanation.append('Le premier scénario n’est pas disponible dans les champs fournis.')
    if product_baseline:
        explanation.append('La composante de base multiplie les probabilités des affiches dans leur ordre ; '
                           'ce produit suppose leur indépendance.')
        if marginal_leader and len(set(marginal_leader)) == 1:
            explanation.append(f'Chaque affiche place « {marginal_leader[0]} » en tête avant correction. '
                               'Le produit de ces premiers choix favorise donc le même résultat répété.' +
                               (' Certaines affiches présentent des premiers choix à égalité.' if any(len(row['first_choices']) > 1 for row in rows) else ''))
        if leader is not None and marginal_leader != leader:
            explanation.append('Le premier scénario final diffère de l’assemblage des premiers choix par affiche : '
                               'la correction des scénarios intervient aussi dans le classement.')
    elif kind == 'frequency':
        explanation.append('Les répétitions seules utilisent les scénarios de la compétition et leurs fréquences par position ; '
                           'elles ne personnalisent pas les probabilités selon les clubs sélectionnés.')
    if kind == 'context_mixture':
        if eta is not None and eta > 0:
            explanation.append(f'Le scénario final mélange le produit des clubs et les fréquences des voisins '
                               f'(poids effectif des voisins : {100*eta:.2f} %). Ce mélange n’est pas un simple produit indépendant.')
        elif eta == 0:
            explanation.append('Le poids effectif des voisins est nul : les répétitions locales ne modifient pas les probabilités des clubs.')
        else:
            missing.append('eta')
            explanation.append('Le poids des voisins n’est pas conservé ; leur contribution exacte est indisponible.')
        if support:
            parts = []
            if 'neighbors' in support:
                parts.append(f"{support['neighbors']} voisins")
            if 'ess' in support:
                parts.append(f"effectif pondéré {support['ess']:.2f}")
            if parts:
                explanation.append('Support local conservé : ' + ', '.join(parts) + '. Il ne mesure pas une confiance prédictive.')
        if payload.get('validated_size') is False:
            explanation.append('Cette taille n’a pas été évaluée : aucune correction de voisins n’a été retenue pour elle.')
        elif support and support.get('eligible') is False:
            explanation.append('Le support local est déclaré insuffisant dans cette estimation.')
    elif kind == 'corrected':
        if alpha == 0:
            explanation.append('La force de répétition alpha est nulle : les répétitions ne corrigent pas la base '
                               + ('CatBoost.' if method == 'catboost' else 'des clubs.'))
        else:
            explanation.append('La fréquence historique corrige la composante de base ; le combiné partage donc des informations '
                               'avec les méthodes « clubs » et « répétitions ».' +
                               (f' Force conservée : alpha = {alpha:g}.' if alpha is not None else ''))
    if count is not None:
        label = 'couverture des profils de clubs' if method == 'catboost' else 'affiches personnalisées selon les profils de clubs'
        explanation.append(f'{count}/{size} {label} ({100*count/size:.1f} %).')
        if count < size:
            explanation.append('CatBoost utilise aussi ses autres caractéristiques : une faible couverture des profils ne signifie pas un retour intégral à la moyenne.'
                               if method == 'catboost' else 'Les affiches sans historique de clubs suffisant utilisent la tendance de la compétition.')
    else:
        missing.append('personalization')
        explanation.append('La couverture des profils de clubs n’est pas conservée ; son pourcentage est indisponible.')
    if any(row['limited_history'] for row in rows):
        explanation.append('Au moins un club dispose de moins de trois précédents dans son rôle domicile ou extérieur.')
    if not any(row['home_profile'] is not None or row['away_profile'] is not None for row in rows):
        missing.append('club_profiles')
        explanation.append('Les effectifs et les buts des profils de clubs ne sont pas conservés dans cet instantané.')
    delta_rows = [row for row in rows if row['delta_points'] is not None]
    maximum_delta = max((abs(delta) for row in delta_rows for delta in row['delta_points'].values()), default=None)
    changed = sum(row['first_choice'] != row['baseline_first_choice'] for row in delta_rows)
    if delta_rows:
        explanation.append(f'Par rapport à la tendance de compétition, le premier choix change pour {changed}/{len(delta_rows)} affiches comparables ; '
                           f'l’écart absolu maximal est de {maximum_delta:.2f} points de pourcentage.')
        explanation.append('Ces écarts sont des comparaisons de probabilités, pas une mesure causale de l’effet des clubs.')
    else:
        missing.append('league_baseline')
        explanation.append('La tendance de compétition par affiche n’est pas conservée ; les écarts à cette référence sont indisponibles.')
    if positions is None:
        missing.append('per_fixture_probabilities')
        explanation.append('Les probabilités par affiche ne sont pas conservées ; elles ne sont pas reconstruites à partir de données actuelles.')
    if frozen:
        for name, other in saved_methods.items():
            if name not in ENGINES.values() or not isinstance(other, dict):
                continue
            other_rank = _ranking(other.get('ranking'), size) if size is not None else []
            if other_rank:
                comparison.append(dict(method=name, leader=other_rank[0]['sequence'], probability=other_rank[0]['probability'],
                                       same_leader=leader == other_rank[0]['sequence'] if leader is not None else None))
        comparison.sort(key=lambda item: item['method'])
        if sum(item['same_leader'] is True for item in comparison) > 1:
            explanation.append('Plusieurs méthodes sauvegardées placent ce même scénario en tête. '
                               'Elles partagent des données ou des composantes ; cet accord n’est pas constitué de validations indépendantes.')
        explanation.append('Ce diagnostic utilise uniquement les champs sauvegardés ; aucun profil actuel ne remplace les informations anciennes manquantes.')
    return dict(schema=SCHEMA, engine=engine, method=method, frozen=frozen, size=size,
                leader=leader, probability=probability, remaining_probability=1-probability if probability is not None else None,
                explanation=explanation, rows=rows, personalization=personalization,
                distribution_kind=kind, product_baseline=product_baseline, assumes_independence=independent,
                marginal_product_leader=marginal_leader if product_baseline else None,
                leader_matches_product=leader == marginal_leader if product_baseline and leader is not None else None,
                alpha=alpha, eta=eta, support=support,
                baseline_comparison=dict(available=len(delta_rows), changed_first_choices=changed, max_abs_delta_points=maximum_delta),
                methods=comparison, missing=missing)
