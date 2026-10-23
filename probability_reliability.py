"""Descriptive reliability of immutable, pre-kickoff Clubs probabilities.

This module is deliberately pure: it reads neither storage nor the clock and
never reconstructs a forecast from a current model. A fixture contributes once
per actual Clubs version, using its earliest eligible saved forecast, including
when that forecast still has no imported result.
"""
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from math import fsum, isclose, isfinite

from league_model import VALUES, outcome, score


VERSION = 'saved-clubs-reliability-v1'
SCHEMA = 'prospective-distribution-v1'


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError('Invalid timestamp')
    value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _id(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError('Invalid identifier')
    value = str(value)
    if not value.isascii() or not value.isdecimal() or int(value) <= 0:
        raise ValueError('Invalid identifier')
    return str(int(value))


def _probabilities(value):
    if not isinstance(value, dict) or set(value) != set(VALUES):
        raise ValueError('Missing probabilities')
    if any(isinstance(p, bool) or not isinstance(p, (int, float))
           or not isfinite(p) or not 0 <= p <= 1 for p in value.values()):
        raise ValueError('Invalid probability')
    if not isclose(fsum(value.values()), 1, rel_tol=1e-9, abs_tol=1e-9):
        raise ValueError('Probabilities do not sum to one')
    return {label: float(value[label]) for label in VALUES}


def _positions(method, fixtures, *, before=None):
    """Extract explicit independent marginals; no corrected-model shortcut."""
    if not isinstance(method, dict) or method.get('source') != 'API-Football':
        raise ValueError('Invalid method source')
    version = method.get('version')
    generation = method.get('generation')
    if not isinstance(version, str) or not version.strip() or (generation is not None and
            (not isinstance(generation, str) or not generation.strip())):
        raise ValueError('Missing method version')
    cutoff = method.get('training_before')
    if not isinstance(cutoff, str) or date.fromisoformat(cutoff).isoformat() != cutoff:
        raise ValueError('Invalid training cutoff')
    if before is not None and before != cutoff:
        raise ValueError('Mixed cutoffs disagree')
    if any(cutoff > _stamp(fixture['timestamp']).date().isoformat() for fixture in fixtures):
        raise ValueError('Noncausal training cutoff')
    distribution = method.get('distribution')
    if not isinstance(distribution, dict) or distribution.get('schema') != SCHEMA:
        raise ValueError('Unknown distribution schema')
    if type(distribution.get('size')) is not int or distribution['size'] != len(fixtures):
        raise ValueError('Distribution size mismatch')
    kind = distribution.get('type')
    if kind == 'independent':
        code = _id(method.get('league'))
        if any(_id(fixture['league']) != code for fixture in fixtures):
            raise ValueError('Wrong competition provenance')
        positions = distribution.get('distributions')
        if not isinstance(positions, list) or len(positions) != len(fixtures):
            raise ValueError('Missing explicit marginals')
        return [dict(version=version, generation=generation,
                     probabilities=_probabilities(p)) for p in positions]
    if kind != 'block_product' or before is not None:
        raise ValueError('No explicit independent Clubs marginals')
    if (method.get('league') != 'mixed' or method.get('provider') != 'api-football'
            or method.get('assumes_block_independence') is not True):
        raise ValueError('Invalid mixed provenance')
    saved = method.get('fixtures')
    fields = ('match_id', 'league', 'home_id', 'away_id', 'timestamp', 'provider')
    if (not isinstance(saved, list) or len(saved) != len(fixtures) or
            any(not isinstance(row, dict) or any(str(row.get(key)) != str(fixture.get(key))
                for key in fields) for row, fixture in zip(saved, fixtures))):
        raise ValueError('Mixed fixtures disagree')
    components = distribution.get('components')
    if not isinstance(components, list) or not components:
        raise ValueError('Missing mixed components')
    result = [None] * len(fixtures)
    for component in components:
        if not isinstance(component, dict):
            raise ValueError('Invalid mixed component')
        indices = component.get('indices')
        if (not isinstance(indices, list) or not indices or
                any(type(index) is not int or not 0 <= index < len(fixtures) for index in indices)
                or indices != sorted(set(indices)) or any(result[index] is not None for index in indices)):
            raise ValueError('Mixed positions overlap')
        code = _id(component.get('league'))
        if any(_id(fixtures[index]['league']) != code for index in indices):
            raise ValueError('Mixed component competition disagrees')
        values = _positions(component.get('method'), [fixtures[index] for index in indices], before=cutoff)
        for index, value in zip(indices, values):
            result[index] = value
    if any(value is None for value in result):
        raise ValueError('Mixed positions are incomplete')
    return result


def _result(entry, fixture):
    """Only the selected forecast's current imported observation is used."""
    if entry.get('status') == 'void':
        return dict(state='excluded', reason='void')
    if entry.get('status') not in ('settled', 'pending', 'unresolved'):
        return dict(state='excluded', reason='unknown_result_status')
    observed = entry.get('observed') or []
    if not isinstance(observed, list):
        return dict(state='excluded', reason='invalid_observed')
    matching = []
    for row in observed:
        try:
            if isinstance(row, dict) and _id(row.get('match_id')) == fixture['match_id']:
                matching.append(row)
        except ValueError:
            continue
    if not matching:
        return dict(state='excluded' if entry.get('status') in ('settled', 'unresolved') else 'pending',
                    reason='missing_or_unresolved_result' if entry.get('status') != 'pending' else None)
    try:
        if _stamp(entry.get('checked_at')) < _stamp(fixture['timestamp']):
            raise ValueError('Observation before kickoff')
        signatures = set()
        for row in matching:
            ht, ft = score(row.get('ht')), score(row.get('ft'))
            value = outcome(ht, ft)
            if row.get('value') != value:
                raise ValueError('Result label disagrees with scores')
            signatures.add((ht, ft, value))
        if len(signatures) != 1:
            raise ValueError('Conflicting results')
        return dict(state='evaluated', actual=next(iter(signatures))[2])
    except (ValueError, TypeError, AttributeError):
        return dict(state='excluded', reason='invalid_or_conflicting_result')


def _bins(records, label):
    bins = []
    for index in range(10):
        members = [row for row in records if min(9, int(row['probabilities'][label] * 10)) == index]
        evaluated = [row for row in members if row['state'] == 'evaluated']
        n = len(evaluated)
        hits = sum(row['actual'] == label for row in evaluated)
        announced = fsum(row['probabilities'][label] for row in evaluated) / n if n else None
        actual = hits / n if n else None
        bins.append(dict(range=f'{index * 10}–{(index + 1) * 10} %', lower=index / 10,
                         upper=(index + 1) / 10, n=n, hits=hits, announced=announced,
                         observed_rate=actual, gap_points=100 * (actual - announced) if n else None,
                         pending=sum(row['state'] == 'pending' for row in members),
                         excluded=sum(row['state'] == 'excluded' for row in members)))
    return bins


def build(entries):
    """Return one-versus-rest reliability bins, separated by Clubs version.

    Entries must be the locally read journal snapshots. Ranking, group success,
    filters and the selected user scenario never influence this population.
    """
    entries = list(entries)
    excluded, candidates = Counter(), []
    for entry in entries:
        try:
            if not isinstance(entry, dict) or entry.get('source') != 'API-Football':
                raise ValueError('Invalid source')
            fixtures = entry.get('fixtures')
            if not isinstance(fixtures, list) or not 2 <= len(fixtures) <= 20:
                raise ValueError('Invalid fixtures')
            created = _stamp(entry.get('created_at'))
            entry_id = entry.get('id')
            if not isinstance(entry_id, str) or not entry_id:
                raise ValueError('Missing journal identity')
            checked = []
            for fixture in fixtures:
                if not isinstance(fixture, dict) or fixture.get('provider') != 'api-football':
                    raise ValueError('Invalid fixture source')
                row = dict(fixture)
                for name in ('match_id', 'league', 'home_id', 'away_id'):
                    row[name] = _id(row.get(name))
                if row['home_id'] == row['away_id']:
                    raise ValueError('Identical clubs')
                _stamp(row['timestamp'])
                checked.append(row)
            if len({row['match_id'] for row in checked}) != len(checked):
                raise ValueError('Duplicate fixture in group')
            methods = entry.get('methods')
            if not isinstance(methods, dict) or 'clubs' not in methods:
                excluded['no_saved_clubs_method'] += 1
                continue
            positions = _positions(methods['clubs'], checked)
            for fixture, position in zip(checked, positions):
                if created >= _stamp(fixture['timestamp']):
                    excluded['not_before_kickoff'] += 1
                    continue
                key = position['version'] + (f" · génération {position['generation']}" if position['generation'] else '')
                candidates.append(dict(position, key=key, match_id=fixture['match_id'],
                    entry_id=entry_id, created_at=created.isoformat(), _created=created,
                    fixture=fixture, _entry=entry))
        except (ValueError, TypeError, AttributeError, KeyError, OverflowError):
            excluded['invalid_or_unsupported_snapshot'] += 1
    first = {}
    for row in sorted(candidates, key=lambda item: (item['_created'], item['entry_id'])):
        first.setdefault((row['key'], row['match_id']), row)
    grouped = defaultdict(list)
    for row in first.values():
        result = dict(row)
        result.update(_result(result.pop('_entry'), result['fixture']))
        result.pop('_created')
        grouped[row['key']].append(result)
    versions = []
    for key, records in sorted(grouped.items()):
        evaluated = [row for row in records if row['state'] == 'evaluated']
        results = {}
        for label in VALUES:
            nominated = [row for row in evaluated if max(VALUES, key=row['probabilities'].get) == label]
            results[label] = dict(bins=_bins(records, label),
                most_likely_n=len(nominated),
                most_likely_hits=sum(row['actual'] == label for row in nominated),
                most_likely_rate=sum(row['actual'] == label for row in nominated) / len(nominated) if nominated else None)
        versions.append(dict(key=key, version=records[0]['version'], generation=records[0]['generation'],
            forecasts=len(records), evaluated=len(evaluated),
            pending=sum(row['state'] == 'pending' for row in records),
            excluded=sum(row['state'] == 'excluded' for row in records),
            result_exclusions=dict(Counter(row['reason'] for row in records if row['state'] == 'excluded')),
            results=results, records=records))
    return dict(version=VERSION, entries_read=len(entries), versions=versions,
                duplicates_removed=len(candidates) - len(first), snapshot_exclusions=dict(excluded))
