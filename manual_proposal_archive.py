"""Exact proposal coverage for immutable manual calculations.

No history lookup, model fit, provider request or retrospective reconstruction.
Legacy statistical records attest only the selected engine's three choices.
"""
from copy import deepcopy
from math import fsum, isclose, isfinite

VALUES = ('1st', 'Égal', '2nd')
ENGINE_METHOD = {'statistical': 'combined', 'catboost': 'catboost',
                 'context_neighbors': 'context_neighbors'}
METHOD_ORDER = ('frequency', 'clubs', 'combined', 'catboost', 'context_neighbors')
VERSION = 'manual-proposals-v1'


def _ranking(payload, size):
    ranking = payload.get('ranking') if isinstance(payload, dict) else None
    if not isinstance(ranking, list) or len(ranking) < 3:
        raise ValueError('Chaque méthode de référence doit conserver ses trois premiers choix ; les propositions sélectionnées sont archivées séparément.')
    seen, chances = set(), []
    for row in ranking:
        if not isinstance(row, dict):
            raise ValueError('Une proposition comparative est invalide.')
        sequence, chance = row.get('sequence'), row.get('probability')
        if (not isinstance(sequence, list) or len(sequence) != size
                or any(value not in VALUES for value in sequence)
                or tuple(sequence) in seen or isinstance(chance, bool)
                or not isinstance(chance, (float, int)) or not isfinite(chance) or not 0 <= chance <= 1):
            raise ValueError('Un scénario ou une probabilité de comparaison est invalide.')
        seen.add(tuple(sequence)); chances.append(chance)
    if fsum(chances) > 1 + 1e-12 or any(b > a for a, b in zip(chances, chances[1:])):
        raise ValueError('Les probabilités comparatives ne respectent pas leur classement.')


def validate(methods, forecast, engine, size, *, cutoff=None, leagues=()):
    """Validate the complete displayed method set and selected-model agreement."""
    selected = ENGINE_METHOD.get(engine)
    if selected is None or not isinstance(methods, dict) or not methods:
        raise ValueError('Les méthodes comparatives affichées doivent être conservées.')
    if any(name not in METHOD_ORDER for name in methods):
        raise ValueError('Une méthode comparative enregistrée est inconnue.')
    expected = {'frequency', 'clubs', 'combined'} if engine == 'statistical' else {selected}
    if not expected.issubset(methods):
        raise ValueError('Une méthode affichée manque dans la sauvegarde de comparaison.')
    for name, payload in methods.items():
        if not isinstance(payload, dict) or not isinstance(payload.get('version'), str) or not payload['version']:
            raise ValueError('La version de chaque méthode comparative doit être conservée.')
        _ranking(payload, size)
        if payload.get('source', 'API-Football') != 'API-Football':
            raise ValueError('La comparaison doit provenir d’API-Football.')
        if payload.get('league') is not None and {str(payload['league'])} != set(map(str, leagues)):
            raise ValueError('La compétition de comparaison ne correspond pas aux matchs.')
        if cutoff and payload.get('training_before') is not None:
            from datetime import date
            before = payload['training_before']
            if not isinstance(before, str) or date.fromisoformat(before).isoformat() != before or before > cutoff:
                raise ValueError('La comparaison contient une coupure historique postérieure au calcul.')
        if payload.get('distribution') is not None:
            import prospective_models
            from scenario_proposal_archive import validate_method
            validate_method(payload, size)
    # Preserve exactly the selected engine's pre-existing ordering and masses.
    for archived, original in zip(methods[selected]['ranking'], forecast['ranking']):
        if archived['sequence'] != original['sequence'] or not isclose(
                archived['probability'], original['probability'], rel_tol=1e-9, abs_tol=1e-12):
            raise ValueError('Le classement comparatif ne correspond pas au moteur sélectionné.')
    return deepcopy(methods)


def methods(entry):
    """Return only frozen methods; never supplement a legacy calculation."""
    archived = entry.get('comparison_methods')
    if isinstance(archived, dict) and archived:
        if entry.get('proposal_selection') is not None:
            import adaptive_scenario_proposals
            return adaptive_scenario_proposals.effective_methods(entry)
        return deepcopy(archived)
    name = ENGINE_METHOD[entry['engine']]
    forecast = entry['forecast']
    return {name: dict(version=forecast.get('version') or entry['engine'],
        ranking=[dict(sequence=list(row['sequence']), probability=row['probability'])
                 for row in forecast['ranking']])}


def coverage(entry):
    saved = methods(entry)
    complete = bool(entry.get('comparison_methods')) or entry['engine'] != 'statistical'
    unique = {tuple(row['sequence']) for payload in saved.values() for row in payload['ranking']}
    adaptive = entry.get('proposal_selection') is not None
    abstained = adaptive and not unique
    return dict(version=VERSION, complete=complete, abstained=abstained,
        unavailable_methods=deepcopy((entry.get('proposal_selection') or {}).get('unavailable', {})),
        reason=('Aucun scénario proposé par la règle historique conservée.' if abstained else
                'Toutes les méthodes proposées sont conservées.' if complete else
                'Archive ancienne incomplète : seuls les trois choix du moteur sélectionné sont conservés ; '
                'les autres propositions affichées ne peuvent pas être vérifiées.'),
        method_names=[name for name in METHOD_ORDER if name in saved],
        proposal_count=len(unique), selected_method=ENGINE_METHOD[entry['engine']])


def evaluate(entry, actual=None):
    """Evaluate the union of saved choices, keeping unknown legacy misses unknown."""
    info, saved = coverage(entry), methods(entry)
    size = len(entry['fixtures'])
    if actual is not None and (not isinstance(actual, (list, tuple)) or len(actual) != size
                               or any(value not in VALUES for value in actual)):
        raise ValueError('Le résultat ordonné complet est nécessaire pour évaluer les propositions.')
    actual = tuple(actual) if actual is not None else None
    indexed = {}
    for name in info['method_names']:
        for rank, item in enumerate(saved[name]['ranking'], 1):
            key = tuple(item['sequence'])
            row = indexed.setdefault(key, dict(sequence=list(key), cells={}, actual=None))
            row['cells'][name] = dict(rank=rank, probability=item['probability'])
            if actual is not None:
                row['actual'] = key == actual
    rows = sorted(indexed.values(), key=lambda row: (
        min(cell['rank'] for cell in row['cells'].values()),
        tuple(row['cells'].get(name, {}).get('rank', 4) for name in info['method_names']), tuple(row['sequence'])))
    hits = [row for row in rows if row['actual'] is True]
    state = ('abstained' if info.get('abstained') else 'pending' if actual is None else
             'hit' if hits else 'miss' if info['complete'] else 'incomplete')
    return dict(state=state, rows=rows, coverage=info, actual=list(actual) if actual is not None else None,
                actual_in_choices=None if actual is None or state in ('incomplete', 'abstained') else bool(hits))
