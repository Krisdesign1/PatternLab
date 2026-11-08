"""Validate the exact proposals retained in an archive, without a top-k cutoff.

The saved ranking is the proposal boundary. A saved full distribution can
verify its probabilities, but does not turn its other possible outcomes into
proposals. This module never fits a model, generates a ranking or performs I/O.
"""
from collections.abc import Mapping
from copy import deepcopy
from hashlib import sha256
import json
from math import fsum, isclose, isfinite

from coupon_sizes import validate_size


VERSION = 'scenario-proposal-archive-v1'
SCOPE = 'explicit_saved_rankings'
VALUES = ('1st', 'Égal', '2nd')
ADAPTIVE_POLICY = 'adaptive-scenario-threshold-v1'
COMPUTED_POLICY = 'all-computed-proposals-v1'


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError('Les propositions doivent être sérialisables sans valeur non finie.') from error


def _copy(value):
    return json.loads(_json(value))


def _digest(value):
    return sha256(_json(value).encode('utf-8')).hexdigest()


def _ranking(payload, size):
    ranking = payload.get('ranking') if isinstance(payload, Mapping) else None
    if not isinstance(ranking, (list, tuple)) or not ranking:
        raise ValueError('Chaque méthode doit conserver ses propositions explicites.')
    seen, chances = set(), []
    for item in ranking:
        if not isinstance(item, Mapping):
            raise ValueError('Une proposition enregistrée est invalide.')
        sequence, chance = item.get('sequence'), item.get('probability')
        if (not isinstance(sequence, (list, tuple)) or len(sequence) != size
                or any(not isinstance(value, str) or value not in VALUES for value in sequence)
                or tuple(sequence) in seen or isinstance(chance, bool)
                or not isinstance(chance, (int, float)) or not isfinite(chance)
                or not 0 <= chance <= 1):
            raise ValueError('Une proposition enregistrée est absente, répétée ou invalide.')
        seen.add(tuple(sequence))
        chances.append(float(chance))
    if fsum(chances) > 1 + 1e-9:
        raise ValueError('La masse des propositions distinctes ne peut pas dépasser un.')
    if any(later > earlier and not isclose(later, earlier, rel_tol=1e-9, abs_tol=0)
           for earlier, later in zip(chances, chances[1:])):
        raise ValueError('Les propositions doivent conserver leur ordre décroissant.')
    return ranking


def validate_method(payload, size):
    """Return an independent JSON copy of every explicitly saved ranking row.

For legacy full distributions the first three canonical choices keep their
original validator. Adaptive payloads keep this reference separately, allowing
one, two or more proposals above their frozen threshold. Every retained row is
checked against the same distribution; completeness belongs to the immutable
selection envelope captured at write time, never to a re-ranking on read.
Legacy ranking-only payloads may contain one or two choices.
"""
    validate_size(size)
    ranking = _ranking(payload, size)
    adaptive = payload.get('proposal_policy')
    if isinstance(adaptive, Mapping) and adaptive.get('version') == COMPUTED_POLICY:
        evidence = adaptive.get('evidence')
        if (not isinstance(evidence, Mapping)
                or evidence.get('status') not in ('qualified', 'not_qualified', 'unavailable')):
            raise ValueError('Le niveau de preuve séparé est invalide.')
        canonical = deepcopy(dict(payload))
        canonical.pop('proposal_policy', None)
        reference = canonical.pop('reference_ranking', None)
        if reference != list(ranking[:3]):
            raise ValueError('La référence ne correspond pas aux premiers choix conservés.')
        validate_method(canonical, size)
        return _copy(dict(payload))
    if adaptive is not None:
        threshold = adaptive.get('threshold') if isinstance(adaptive, Mapping) else None
        reference = payload.get('reference_ranking')
        if (not isinstance(adaptive, Mapping) or adaptive.get('version') != ADAPTIVE_POLICY
                or isinstance(threshold, bool) or not isinstance(threshold, (int, float))
                or not isfinite(threshold) or not 0 < threshold <= 1
                or not isinstance(reference, list) or len(reference) != 3
                or payload.get('distribution') is None):
            raise ValueError('La sélection par probabilité nécessite son seuil et sa référence figés.')
        import prospective_models as prospective
        canonical = deepcopy(dict(payload))
        canonical['ranking'] = deepcopy(reference)
        canonical.pop('proposal_policy', None)
        canonical.pop('reference_ranking', None)
        prospective.validate_method(canonical, size)
        # Completeness is attested when the selection envelope is captured.
        # A reader checks every retained row without enumerating 3**N again.
        for item in ranking:
            chance = prospective.probability_for_sequence(canonical, item['sequence'])
            if (item['probability'] < threshold
                    or not isclose(item['probability'], chance, rel_tol=1e-9, abs_tol=0)):
                raise ValueError('Une proposition ne respecte pas son seuil ou la distribution figée.')
        return _copy(dict(payload))
    if 'reference_ranking' in payload:
        raise ValueError('La référence séparée doit être accompagnée de sa politique de sélection.')
    if payload.get('distribution') is not None:
        if len(ranking) < 3:
            raise ValueError('La distribution figée nécessite ses trois premiers choix canoniques.')
        import prospective_models as prospective
        canonical = deepcopy(dict(payload))
        canonical['ranking'] = deepcopy(list(ranking[:3]))
        prospective.validate_method(canonical, size)
        # The frozen validator already checks the first three probabilities.
        # Only additional explicitly saved proposals need individual scoring.
        for item in ranking[3:]:
            chance = prospective.probability_for_sequence(payload, item['sequence'])
            if not isclose(item['probability'], chance, rel_tol=1e-9, abs_tol=0):
                raise ValueError('La probabilité d’une proposition contredit la distribution figée.')
    return _copy(dict(payload))


def _validate_shape(methods, size=None):
    """Check the proposal envelope only; never parse or score a distribution."""
    if not isinstance(methods, Mapping) or not methods:
        raise ValueError('Les méthodes proposées doivent être conservées.')
    if any(not isinstance(name, str) or not name.strip() for name in methods):
        raise ValueError('Chaque méthode doit conserver son nom.')
    if size is None:
        first = next(iter(methods.values()))
        ranking = first.get('ranking') if isinstance(first, Mapping) else None
        sequence = (ranking[0].get('sequence') if isinstance(ranking, (list, tuple))
                    and ranking and isinstance(ranking[0], Mapping) else None)
        if not isinstance(sequence, (list, tuple)):
            raise ValueError('La taille des propositions enregistrées est inconnue.')
        size = len(sequence)
    validate_size(size)
    for payload in methods.values():
        _ranking(payload, size)
    return size


def validate_methods(methods, size=None):
    """Validate all saved methods and retain all their rows, without generation."""
    size = _validate_shape(methods, size)
    return {name: validate_method(payload, size) for name, payload in methods.items()}


def _manifest(methods, *, unavailable=None, policy='explicit_saved_rankings'):
    """Build integrity fields from a shape-checked set, without model work."""
    if not isinstance(policy, str) or not policy.strip():
        raise ValueError('La politique des propositions doit être nommée.')
    unavailable = {} if unavailable is None else unavailable
    if (not isinstance(unavailable, Mapping)
            or any(not isinstance(name, str) or not name.strip()
                   or not isinstance(reason, str) or not reason.strip()
                   for name, reason in unavailable.items())
            or set(unavailable) & set(methods)):
        raise ValueError('Les méthodes indisponibles et leurs motifs doivent être explicites.')
    counts = {name: len(methods[name]['ranking']) for name in sorted(methods)}
    if policy == 'top3_per_method' and any(count != 3 for count in counts.values()):
        raise ValueError('La politique top3_per_method exige exactement trois propositions par méthode.')
    unique = {tuple(item['sequence']) for payload in methods.values() for item in payload['ranking']}
    info = dict(version=VERSION, scope=SCOPE, policy=policy,
                method_names=sorted(methods), proposal_counts=counts,
                saved_proposal_count=sum(counts.values()), distinct_proposal_count=len(unique),
                method_digests={name: _digest(methods[name]) for name in sorted(methods)},
                unavailable=_copy(dict(unavailable)))
    info['sha256'] = _digest(info)
    return info


def manifest(methods, *, unavailable=None, policy='explicit_saved_rankings'):
    """Semantically validate once at write time, then attest the saved set.

    Pass ``policy='top3_per_method'`` at current automatic producers when that is
    their generation rule. Other distribution outcomes never become proposals.
    """
    saved = validate_methods(methods)
    return _manifest(saved, unavailable=unavailable, policy=policy)


def validate_manifest(methods, value):
    """Verify integrity and shape on read, without model validation or scoring.

    Semantic validation belongs to ``manifest`` at write time. Re-reading a
    journal must not re-rank models or evaluate every distribution again.
    """
    if not isinstance(value, Mapping):
        raise ValueError('Le manifeste des propositions est absent.')
    _validate_shape(methods)
    expected = _manifest(methods, unavailable=value.get('unavailable'), policy=value.get('policy'))
    if _json(dict(value)) != _json(expected):
        raise ValueError('Le manifeste ne correspond plus aux propositions enregistrées.')
    return _copy(expected)
