"""Competition analysis of verified, active Forebet imports (no database writes)."""
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime
from coupon_sizes import top_independent
from pathlib import Path

VALUES = ('1st', 'Égal', '2nd')
DATA_DIR = Path(__file__).resolve().parent / 'data' / 'forebet'
PRIOR_WEIGHT = 30
MIN_MATCHES = 30
MIN_DAYS = 3


def score(text):
    parts = re.fullmatch(r'\s*\(?\s*(\d+)\s*-\s*(\d+)\s*\)?\s*', text or '')
    if not parts:
        raise ValueError('Score HT/FT absent ou invalide')
    return tuple(map(int, parts.groups()))


def outcome(ht, ft):
    if any(a > b for a, b in zip(ht, ft)):
        raise ValueError('HT supérieur au FT')
    first, second = sum(ht), sum(ft) - sum(ht)
    return '1st' if first > second else '2nd' if second > first else 'Égal'


def load_matches(coupons, directory=DATA_DIR):
    """Join immutable import provenance with current coupons; reject changed scores."""
    active = {c['id']: c for c in coupons}
    details = defaultdict(list)
    for path in sorted(directory.glob('*_details.json')):
        for row in json.loads(path.read_text())['matches']:
            if row.get('id'):
                details[str(row['id'])].append(row)
    rows, issues, seen = [], [], set()
    expected = 0
    for path in sorted(directory.glob('*_import.json')):
        report = json.loads(path.read_text())
        for coupon in report['imported']:
            current = active.get(coupon['coupon_id'])
            ids = [str(m['match_id']) for m in coupon['matches']]
            # An id can be reused after clearing the database: require provenance too.
            if not current or f"match_ids={','.join(ids)}" not in current.get('note', ''):
                continue
            for pos, original in enumerate(coupon['matches']):
                expected += 1
                mid = str(original['match_id'])
                try:
                    if mid in seen:
                        raise ValueError('Identifiant déjà utilisé')
                    candidates = [d for d in details.get(mid, []) if d.get('ft') and d.get('ht')]
                    if not candidates:
                        raise ValueError('Détails manquants')
                    signatures = set()
                    for detail in candidates:
                        ht, ft = score(detail.get('ht')), score(detail.get('ft'))
                        if ht != score(original['ht']) or ft != score(original['ft']):
                            raise ValueError('Scores différents du relevé importé')
                        fields = re.findall(r"'((?:\\.|[^'\\])*)'", detail.get('league_info') or '')
                        if len(fields) != 4:
                            raise ValueError('Compétition non reconnue')
                        country, competition, key, _ = fields
                        stamp = datetime.strptime(detail['date'], '%d/%m/%Y %H:%M')
                        signatures.add((country, competition, key, stamp.isoformat(), detail['home'], detail['away']))
                    if len(signatures) != 1:
                        raise ValueError('Métadonnées contradictoires')
                    country, competition, key, timestamp, home, away = signatures.pop()
                    value = outcome(ht, ft)
                    if value != original['value'] or value != current['sequence'][pos]:
                        raise ValueError('Étiquette différente du coupon')
                    rows.append(dict(match_id=mid, coupon_id=current['id'], position=pos + 1,
                                     country=country, competition=competition, league=key,
                                     timestamp=timestamp, day=timestamp[:10], home=home, away=away,
                                     ht=original['ht'], ft=original['ft'], value=value,
                                     source=original.get('original_source', report['source'])))
                    seen.add(mid)
                except (ValueError, KeyError, IndexError) as error:
                    issues.append(dict(match_id=mid, reason=str(error)))
    rows.sort(key=lambda r: (r['timestamp'], r['match_id']))
    return rows, dict(expected=expected, matched=len(rows), issues=issues)


def probabilities(rows, league=None):
    """Laplace global prior; shrink competition estimates, fallback for sparse data."""
    counts = Counter(r['value'] for r in rows)
    global_p = {v: (counts[v] + 1) / (len(rows) + 3) for v in VALUES}
    subset = [r for r in rows if r['league'] == league]
    days = len({r['day'] for r in subset})
    eligible = league is not None and len(subset) >= MIN_MATCHES and days >= MIN_DAYS
    local = Counter(r['value'] for r in subset)
    p = {v: (local[v] + PRIOR_WEIGHT * global_p[v]) / (len(subset) + PRIOR_WEIGHT)
         for v in VALUES} if eligible else global_p
    return dict(probabilities=p, matches=len(subset), days=days,
                method='Compétition lissée' if eligible else 'Référence globale')


def chronological_evaluation(rows):
    """Expanding window: all results on target day excluded from training."""
    days = sorted({r['day'] for r in rows})
    records = []
    for day in days[1:]:
        train = [r for r in rows if r['day'] < day]
        if len(train) < 100:
            continue
        test = [r for r in rows if r['day'] == day]
        global_result = probabilities(train)
        cache = {}
        for row in test:
            if row['league'] not in cache:
                cache[row['league']] = probabilities(train, row['league'])
            local = cache[row['league']]
            for name, estimate in (('Global', global_result), ('Par compétition', local)):
                p = estimate['probabilities']
                records.append(dict(day=day, league=row['league'], model=name,
                                    correct=int(max(VALUES, key=p.get) == row['value']),
                                    loss=-math.log(p[row['value']]),
                                    specialized=int(estimate['method'] == 'Compétition lissée')))
    return records


def evaluation_summary(records):
    output = []
    for name in ('Global', 'Par compétition'):
        group = [r for r in records if r['model'] == name]
        if group:
            output.append({'Modèle': name, 'Matchs testés': len(group),
                           'Réussite (%)': round(100 * sum(r['correct'] for r in group) / len(group), 2),
                           'Perte logarithmique': round(sum(r['loss'] for r in group) / len(group), 4),
                           'Estimations spécialisées': sum(r['specialized'] for r in group)})
    return output


def league_summary(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[row['league']].append(row)
    result = []
    for key, group in groups.items():
        counts = Counter(r['value'] for r in group)
        maximum = max(counts.values())
        result.append({'Clé': key, 'Pays': group[0]['country'], 'Compétition': group[0]['competition'],
                       'Matchs': len(group), 'Jours': len({r['day'] for r in group}),
                       **{v + ' (%)': round(100 * counts[v] / len(group), 2) for v in VALUES},
                       'Maximum observé': ' / '.join(v for v in VALUES if counts[v] == maximum)})
    return sorted(result, key=lambda r: (-r['Matchs'], r['Pays'], r['Compétition']))


def coupon_scenarios(estimates):
    return [{'Scénario': ' → '.join(row['sequence']),
             'Probabilité indicative (%)': 100 * row['probability']}
            for row in top_independent([e['probabilities'] for e in estimates])]
