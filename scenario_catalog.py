"""Bounded pages of observed and unobserved combinations, including size 20."""
from analytics import VALID

def catalog_page(counts, size, category, page=1, page_size=50):
    if not 2 <= size <= 20 or not isinstance(page_size, int) or not 1 <= page_size <= 5000:
        raise ValueError('Réglages de pagination invalides')
    observed = sorted(((s, n) for s, n in counts.items() if n > 0), key=lambda x: -x[1])
    def encode(seq):
        value = 0
        for item in seq:
            value = value * 3 + VALID.index(item)
        return value
    excluded = sorted(encode(s) for s, _ in observed)
    missing = 3**size - len(observed)
    selected = {'Observées': observed, 'Une seule fois': [(s,n) for s,n in observed if n == 1],
                'Répétées': [(s,n) for s,n in observed if n >= 2],
                'Jamais observées (0)': [], 'Toutes les combinaisons': observed}[category]
    include_zero = category in ('Jamais observées (0)', 'Toutes les combinaisons')
    total = len(selected) + (missing if include_zero else 0)
    pages = max(1, (total + page_size - 1)//page_size)
    page = max(1, min(int(page), pages))
    start = (page-1)*page_size
    result = []
    denominator = sum(counts.values())
    for index in range(start, min(start+page_size, total)):
        if index < len(selected):
            seq, count = selected[index]
        else:
            value = index-len(selected)
            for occupied in excluded:
                if occupied > value:
                    break
                value += 1
            digits = []
            for _ in range(size):
                digits.append(VALID[value % 3])
                value //= 3
            seq, count = tuple(reversed(digits)), 0
        result.append({'scénario': ' → '.join(seq), 'occurrences': count,
                       'part_des_occurrences_%': round(100*count/denominator, 2) if denominator else 0.0})
    return result, total, pages, page
