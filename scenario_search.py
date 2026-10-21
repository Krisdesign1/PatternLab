"""Search observed scenarios without limiting the ranking to its first 50 rows."""
import re
from collections import Counter
from analytics import normalize_value


def search_scenarios(counts, query, exact=True):
    tokens = [t for t in re.split(r'[\s,;|>→/\-]+', query.strip()) if t]
    if not tokens:
        raise ValueError('Saisis au moins un résultat : 1st, Égal ou 2nd.')
    sequence = tuple(normalize_value(t) for t in tokens)
    if len(sequence) > 20:
        raise ValueError('La recherche accepte au maximum 20 résultats.')
    matches = Counter()
    for candidate, count in counts.items():
        found = candidate == sequence if exact else any(
            candidate[i:i+len(sequence)] == sequence
            for i in range(len(candidate)-len(sequence)+1))
        if found:
            matches[candidate] = count
    return sequence, matches
