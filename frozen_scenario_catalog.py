"""A complete, lazy catalogue of an already frozen scenario distribution.

The compact descriptor fingerprints mathematical inputs and the original
three-row reference. It is not another model, a selection policy or a claim of
predictive quality. Pages expose the full 3**N space in probability/tuple order;
their length limits presentation only. No data, training or store is accessed.
"""
from collections.abc import Mapping
from copy import deepcopy
from hashlib import sha256
from heapq import heappop, heappush, merge
from itertools import product
import json
from math import isclose, isfinite, prod
from time import monotonic

import prospective_models as frozen
from league_model import VALUES


VERSION = 'frozen-scenario-catalog-v1'
ORDER = 'probability_desc_sequence_asc'
DEFAULT_TIME_BUDGET_SECONDS = 5.0
MAX_PAGE_SIZE = 1000


class CatalogError(ValueError):
    """The catalogue, requested page or frozen input is inconsistent."""


class CatalogBudgetExceeded(TimeoutError):
    """No complete page could be produced within the computation budget."""


def _digest(value):
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        raise CatalogError('Le catalogue doit conserver des valeurs JSON finies.') from None
    return sha256(text.encode()).hexdigest()


def _size(method):
    if not isinstance(method, Mapping) or not isinstance(method.get('version'), str) or not method['version']:
        raise CatalogError('La version de la méthode figée doit être conservée.')
    distribution = method.get('distribution')
    if not isinstance(distribution, Mapping) or distribution.get('schema') != frozen.DISTRIBUTION_VERSION:
        raise CatalogError('La distribution figée du catalogue est absente ou inconnue.')
    size = distribution.get('size')
    if type(size) is not int or not 2 <= size <= 20:
        raise CatalogError('Un catalogue doit contenir de 2 à 20 matchs.')
    return size


def _reference(method, size):
    ranking = method.get('reference_ranking', method.get('ranking'))
    if not isinstance(ranking, list) or len(ranking) < 3:
        raise CatalogError('La référence originale des trois premiers scénarios est nécessaire.')
    result = []
    for row in ranking[:3]:
        if not isinstance(row, Mapping):
            raise CatalogError('La référence du catalogue est invalide.')
        sequence = row.get('sequence')
        chance = row.get('probability')
        if (not isinstance(sequence, (list, tuple)) or len(sequence) != size
                or any(label not in VALUES for label in sequence)
                or type(chance) not in (int, float) or not isfinite(chance) or not 0 <= chance <= 1 + 1e-12):
            raise CatalogError('La référence du catalogue est invalide.')
        result.append(dict(sequence=list(sequence), probability=float(chance)))
    return result


def _math_input(distribution):
    """Project mathematical fields only, including nested competition models.

    Cheap archive reads must not rerank or rescore. Admissibility is checked by
    the producer/reader's normal snapshot validator, and again when a page is
    actually requested. Later fixture/provenance bindings do not change this
    mathematical identity.
    """
    try:
        kind = distribution['type']
        value = dict(schema=distribution['schema'], type=kind, size=distribution['size'])
        if kind == 'block_product':
            value['components'] = [dict(indices=list(block['indices']),
                distribution=_math_input(block['method']['distribution']))
                for block in distribution['components']]
        elif kind == 'context_mixture':
            value.update(baseline=distribution['baseline'], eta=distribution['eta'],
                local_mass=sorted([dict(sequence=list(row['sequence']), probability=row['probability'])
                    for row in distribution['local_mass']], key=lambda row: tuple(row['sequence'])))
        elif kind in ('frequency', 'independent', 'corrected'):
            if kind != 'independent':
                fit = distribution['fit']
                value.update(fit=dict(n=fit['n'], prior=fit['prior'],
                    counts=sorted([dict(sequence=list(row['sequence']), count=row['count'])
                        for row in fit['counts']], key=lambda row: tuple(row['sequence']))),
                    prior_weight=distribution['prior_weight'])
            if kind != 'frequency':
                value['distributions'] = distribution['distributions']
            if kind == 'corrected':
                value.update(alpha=distribution['alpha'], normalization=distribution['normalization'])
        else:
            raise CatalogError('Le type de distribution du catalogue est inconnu.')
        return value
    except (KeyError, TypeError, AttributeError):
        raise CatalogError('La distribution figée du catalogue est incomplète.') from None


def descriptor(method):
    """Fingerprint a validated frozen method without generating any outcomes.

    This inexpensive operation verifies structural identity only. It must not
    be substituted for the normal mathematical snapshot validation at capture.
    """
    size = _size(method)
    value = dict(version=VERSION, size=size, total=3**size, order=ORDER,
        method_version=method['version'],
        distribution_sha256=_digest(_math_input(method['distribution'])),
        reference_sha256=_digest(_reference(method, size)))
    value['sha256'] = _digest(value)
    return value


def validate_descriptor(method, description):
    """Validate the compact archive binding without ranking or scoring."""
    expected = descriptor(method)
    if not isinstance(description, Mapping) or dict(description) != expected:
        raise CatalogError('Le catalogue ne correspond pas à la distribution figée ou à sa référence.')
    return deepcopy(expected)


class _Budget:
    def __init__(self, callback, seconds):
        if callback is not None and not callable(callback):
            raise CatalogError('Le contrôle du temps doit être une fonction.')
        if type(seconds) not in (int, float) or not isfinite(seconds) or seconds <= 0:
            raise CatalogError('Le budget de calcul doit être un nombre de secondes strictement positif.')
        self.callback = callback
        self.deadline = monotonic() + seconds
        self.operations = 0

    def check(self, *, force=False):
        self.operations += 1
        if force or self.operations % 64 == 0:
            if self.callback is not None:
                self.callback()
            if monotonic() >= self.deadline:
                raise CatalogBudgetExceeded('Le calcul de cette page a dépassé le temps disponible ; '
                    'aucune page partielle n’a été retournée. Choisissez une page plus proche du début.')


def _model(method, budget):
    budget.check(force=True)
    size = _size(method)
    reference = _reference(method, size)
    if method.get('scenario_catalog') is not None:
        validate_descriptor(method, method['scenario_catalog'])
    model = frozen._read_distribution(method['distribution'])
    expected = frozen._rank(model, 3)
    for saved, actual in zip(reference, expected):
        if (tuple(saved['sequence']) != tuple(actual['sequence'])
                or not isclose(saved['probability'], actual['probability'], rel_tol=1e-9, abs_tol=0)):
            raise CatalogError('La référence du catalogue contredit la distribution figée.')
    budget.check(force=True)
    return model, reference


def _probability(model, sequence):
    """Use the frozen arithmetic, allowing representable zero/underflow mass.

    Existing snapshots require positive per-match inputs. Some products of very
    small positive inputs can nevertheless round to zero. Such outcomes still
    belong to the complete catalogue and are ordered last, lexicographically.
    """
    kind = model['type']
    if kind == 'block_product':
        value = prod(_probability(block['model'], tuple(sequence[index] for index in block['indices']))
                     for block in model['blocks'])
    elif kind == 'frequency':
        value = frozen._frequency_probability(model['fit'], sequence, model['weight'])
    else:
        value = prod(position[label] for position, label in zip(model['distributions'], sequence))
        if kind == 'context_mixture':
            value = (1-model['eta'])*value + model['eta']*model['local'].get(sequence, 0)
        elif kind == 'corrected':
            value *= model['corrections'].get(sequence, model['unseen']) / model['normalization']
    if not isfinite(value) or not 0 <= value <= 1 + 1e-12:
        raise CatalogError('La probabilité du catalogue est invalide.')
    return value


def _row(sequence, probability):
    return dict(sequence=list(sequence), probability=float(probability))


def _key(row):
    return -row['probability'], tuple(row['sequence'])


def _products(positions, probability_for, budget):
    choices = [sorted(position, key=lambda label: (-position[label], label)) for position in positions]
    seen, heap = set(), []

    def push(indices):
        if indices in seen:
            return
        seen.add(indices)
        budget.check()
        sequence = tuple(choices[position][index] for position, index in enumerate(indices))
        chance = probability_for(sequence)
        if chance > 0:
            heappush(heap, (-chance, sequence, indices))

    push((0,) * len(positions))
    while heap:
        negative, sequence, indices = heappop(heap)
        budget.check()
        yield _row(sequence, -negative)
        for position, index in enumerate(indices):
            if index < len(choices[position]) - 1:
                neighbor = list(indices)
                neighbor[position] += 1
                push(tuple(neighbor))


def _sparse(model, budget):
    kind = model['type']
    positions = model['fit'][2] if kind == 'frequency' else model['distributions']
    exceptions = model['local'] if kind == 'context_mixture' else model['fit'][0]

    def ordinary_probability(sequence):
        value = prod(position[label] for position, label in zip(positions, sequence))
        if kind == 'frequency':
            return (model['weight'] * value) / (model['fit'][1] + model['weight'])
        if kind == 'corrected':
            return value * (model['unseen'] / model['normalization'])
        return (1-model['eta'])*value

    exceptional = []
    for sequence in exceptions:
        budget.check()
        chance = _probability(model, sequence)
        if chance > 0:
            exceptional.append(_row(sequence, chance))
    exceptional.sort(key=_key)
    ordinary = (row for row in _products(positions, ordinary_probability, budget)
                if tuple(row['sequence']) not in exceptions)
    yield from merge(exceptional, ordinary, key=_key)


class _Choices:
    def __init__(self, stream):
        self.stream = iter(stream)
        self.rows = []
        self.finished = False

    def get(self, index):
        while len(self.rows) <= index and not self.finished:
            try:
                self.rows.append(next(self.stream))
            except StopIteration:
                self.finished = True
        return self.rows[index] if index < len(self.rows) else None


def _blocks(model, budget):
    components = [_Choices(_positive(block['model'], budget)) for block in model['blocks']]
    heap, seen = [], set()

    def push(indices):
        if indices in seen:
            return
        seen.add(indices)
        budget.check()
        rows = [component.get(index) for component, index in zip(components, indices)]
        if any(row is None for row in rows):
            return
        chance = prod(row['probability'] for row in rows)
        if chance <= 0:
            return
        sequence = [None] * model['size']
        for block, row in zip(model['blocks'], rows):
            for index, label in zip(block['indices'], row['sequence']):
                sequence[index] = label
        heappush(heap, (-chance, tuple(sequence), indices))

    push((0,) * len(components))
    while heap:
        negative, sequence, indices = heappop(heap)
        budget.check()
        yield _row(sequence, -negative)
        for position in range(len(components)):
            neighbor = list(indices)
            neighbor[position] += 1
            push(tuple(neighbor))


def _positive(model, budget):
    if model['type'] == 'block_product':
        yield from _blocks(model, budget)
    elif model['type'] == 'independent':
        yield from _products(model['distributions'], lambda seq: _probability(model, seq), budget)
    else:
        yield from _sparse(model, budget)


def _all(model, budget):
    positive = 0
    for row in _positive(model, budget):
        positive += 1
        yield row
    if positive < 3**model['size']:
        # A separate lexicographic stream is necessary for floating-point zero
        # ties: coordinate probability order need not be tuple order at zero.
        for sequence in product(sorted(VALUES), repeat=model['size']):
            budget.check()
            if _probability(model, sequence) == 0:
                yield _row(sequence, 0)


def page(method, offset=0, page_size=50, *, check_time=None,
         time_budget_seconds=DEFAULT_TIME_BUDGET_SECONDS):
    """Return one complete page or raise, without imposing a catalogue limit.

    Later pages are computed from the immutable inputs again. A very distant
    page can exceed the explicit CPU budget; it is never reported as an empty
    or complete catalogue. The total remains exactly 3**N in every valid page.
    """
    size = _size(method)
    total = 3**size
    if type(offset) is not int or not 0 <= offset <= total:
        raise CatalogError('La position de la page doit appartenir au catalogue.')
    if type(page_size) is not int or not 1 <= page_size <= MAX_PAGE_SIZE:
        raise CatalogError('Une page doit afficher de 1 à 1000 scénarios.')
    budget = _Budget(check_time, time_budget_seconds)
    model, _ = _model(method, budget)
    end = min(total, offset + page_size)
    ranking = []
    if offset < total:
        for index, row in enumerate(_all(model, budget)):
            budget.check()
            if index >= offset:
                ranking.append(row)
            if index + 1 == end:
                break
        if len(ranking) != end-offset:
            raise CatalogError('La distribution n’a pas produit toute la page demandée.')
    budget.check(force=True)
    return dict(version=VERSION, offset=offset, page_size=page_size,
                ranking=ranking, total=total, has_next=end < total)


def lookup(method, sequence, *, check_time=None,
           time_budget_seconds=DEFAULT_TIME_BUDGET_SECONDS):
    """Score one exact ordered outcome; never invent an uncomputed full rank."""
    budget = _Budget(check_time, time_budget_seconds)
    model, reference = _model(method, budget)
    sequence = frozen._sequence(sequence, model['size'])
    probability = _probability(model, sequence)
    rank = next((index for index, row in enumerate(reference, 1)
                 if tuple(row['sequence']) == sequence), None)
    budget.check(force=True)
    return dict(sequence=list(sequence), probability=probability, rank=rank,
                rank_status='reference' if rank is not None else 'not_computed',
                total=3**model['size'])
