"""Observed chronological groups across leagues, independent of storage coupons."""
from coupon_sizes import validate_size
from league_model import VALUES


def global_groups(rows, size):
    validate_size(size)
    unique = {}
    for row in rows:
        mid = str(row['match_id'])
        if row['value'] not in VALUES:
            raise ValueError('Résultat invalide')
        if mid in unique:
            prior = unique[mid]
            if any(prior[k] != row[k] for k in ('timestamp', 'league', 'value')):
                raise ValueError('Rencontre contradictoire')
            continue
        unique[mid] = row
    matches = sorted(unique.values(), key=lambda r: (r['timestamp'], int(r['match_id'])))
    complete = len(matches)//size
    groups = []
    for i in range(complete):
        block = matches[i*size:(i+1)*size]
        groups.append({'number':i+1, 'sequence':tuple(r['value'] for r in block),'matches':block})
    return {'size':size,'groups':groups,'remaining':matches[complete*size:],
            'matches':len(matches)}
