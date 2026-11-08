"""Exact probability-threshold selection from an existing frozen distribution.

The threshold is supplied by a separately qualified policy. This module does
not choose it, fit a model, reinterpret old proposals, or cap the number of
results. Outcomes below the threshold are never turned into proposals merely
to fill a fixed number of places. The full 3**N outcome space is not enumerated.
"""
from collections.abc import Mapping
from heapq import heappop, heappush, merge
from math import isclose, isfinite, prod

import prospective_models as frozen


VERSION = 'adaptive-scenario-threshold-v1'


def _threshold(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('Le seuil de sélection doit être une probabilité strictement positive, au plus égale à un.')
    try:
        chance = float(value)
    except (ValueError, OverflowError):
        raise ValueError('Le seuil de sélection est invalide.') from None
    if not isfinite(chance) or not 0 < chance <= 1:
        raise ValueError('Le seuil de sélection doit être une probabilité strictement positive, au plus égale à un.')
    return chance


class _Budget:
    def __init__(self, callback):
        if callback is not None and not callable(callback):
            raise ValueError('Le contrôle du temps doit être une fonction.')
        self.callback = callback
        self.operations = 0

    def check(self, *, force=False):
        self.operations += 1
        if self.callback is not None and (force or self.operations % 64 == 0):
            self.callback()


def _model(method, budget):
    """Validate the original canonical V1 snapshot, parsing it only once.

    This is the same version/ranking check as the frozen V1 validator. Its
    mathematical distribution parser also validates nested component payloads.
    An adaptive output is a new proposal set, not a replacement V1 ranking.
    """
    budget.check(force=True)
    if (not isinstance(method, Mapping) or not isinstance(method.get('version'), str)
            or not method['version']):
        raise ValueError('La version de la méthode figée doit être conservée.')
    model = frozen._read_distribution(method.get('distribution'))
    ranking = method.get('ranking')
    if not isinstance(ranking, list) or len(ranking) != 3:
        raise ValueError('La méthode source doit conserver son classement canonique avant la sélection adaptative.')
    expected = frozen._rank(model, 3)
    for saved, actual in zip(ranking, expected):
        if (not isinstance(saved, Mapping)
                or frozen._sequence(saved.get('sequence'), model['size']) != tuple(actual['sequence'])
                or not isclose(frozen._number(saved.get('probability'), positive=True),
                               actual['probability'], rel_tol=1e-9, abs_tol=0)):
            raise ValueError('Le classement source ne correspond pas à la distribution figée.')
    budget.check(force=True)
    return model


def _row(sequence, chance):
    return dict(sequence=list(sequence), probability=float(chance))


def _key(row):
    return -row['probability'], tuple(row['sequence'])


def _products(positions, probability_for, threshold, budget):
    """Yield every qualifying product in exact probability/sequence order.

    Increasing one coordinate cannot increase its independent product. Heap
    traversal therefore discovers the next highest product without enumerating
    the Cartesian space. The supplied probability function is its monotone
    scaling and uses the frozen model's arithmetic, including at boundaries.
    """
    choices = [sorted(position, key=lambda label: (-position[label], label)) for position in positions]
    seen, heap = set(), []

    def push(indices):
        if indices in seen:
            return
        seen.add(indices)
        budget.check()
        sequence = tuple(choices[index][choice] for index, choice in enumerate(indices))
        chance = probability_for(sequence)
        if chance >= threshold:
            heappush(heap, (-chance, sequence, indices))

    push((0,) * len(positions))
    while heap:
        negative, sequence, indices = heappop(heap)
        yield _row(sequence, -negative)
        for position, choice in enumerate(indices):
            if choice + 1 < len(choices[position]):
                next_indices = list(indices)
                next_indices[position] += 1
                push(tuple(next_indices))


def _sparse(model, threshold, budget):
    """Merge sparse exceptions with the scaled independent remainder."""
    kind = model['type']
    positions = model['fit'][2] if kind == 'frequency' else model['distributions']
    exceptions = model['local'] if kind == 'context_mixture' else model['fit'][0]

    def remainder_probability(sequence):
        value = prod(position[label] for position, label in zip(positions, sequence))
        if kind == 'frequency':
            return (model['weight'] * value) / (model['fit'][1] + model['weight'])
        if kind == 'corrected':
            return value * (model['unseen'] / model['normalization'])
        return (1 - model['eta']) * value

    exceptional = []
    for sequence in exceptions:
        budget.check()
        chance = frozen._probability(model, sequence)
        if chance >= threshold:
            exceptional.append(_row(sequence, chance))
    exceptional.sort(key=_key)
    ordinary = (row for row in _products(positions, remainder_probability, threshold, budget)
                if tuple(row['sequence']) not in exceptions)
    yield from merge(exceptional, ordinary, key=_key)


class _LazyChoices:
    def __init__(self, stream):
        self.stream = iter(stream)
        self.values = []
        self.finished = False

    def get(self, index):
        while len(self.values) <= index and not self.finished:
            try:
                self.values.append(next(self.stream))
            except StopIteration:
                self.finished = True
        return self.values[index] if index < len(self.values) else None


def _blocks(model, threshold, budget):
    """Heap-merge lazy component rankings; never multiply whole candidate sets."""
    components = [_LazyChoices(_selected(block['model'], threshold, budget)) for block in model['blocks']]
    if any(component.get(0) is None for component in components):
        return
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
        if chance < threshold:
            return
        sequence = [None] * model['size']
        for block, row in zip(model['blocks'], rows):
            for index, label in zip(block['indices'], row['sequence']):
                sequence[index] = label
        heappush(heap, (-chance, tuple(sequence), indices))

    push((0,) * len(components))
    while heap:
        negative, sequence, indices = heappop(heap)
        yield _row(sequence, -negative)
        for position in range(len(components)):
            next_indices = list(indices)
            next_indices[position] += 1
            push(tuple(next_indices))


def _selected(model, threshold, budget):
    if model['type'] == 'block_product':
        yield from _blocks(model, threshold, budget)
    elif model['type'] == 'independent':
        positions = model['distributions']
        yield from _products(positions, lambda sequence: frozen._probability(model, sequence), threshold, budget)
    else:
        yield from _sparse(model, threshold, budget)


def iter_selected(method, threshold, *, check_time=None):
    """Yield all explicit selections with probability >= the supplied threshold.

    Ties are ordered by the complete sequence. Time/resource interruption raises
    to the caller: a consumed prefix is never evidence of a complete set. No
    threshold calibration or predictive-quality claim is made by this function.
    """
    threshold = _threshold(threshold)
    budget = _Budget(check_time)
    model = _model(method, budget)
    yield from _selected(model, threshold, budget)
    budget.check(force=True)


def select(method, threshold, *, check_time=None):
    """Return the complete set or raise; never return an interrupted prefix."""
    return list(iter_selected(method, threshold, check_time=check_time))
