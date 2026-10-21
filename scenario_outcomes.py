"""Pure verdicts for the exact ordered scenarios saved before a match group.

The result state remains separate from the group's lifecycle: settled means
evaluated, whereas a hit requires one of the saved top three to match exactly.
No current model, distribution, provider state or saved evaluation is read.
"""
from collections.abc import Mapping
from math import isfinite

from coupon_sizes import validate_size


METHOD_ORDER = ('frequency', 'clubs', 'combined', 'catboost', 'context_neighbors')
VALUES = ('1st', 'Égal', '2nd')
LABELS = {
    'pending': 'Scénarios en attente',
    'hit': 'Scénario réalisé',
    'miss': 'Aucun scénario proposé réalisé',
    'void': 'Scénarios non évalués · Annulé',
    'unresolved': 'Scénarios à vérifier',
    'unavailable': 'Vérification des scénarios indisponible',
}


def _result(state, *, matches=None, actual=None):
    return dict(state=state, label=LABELS[state], matches=matches or [], actual=actual)


def _sequence(value, size):
    if (not isinstance(value, (list, tuple)) or len(value) != size
            or any(not isinstance(item, str) or item not in VALUES for item in value)):
        raise ValueError('Le scénario doit conserver tous les résultats dans leur ordre enregistré.')
    return tuple(value)


def summarize(entry):
    """Compare all methods' frozen top three with a complete settled outcome.

    Nonfinal groups never produce a success or failure, even if stale actuals
    or evaluations are present. Legacy abbreviated rankings retain their one
    or two saved proposals. Longer rankings are bounded to the displayed top
    three. Damaged saved choices fail closed rather than yielding a false miss.
    Returned lists are independent copies; inputs are never changed.
    """
    if not isinstance(entry, Mapping):
        return _result('unavailable')
    status = entry.get('status')
    if status in ('pending', 'void', 'unresolved'):
        return _result(status)
    if status != 'settled':
        return _result('unavailable')
    try:
        size = validate_size(entry.get('size'))
        actual = _sequence(entry.get('actual'), size)
        methods = entry.get('methods')
        if not isinstance(methods, Mapping) or not methods:
            raise ValueError('Les scénarios proposés doivent être conservés.')
        if any(not isinstance(name, str) or not name.strip() for name in methods):
            raise ValueError('Chaque méthode doit conserver son nom.')
        names = [name for name in METHOD_ORDER if name in methods]
        names += sorted(set(methods) - set(names))
        matches = []
        for name in names:
            payload = methods[name]
            ranking = payload.get('ranking') if isinstance(payload, Mapping) else None
            if not isinstance(ranking, (list, tuple)) or not ranking:
                raise ValueError('Les propositions enregistrées sont nécessaires au verdict.')
            seen = set()
            for rank, item in enumerate(ranking[:3], 1):
                if not isinstance(item, Mapping):
                    raise ValueError('Une proposition enregistrée est invalide.')
                sequence = _sequence(item.get('sequence'), size)
                probability = item.get('probability')
                if (sequence in seen or isinstance(probability, bool)
                        or not isinstance(probability, (int, float))
                        or not isfinite(probability) or not 0 <= probability <= 1):
                    raise ValueError('Une proposition enregistrée est absente, répétée ou invalide.')
                seen.add(sequence)
                if sequence == actual:
                    matches.append(dict(method=name, rank=rank, sequence=list(sequence)))
        return _result('hit' if matches else 'miss', matches=matches, actual=list(actual))
    except (KeyError, TypeError, ValueError):
        return _result('unavailable')
