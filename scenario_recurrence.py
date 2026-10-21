"""Describe repetitions relative to the observed frequencies at each position.

The reference keeps each position's empirical outcome frequencies and assumes
independent positions. It is descriptive, not a statistical significance test:
many patterns are inspected, and temporal dependence is not modelled. Neither
the historical ordering nor the dates of the input groups are changed.
"""
from collections import Counter, defaultdict
from math import prod

from coupon_sizes import validate_size
from league_model import VALUES


def recurrence_diagnostics(result):
    """Return observed patterns in the order of ``repeated_scenarios``' ranking.

    For N complete groups, the expected count of a sequence s is
    N * product(count_at_position[i][s[i]] / N). Frequencies are not smoothed,
    so a constant history has observed = expected = N and ratio = 1.
    Pending matches never enter the reference or the observed counts.
    """
    size = validate_size(result.get('size', 4))
    blocks = result['blocks']
    groups = len(blocks)
    if not groups:
        return []

    positions = [Counter() for _ in range(size)]
    observed = Counter()
    block_numbers = defaultdict(list)
    for block in blocks:
        sequence = tuple(block['sequence'])
        if len(sequence) != size or any(value not in VALUES for value in sequence):
            raise ValueError('Chaque groupe doit contenir le nombre prévu de résultats valides.')
        observed[sequence] += 1
        block_numbers[sequence].append(block['number'])
        for position, value in enumerate(sequence):
            positions[position][value] += 1

    # Follow the existing ranking, including its conventional ordering of ties.
    order = [tuple(row['sequence']) for row in result['ranking']]
    if len(order) != len(observed) or set(order) != set(observed):
        raise ValueError('Le classement doit contenir chaque scénario observé une seule fois.')

    diagnostics = []
    for sequence in order:
        probability = prod(positions[i][value] / groups
                           for i, value in enumerate(sequence))
        expected = groups * probability
        count = observed[sequence]
        diagnostics.append({
            'sequence': sequence,
            'observed': count,
            'groups': groups,
            'expected': expected,
            'ratio': count / expected,
            'excess': count - expected,
            'blocks': list(block_numbers[sequence]),
        })
    return diagnostics
