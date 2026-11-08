"""Causal club histories for an isolated order-versus-bag experiment.

No application, network, storage or training imports. A whole UTC day is emitted
before any result from that day updates the history. Bag and ordered views use
the exact same ten vectors, with no per-vector date or match identifier.
"""
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from itertools import groupby
from math import comb, fsum, isfinite, sqrt


FEATURE_VERSION = 'club-trajectories-order-bag-v1'
LABELS = ('1st', 'Égal', '2nd')
WINDOW = 10
PROFILES = ('home_overall', 'home_role', 'away_overall', 'away_role')
CHANNELS = ('ht_for', 'ht_against', 'sh_for', 'sh_against', 'total',
            'allocation_residual', 'variance_residual', 'equal_residual',
            'is_first', 'is_equal', 'is_second')
CATEGORICAL_COLUMNS = ('league', 'home_id', 'away_id')
CONTEXT_NUMERIC = ('season', 'month', 'weekday', 'hour_utc', 'league_count',
                   'league_first_share', 'league_rate_first', 'league_rate_equal',
                   'league_rate_second') + tuple(
    f'{p}_{field}' for p in PROFILES for field in ('count', 'days_rest', 'window_count'))
SUMMARY_COLUMNS = tuple(f'{p}_{c}_{stat}' for p in PROFILES for c in CHANNELS
                        for stat in ('mean', 'variance', 'minimum', 'maximum'))
VECTOR_COLUMNS = tuple(f'{p}_slot{slot}_{c}' for p in PROFILES for slot in range(1, WINDOW+1)
                       for c in ('present',)+CHANNELS)


def columns(mode):
    """Return stable (numeric, categorical) columns for one representation."""
    if mode not in ('summary', 'bag', 'ordered'):
        raise ValueError('Unknown research representation.')
    numeric = CONTEXT_NUMERIC + SUMMARY_COLUMNS
    if mode != 'summary':
        numeric += VECTOR_COLUMNS
    return numeric, CATEGORICAL_COLUMNS


def _id(value):
    if isinstance(value, bool) or not str(value).isdigit() or int(value) <= 0:
        raise ValueError('A positive provider identifier is required.')
    return str(int(value))


def _score(value):
    if not isinstance(value, str) or len(value.split('-')) != 2:
        raise ValueError('Invalid regulation score.')
    parts = value.split('-')
    if any(not p.isdigit() for p in parts):
        raise ValueError('Invalid regulation score.')
    return tuple(map(int, parts))


def _prepare(rows):
    prepared, seen = [], {}
    for row in rows:
        if row.get('provider') != 'api-football':
            raise ValueError('Only API-Football is allowed in this experiment.')
        mid, league, home, away = (_id(row.get(k)) for k in ('match_id', 'league', 'home_id', 'away_id'))
        if home == away:
            raise ValueError('The two clubs must have different identifiers.')
        try:
            stamp = datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00'))
            stamp = (stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp).astimezone(timezone.utc)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ValueError('Invalid kickoff.') from None
        ht, ft = _score(row.get('ht')), _score(row.get('ft'))
        if any(h > f for h, f in zip(ht, ft)):
            raise ValueError('HT cannot exceed regulation FT.')
        first, total = sum(ht), sum(ft)
        actual = '1st' if 2*first > total else 'Égal' if 2*first == total else '2nd'
        if row.get('value') != actual:
            raise ValueError('The label contradicts the scores.')
        season = row.get('season', stamp.year)
        if type(season) is not int or not 1900 <= season <= 2100:
            raise ValueError('Invalid season.')
        item = (stamp, mid, league, home, away, season, ht, ft, actual)
        if mid in seen:
            if seen[mid] != item:
                raise ValueError('Conflicting provider match identifier.')
            continue
        seen[mid] = item
        prepared.append(item)
    prepared.sort(key=lambda r: (r[0], int(r[1])))
    return prepared


def _vector(ht, ft, venue, share):
    first, total = sum(ht), sum(ft)
    second = total-first
    variance = total*share*(1-share)
    delta = first-total*share
    expected_equal = 0.
    # The equality reference concerns K=N/2, including when K was different.
    if total % 2 == 0:
        k = total//2
        expected_equal = comb(total, k)*share**k*(1-share)**(total-k)
    idx = 0 if venue == 'home' else 1
    return (float(ht[idx]), float(ht[1-idx]), float(ft[idx]-ht[idx]), float(ft[1-idx]-ht[1-idx]),
            float(total), delta/sqrt(max(variance, .25)),
            (delta*delta-variance)/max(variance, .25),
            float(first == second)-expected_equal,
            float(first > second), float(first == second), float(first < second))


def iter_examples(rows):
    """Yield independent transient dictionaries; do not materialize all views.

    Each historical residual uses the league's smoothed first-goal share before
    that historical UTC day. Final totals enter these *past* vectors only.
    """
    prepared = _prepare(rows)
    histories = defaultdict(lambda: deque(maxlen=WINDOW))
    counts, latest = Counter(), {}
    leagues = defaultdict(lambda: dict(count=0, first=0, total=0, labels=Counter()))
    for day, group in groupby(prepared, key=lambda r: r[0].date()):
        matches = list(group)
        pending = []
        for stamp, mid, league, home, away, season, ht, ft, actual in matches:
            state = leagues[league]
            share = (state['first']+1)/(state['total']+2)
            keys = ((league, home, 'all'), (league, home, 'home'),
                    (league, away, 'all'), (league, away, 'away'))
            context = dict(league=league, home_id=home, away_id=away,
                           season=season, month=stamp.month, weekday=stamp.weekday(), hour_utc=stamp.hour,
                           league_count=state['count'], league_first_share=share,
                           **{f'league_rate_{tag}': (state['labels'][v]+1)/(state['count']+3)
                              for v, tag in zip(LABELS, ('first', 'equal', 'second'))})
            summary, bag, ordered, history_ids = {}, {}, {}, {}
            for prefix, key in zip(PROFILES, keys):
                records = list(histories[key])
                values = [r[1] for r in records]
                context.update({f'{prefix}_count': counts[key],
                                f'{prefix}_days_rest': (day-latest[key]).days if key in latest else -1,
                                f'{prefix}_window_count': len(records)})
                for index, channel in enumerate(CHANNELS):
                    vals = [v[index] for v in values]
                    mean = fsum(vals)/len(vals) if vals else 0.
                    stats = (mean, fsum((v-mean)**2 for v in vals)/len(vals) if vals else 0.,
                             min(vals, default=0.), max(vals, default=0.))
                    for stat, value in zip(('mean', 'variance', 'minimum', 'maximum'), stats):
                        summary[f'{prefix}_{channel}_{stat}'] = value
                for result, vectors in ((ordered, list(reversed(values))), (bag, sorted(values))):
                    for slot in range(1, WINDOW+1):
                        exists = slot <= len(vectors)
                        vector = vectors[slot-1] if exists else (0.,)*len(CHANNELS)
                        result[f'{prefix}_slot{slot}_present'] = float(exists)
                        for channel, value in zip(CHANNELS, vector):
                            result[f'{prefix}_slot{slot}_{channel}'] = value
                history_ids[prefix] = [r[0] for r in records]
            assert all(isfinite(float(v)) for obj in (summary, bag, ordered) for v in obj.values())
            yield dict(match_id=mid, day=day.isoformat(), league=league, home_id=home, away_id=away,
                       actual=actual, context=context, summary=summary, bag=bag, ordered=ordered,
                       history_ids=history_ids)
            pending.append((league, home, away, ht, ft, actual, mid, share))
        # No target from this UTC day contributes to another target's features.
        for league, home, away, ht, ft, actual, mid, share in pending:
            state = leagues[league]
            state['count'] += 1
            state['first'] += sum(ht)
            state['total'] += sum(ft)
            state['labels'][actual] += 1
            for team, venue in ((home, 'home'), (away, 'away')):
                vector = _vector(ht, ft, venue, share)
                for role in ('all', venue):
                    key = (league, team, role)
                    histories[key].append((mid, vector))
                    counts[key] += 1
                    latest[key] = day


def build_examples(rows):
    """Small-data convenience wrapper; use iter_examples for the real dataset."""
    return list(iter_examples(rows))
