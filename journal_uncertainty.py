"""Read-only, paired block bootstrap for a dated prospective journal protocol.

Intervals are descriptive: neither repeated looks nor multiple comparisons are
controlled. UTC weeks and shared fixtures define dependent components, not
independent matches. No provider, model or persistent store is accessed here.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from math import fsum, isclose, isfinite
import random

from journal_protocol import validate_protocol


def _stamp(value, *, naive_utc=False):
    if not isinstance(value, str):
        raise ValueError('Horodatage du groupe absent ou invalide.')
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            if not naive_utc:
                raise ValueError()
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        raise ValueError('Horodatage du groupe absent ou invalide.') from None


def _week(stamp):
    day = stamp.date()
    return day-timedelta(days=day.weekday())


def _metadata(entry, created):
    fixtures = entry['fixtures']
    if not isinstance(fixtures, list) or len(fixtures) != entry['size']:
        raise ValueError('Le groupe ne contient pas les affiches attendues.')
    ids, stamps = set(), []
    for fixture in fixtures:
        identity = fixture['match_id']
        if isinstance(identity, bool) or not str(identity).isdigit() or int(identity) <= 0:
            raise ValueError('Identifiant de match absent ou invalide.')
        identity = str(int(identity))
        if identity in ids:
            raise ValueError('Un match ne peut apparaître deux fois dans un groupe.')
        ids.add(identity)
        stamps.append(_stamp(fixture['timestamp'], naive_utc=True))
    if created >= min(stamps):
        raise ValueError('Une prévision doit être enregistrée avant le premier coup d’envoi.')
    start, end = _week(min(stamps)), _week(max(stamps))
    weeks = []
    while start <= end:
        weeks.append(start)
        start += timedelta(days=7)
    return dict(entry=entry, ids=ids, weeks=weeks)


def _components(records):
    """Build dependencies before removing incomplete or open-week groups."""
    parents = list(range(len(records)))
    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index
    owners = {}
    for index, record in enumerate(records):
        keys = [('week', week) for week in record['weeks']]+[('fixture', mid) for mid in sorted(record['ids'])]
        for key in keys:
            if key in owners:
                first, second = find(index), find(owners[key])
                if first != second:
                    parents[max(first, second)] = min(first, second)
            else:
                owners[key] = index
    grouped = defaultdict(list)
    for index, record in enumerate(records):
        grouped[find(index)].append(record)
    return [grouped[key] for key in sorted(grouped)]


def _scores(entry, baseline, method):
    if entry.get('status') != 'settled':
        return None
    evaluations = entry.get('evaluations')
    if not isinstance(evaluations, dict):
        return None
    values = []
    for name in (baseline, method):
        evaluation = evaluations.get(name)
        if not isinstance(evaluation, dict) or type(evaluation.get('top3')) is not bool:
            return None
        loss = evaluation.get('loss')
        if isinstance(loss, bool) or not isinstance(loss, (int, float)) or not isfinite(loss) or loss < 0:
            return None
        values.append((float(loss), int(evaluation['top3'])))
    return values[0][0]-values[1][0], values[1][1]-values[0][1]


def _percentile(ordered, probability):
    position = (len(ordered)-1)*probability
    low = int(position)
    high = min(low+1, len(ordered)-1)
    return ordered[low]+(ordered[high]-ordered[low])*(position-low)


def _bootstrap(blocks, protocol):
    """Identical draws for both paired gains; ratios retain group weights."""
    generator = random.Random(protocol['seed'])
    loss_draws, hit_draws = [], []
    for _ in range(protocol['bootstrap_samples']):
        chosen = generator.choices(blocks, k=len(blocks))
        count = sum(block[0] for block in chosen)
        loss_draws.append(fsum(block[1] for block in chosen)/count)
        hit_draws.append(fsum(block[2] for block in chosen)/count)
    tail = (1-protocol['confidence'])/2
    results = []
    for values, index in ((loss_draws, 1), (hit_draws, 2)):
        means = [block[index]/block[0] for block in blocks]
        ordered = sorted(values)
        lower, upper = _percentile(ordered, tail), _percentile(ordered, 1-tail)
        # No observed variation is not evidence of exact certainty/equivalence.
        variable = not isclose(min(means), max(means), rel_tol=1e-12, abs_tol=1e-12)
        spread = not isclose(lower, upper, rel_tol=1e-12, abs_tol=1e-12)
        results.append((lower, upper) if variable and spread else (None, None))
    return results


def _pair(records, invalid_count, method, protocol, current_week):
    baseline = protocol['baseline']
    excluded = dict(pending=0, missing_methods=0, invalid=invalid_count,
                    open_week=0, incomplete_block=0, void=0)
    blocks, group_ids = [], []
    for component in _components(records):
        scored = []
        incomplete = False
        for record in component:
            entry = record['entry']
            if not {baseline, method}.issubset(entry.get('methods', {})):
                excluded['missing_methods'] += 1
                continue
            status = entry.get('status')
            if status == 'void':
                excluded['void'] += 1
            elif status == 'pending':
                excluded['pending'] += 1
                incomplete = True
            elif status == 'unresolved':
                excluded['invalid'] += 1
                incomplete = True
            else:
                gains = _scores(entry, baseline, method)
                if gains is None:
                    excluded['invalid'] += 1
                    incomplete = True
                else:
                    scored.append((entry['id'], gains))
        if any(week >= current_week for record in component for week in record['weeks']):
            excluded['open_week'] += len(scored)
        elif incomplete:
            excluded['incomplete_block'] += len(scored)
        elif scored:
            blocks.append((len(scored), fsum(g[0] for _,g in scored), fsum(g[1] for _,g in scored)))
            group_ids.extend(identifier for identifier,_ in scored)
    groups = sum(block[0] for block in blocks)
    estimate = lambda index: fsum(block[index] for block in blocks)/groups if groups else None
    loss = dict(estimate=estimate(1), lower=None, upper=None)
    top3 = dict(estimate=estimate(2), lower=None, upper=None)
    status, reason = 'insufficient', f"Au moins {protocol['min_blocks']} blocs clos sont requis ; {len(blocks)} disponibles. Ce minimum ne garantit pas une puissance suffisante."
    if invalid_count:
        reason = 'Des affiches ou dates invalides empêchent de reconstituer toutes les dépendances ; aucun intervalle n’est calculé.'
    elif len(blocks) >= protocol['min_blocks']:
        intervals = _bootstrap(blocks, protocol)
        for output, (lower, upper) in zip((loss, top3), intervals):
            output.update(lower=lower, upper=upper)
        status = 'inconclusive'
        if any(interval[0] is None for interval in intervals):
            reason = 'Au moins un indicateur ne varie pas entre les blocs ou son intervalle se réduit à un point ; aucune précision ni équivalence n’est établie.'
        elif loss['lower'] > 0 and top3['lower'] >= 0:
            status, reason = 'favorable', 'Écart descriptif favorable sur l’erreur probabiliste, sans recul du top 3 dans les intervalles observés ; gain prédictif non confirmé.'
        elif loss['upper'] < 0 or top3['upper'] < 0:
            status, reason = 'unfavorable', 'Au moins un intervalle décrit un recul par rapport aux clubs seuls ; aucune conclusion définitive.'
        else:
            reason = 'Les intervalles ne permettent pas de distinguer un progrès selon les critères fixés.'
    return dict(method=method, baseline=baseline, groups=groups, blocks=len(blocks), status=status,
                reason=reason, delta_loss=loss, delta_top3=top3, excluded=excluded,
                group_ids=sorted(group_ids))


def evaluate(entries, methods, protocol, *, now=None):
    """Describe one exact-version cohort without modifying it or fitting a model."""
    protocol = validate_protocol(protocol)
    entries, methods = list(entries), tuple(methods)
    allowed = {protocol['baseline'], *protocol['challengers']}
    if len(set(methods)) != len(methods) or not set(methods).issubset(allowed):
        raise ValueError('Les méthodes de comparaison sont inconnues ou dupliquées.')
    if protocol['baseline'] not in methods:
        raise ValueError('Clubs seuls doit rester la référence de la comparaison.')
    current = now if now is not None else datetime.now(timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise ValueError('L’heure du bilan doit préciser son fuseau.')
    current = current.astimezone(timezone.utc)
    registered = _stamp(protocol['registered_at'])
    scopes, identities, versions = set(), set(), {name:set() for name in methods}
    records, legacy, prospective, invalid = [], 0, 0, 0
    for entry in sorted(entries, key=lambda item:str(item.get('id', ''))):
        try:
            identity, league, size = entry['id'], entry['league'], entry['size']
            if not isinstance(identity, str) or not identity or identity in identities:
                raise ValueError()
            if type(size) is not int or size not in protocol['sizes']:
                raise ValueError()
            identities.add(identity)
            scopes.add((str(league), size))
            for name in methods:
                if name in entry['methods']:
                    payload = entry['methods'][name]
                    version, generation = payload['version'], payload.get('generation', '')
                    if not isinstance(version, str) or not version or not isinstance(generation, str):
                        raise ValueError()
                    versions[name].add((version, generation))
            created = _stamp(entry['created_at'])
        except (KeyError, TypeError, ValueError):
            raise ValueError('La cohorte contient un groupe, une date ou une version invalide ou dupliquée.') from None
        if created < registered:
            legacy += 1
            continue
        prospective += 1
        try:
            if created > current:
                raise ValueError()
            records.append(_metadata(entry, created))
        except (KeyError, TypeError, ValueError, OverflowError):
            invalid += 1
    if len(scopes) > 1 or any(len(value) > 1 for value in versions.values()):
        raise ValueError('Le bilan doit conserver une seule compétition, une seule taille et les mêmes versions de chaque méthode.')
    comparisons = [_pair(records, invalid, method, protocol, _week(current))
                   for method in sorted(set(methods)-{protocol['baseline']})]
    return dict(protocol_version=protocol['version'], legacy_count=legacy, prospective_count=prospective,
                comparisons=comparisons)
