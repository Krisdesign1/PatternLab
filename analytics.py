from collections import Counter, defaultdict
from math import sqrt

VALID = ("1st", "Égal", "2nd")

def normalize_value(value: str) -> str:
    v = str(value).strip().lower()
    aliases = {
        "1st": "1st", "1": "1st", "first": "1st", "1er": "1st",
        "égal": "Égal", "egal": "Égal", "draw": "Égal", "x": "Égal", "=": "Égal",
        "2nd": "2nd", "2": "2nd", "second": "2nd", "2e": "2nd",
    }
    if v not in aliases:
        raise ValueError(f"Valeur inconnue: {value}")
    return aliases[v]

def normalize_sequence(seq):
    return tuple(normalize_value(x) for x in seq)

def window_coupons(coupons, limit=None):
    if limit in (None, 0, "Tous"):
        return list(coupons)
    return list(coupons)[-int(limit):]

def complete_patterns(coupons, length=None):
    """
    Compte les coupons complets exacts.
    Si length est fourni, seuls les coupons de cette longueur sont pris en compte.
    """
    c = Counter()
    for seq in coupons:
        seq = normalize_sequence(seq)
        if length is None or len(seq) == int(length):
            c[seq] += 1
    return c

def subsequence_patterns(coupons, length):
    """
    Compte toutes les sous-séquences CONTIGUËS d'une longueur donnée.
    Un motif ne traverse jamais la frontière entre deux coupons.
    """
    n = int(length)
    c = Counter()
    for seq in coupons:
        seq = normalize_sequence(seq)
        if len(seq) < n:
            continue
        for i in range(len(seq) - n + 1):
            c[seq[i:i+n]] += 1
    return c

def top_patterns(counter, top_n=20):
    total = sum(counter.values())
    rows = []
    for seq, count in counter.most_common(top_n):
        rows.append({
            "scénario": " → ".join(seq),
            "occurrences": count,
            "part_des_occurrences_%": round((count / total * 100), 2) if total else 0.0,
        })
    return rows

def next_value_distribution(coupons, context):
    """
    Cherche le contexte comme sous-séquence contiguë dans chaque coupon
    et compte la valeur qui suit immédiatement.
    """
    context = normalize_sequence(context)
    if not context:
        return Counter(), 0
    out = Counter()
    occurrences = 0
    k = len(context)
    for seq in coupons:
        seq = normalize_sequence(seq)
        if len(seq) <= k:
            continue
        for i in range(len(seq) - k):
            if seq[i:i+k] == context:
                nxt = seq[i+k]
                out[nxt] += 1
                occurrences += 1
    return out, occurrences

def distribution_rows(counter):
    total = sum(counter.values())
    rows = []
    for key in VALID:
        count = counter.get(key, 0)
        rows.append({
            "suite": key,
            "occurrences": count,
            "fréquence_%": round(count / total * 100, 2) if total else 0.0
        })
    return rows

def position_distribution(coupons):
    by_pos = defaultdict(Counter)
    for seq in coupons:
        seq = normalize_sequence(seq)
        for i, value in enumerate(seq, start=1):
            by_pos[i][value] += 1
    rows = []
    for pos in sorted(by_pos):
        total = sum(by_pos[pos].values())
        row = {"position": pos, "n": total}
        for value in VALID:
            row[value] = by_pos[pos].get(value, 0)
            row[f"{value}_%"] = round(by_pos[pos].get(value, 0) / total * 100, 2) if total else 0.0
        rows.append(row)
    return rows

def transition_matrix(coupons):
    matrix = {a: Counter() for a in VALID}
    for seq in coupons:
        seq = normalize_sequence(seq)
        for a, b in zip(seq, seq[1:]):
            matrix[a][b] += 1
    rows = []
    for a in VALID:
        total = sum(matrix[a].values())
        row = {"après": a, "n": total}
        for b in VALID:
            row[b] = round(matrix[a][b] / total * 100, 2) if total else 0.0
        rows.append(row)
    return rows

def recent_vs_global(coupons, length, recent_n=50, top_n=20, complete=False):
    counter = complete_patterns if complete else subsequence_patterns
    global_counts = counter(coupons, length)
    recent_counts = counter(coupons[-recent_n:], length)
    keys = set(global_counts) | set(recent_counts)

    global_total = sum(global_counts.values())
    recent_total = sum(recent_counts.values())
    rows = []
    for key in keys:
        g = global_counts.get(key, 0)
        r = recent_counts.get(key, 0)
        gp = g / global_total * 100 if global_total else 0.0
        rp = r / recent_total * 100 if recent_total else 0.0
        rows.append({
            "scénario": " → ".join(key),
            "historique_%": round(gp, 2),
            f"{recent_n}_derniers_%": round(rp, 2),
            "écart_points": round(rp - gp, 2),
            "occurrences_récentes": r,
            "occurrences_historiques": g,
        })
    rows.sort(key=lambda x: (x["écart_points"], x["occurrences_récentes"]), reverse=True)
    return rows[:top_n]

def wilson_interval(successes, total, z=1.96):
    """Intervalle de Wilson indicatif pour une proportion historique."""
    if total <= 0:
        return (0.0, 0.0)
    p = successes / total
    den = 1 + z*z/total
    center = (p + z*z/(2*total)) / den
    margin = z * sqrt((p*(1-p) + z*z/(4*total))/total) / den
    return max(0.0, center-margin), min(1.0, center+margin)
