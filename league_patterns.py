"""Count actual non-overlapping four-match sequences separately per competition."""
from collections import Counter, defaultdict

from league_model import VALUES
from coupon_sizes import validate_size


def repeated_scenarios(rows, size=4):
    validate_size(size)
    leagues = defaultdict(list)
    seen = {}
    for row in rows:
        mid = str(row['match_id'])
        signature = (row['league'], row['timestamp'], row['value'])
        if mid in seen:
            if seen[mid] != signature:
                raise ValueError(f'Match contradictoire : {mid}')
            continue
        if row['value'] not in VALUES:
            raise ValueError(f'Résultat invalide : {mid}')
        seen[mid] = signature
        leagues[row['league']].append(row)
    output = {}
    for league, matches in leagues.items():
        # Numeric provider id resolves simultaneous kickoffs reproducibly, never by result.
        matches.sort(key=lambda r: (r['timestamp'], int(r['match_id'])))
        complete = len(matches) // size
        blocks = []
        for i in range(complete):
            block = matches[i * size:(i + 1) * size]
            blocks.append({'number': i + 1, 'sequence': tuple(r['value'] for r in block),
                           'matches': block})
        counts = Counter(b['sequence'] for b in blocks)
        ranking = []
        previous_count, rank = None, 0
        for index, (sequence, count) in enumerate(sorted(counts.items(), key=lambda item: (-item[1], item[0])), 1):
            if count != previous_count:
                rank = index
                previous_count = count
            ranking.append({'rank': rank, 'sequence': sequence, 'count': count,
                            'blocks': [b['number'] for b in blocks if b['sequence'] == sequence]})
        output[league] = {'size': size, 'matches': matches, 'blocks': blocks, 'ranking': ranking,
                          'pending': matches[complete * size:],
                          'simultaneous': any(n > 1 for n in Counter(r['timestamp'] for r in matches).values())}
    return output


def scenario_completions(result, prefix):
    """Exact prefix matches at the start of complete historical groups only."""
    size = result.get("size", 4)
    prefix = tuple(prefix)
    if not 1 <= len(prefix) < size or any(v not in VALUES for v in prefix):
        raise ValueError(f'Saisis de 1 à {size-1} résultats valides dans l’ordre du groupe.')
    matches = [b for b in result['blocks'] if b['sequence'][:len(prefix)] == prefix]
    counts = Counter(b['sequence'][len(prefix):] for b in matches)
    ranking = []
    previous, rank = None, 0
    for index, (suffix, count) in enumerate(sorted(counts.items(), key=lambda x: (-x[1], x[0])), 1):
        if count != previous:
            rank, previous = index, count
        ranking.append({'rank': rank, 'suffix': suffix, 'count': count,
                        'blocks': [b['number'] for b in matches if b['sequence'][len(prefix):] == suffix]})
    return {'prefix': prefix, 'support': len(matches), 'total': len(result['blocks']), 'ranking': ranking}
