"""Pure daily paper-betting ledger from genuine saved pre-match scenarios.

One stake buys one distinct economic scenario, regardless of how many methods
proposed it. There is no network, storage, fitting or actual bet placement here.
The fixed user odds are indicative references, not historical bookmaker prices.
"""
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from hashlib import sha256
import json
from math import isclose, isfinite
from zoneinfo import ZoneInfo

from coupon_sizes import validate_size
from scenario_odds import DEFAULT_ODDS
import scenario_outcomes


ZONE = ZoneInfo('Africa/Kinshasa')
VALUES = ('1st', 'Égal', '2nd')
METHOD_LABELS = {'frequency': 'Répétitions', 'clubs': 'Clubs', 'combined': 'Combinaison',
                 'catboost': 'CatBoost', 'context_neighbors': 'Contextes'}
STATUSES = ('won', 'lost', 'pending', 'unresolved', 'refunded')
CENT = Decimal('.01')


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError('Date absente ou invalide.')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _amount(value):
    if isinstance(value, bool):
        raise ValueError('La mise doit être positive et exprimée en unités monétaires.')
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result <= 0:
            raise ValueError()
        with localcontext() as context:
            context.prec = 512
            result = result.quantize(CENT, rounding=ROUND_HALF_UP)
        if result <= 0 or not isfinite(float(result)):
            raise ValueError()
        return result
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        raise ValueError('La mise doit être finie, positive et atteindre au moins 0,01.') from None


def _float(value):
    result = float(value)
    if not isfinite(result):
        raise ValueError('Le montant de simulation dépasse la plage numérique disponible.')
    return result


def _sequence(value, size):
    if (not isinstance(value, (list, tuple)) or len(value) != size
            or any(not isinstance(item, str) or item not in VALUES for item in value)):
        raise ValueError('Séquence ordonnée absente ou invalide.')
    return tuple(value)


def _probability(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or not 0 <= value <= 1:
        raise ValueError('Probabilité enregistrée invalide.')
    return float(value)


def _fixture_id(value):
    if (isinstance(value, bool) or not isinstance(value, (str, int))
            or not str(value).isascii() or not str(value).isdecimal() or int(value) <= 0):
        raise ValueError('Identifiant API-Football invalide.')
    return str(int(value))


def _context(entry, current):
    if not isinstance(entry, dict) or entry.get('source') != 'API-Football':
        raise ValueError('invalid_source')
    if not isinstance(entry.get('id'), str) or not entry['id']:
        raise ValueError('invalid_forecast')
    size = validate_size(entry.get('size'))
    fixtures = entry.get('fixtures')
    if not isinstance(fixtures, (list, tuple)) or len(fixtures) != size:
        raise ValueError('invalid_forecast')
    created = _stamp(entry.get('created_at'))
    if created > current:
        raise ValueError('future_capture')
    identifiers, stamps = [], []
    for fixture in fixtures:
        if not isinstance(fixture, dict) or fixture.get('provider') != 'api-football':
            raise ValueError('invalid_source')
        identifiers.append(_fixture_id(fixture.get('match_id')))
        if not all(isinstance(fixture.get(key), str) and fixture[key].strip() for key in ('home','away')):
            raise ValueError('invalid_forecast')
        stamps.append(_stamp(fixture.get('timestamp')))
    if len(set(identifiers)) != size:
        raise ValueError('invalid_forecast')
    if created >= min(stamps):
        raise ValueError('after_kickoff')
    days = {stamp.astimezone(ZONE).date().isoformat() for stamp in stamps}
    if len(days) != 1:
        raise ValueError('mixed_days')
    return dict(size=size, fixtures=fixtures, identifiers=identifiers, stamps=stamps,
                created=created, day=next(iter(days)))


def _proposals(entry, context, exclusions):
    if 'proposal_selection' in entry:
        from adaptive_scenario_proposals import effective_methods
        methods = effective_methods(entry)
        if not methods:
            raise ValueError('selection_abstained')
    else:
        methods = entry.get('methods')
    if not isinstance(methods, dict) or not methods:
        raise ValueError('invalid_rankings')
    result = []
    names = [name for name in scenario_outcomes.METHOD_ORDER if name in methods]
    names += sorted(set(methods)-set(names), key=str)
    for name in names:
        try:
            if not isinstance(name, str) or not name.strip():
                raise ValueError()
            method = methods[name]
            ranking = method.get('ranking') if isinstance(method, dict) else None
            if not isinstance(ranking, (list, tuple)) or not ranking:
                raise ValueError()
            proposals, seen = [], set()
            for rank, item in enumerate(ranking, 1):
                if not isinstance(item, dict):
                    raise ValueError()
                sequence = _sequence(item.get('sequence'), context['size'])
                probability = _probability(item.get('probability'))
                if sequence in seen:
                    raise ValueError()
                seen.add(sequence)
                proposals.append((sequence, f'{METHOD_LABELS.get(name,name)} · Choix {rank}', probability))
            result.extend(proposals)
        except (ValueError, TypeError, KeyError):
            exclusions['invalid_rankings'] += 1
    if not result:
        raise ValueError('no_valid_proposals')
    return result


def _requested(entry, selection, size):
    if not isinstance(selection, dict) or selection.get('requested_scenario') is None:
        return None
    requested = selection['requested_scenario']
    if not isinstance(requested, dict):
        raise ValueError('invalid_requested_scenario')
    sequence = _sequence(requested.get('sequence'), size)
    probability = _probability(requested.get('probability'))
    # This reads only the supplied immutable mathematical distribution. It
    # never reconstructs probabilities from today's history or a live model.
    from prospective_models import probability_for_sequence
    expected = probability_for_sequence(entry['methods']['clubs'], sequence)
    if not isclose(probability, expected, rel_tol=1e-9, abs_tol=1e-12):
        raise ValueError('invalid_requested_scenario')
    return sequence, 'Clubs · Scénario choisi', probability


def _settlement(entry, context, sequence, current):
    status = entry.get('status')
    if status == 'pending':
        return 'pending'
    if status == 'unresolved' or status not in ('settled','void'):
        return 'unresolved'
    # Never let a later saved revision leak into an earlier simulated clock.
    checked = entry.get('checked_at')
    if checked is not None:
        try:
            checked = _stamp(checked)
            if checked > current:
                return 'unresolved'
        except (ValueError, TypeError):
            return 'unresolved'
    if status == 'void':
        return 'refunded'
    try:
        from fixture_schedule_resolution import observation_kickoff
        observations = entry.get('observed') or []
        if not isinstance(observations, (list,tuple)):
            return 'unresolved'
        effective = []
        for fixture, original in zip(context['fixtures'],context['stamps']):
            matching = [item for item in observations if isinstance(item,dict)
                        and str(item.get('match_id')) == str(fixture['match_id'])]
            stamps = {observation_kickoff(entry,fixture,item) for item in matching}
            if len(stamps)>1:
                return 'unresolved'
            effective.append(next(iter(stamps)) if stamps else original)
    except (ValueError, TypeError, KeyError, AttributeError):
        return 'unresolved'
    if max(effective) > current or checked is not None and checked < max(effective):
        return 'unresolved'
    report = scenario_outcomes.summarize(entry)
    if report['state'] not in ('hit','miss'):
        return 'unresolved'
    return 'won' if tuple(report['actual']) == sequence else 'lost'


def _economic_key(context, sequence):
    return tuple(sorted(zip(context['identifiers'], sequence), key=lambda item: int(item[0])))


def _first_kickoff(entry, context):
    """Conservative decision boundary, including verified earlier kickoffs."""
    try:
        from fixture_schedule_resolution import observation_kickoff
        stamps = list(context['stamps'])
        for fixture in context['fixtures']:
            for observation in entry.get('observed') or []:
                if isinstance(observation,dict) and str(observation.get('match_id')) == str(fixture['match_id']):
                    stamps.append(observation_kickoff(entry,fixture,observation))
        return min(stamps).isoformat()
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def _odds(sequence):
    value = Decimal(1)
    for label in sequence:
        value *= Decimal(str(DEFAULT_ODDS[label]))
    return value


def _summary(tickets):
    counts = Counter(row['status'] for row in tickets)
    stake_total = sum((row['_stake'] for row in tickets), Decimal(0))
    stake_settled = sum((row['_stake'] for row in tickets if row['status'] in ('won','lost')), Decimal(0))
    stake_open = sum((row['_stake'] for row in tickets if row['status'] in ('pending','unresolved')), Decimal(0))
    stake_refunded = sum((row['_stake'] for row in tickets if row['status']=='refunded'), Decimal(0))
    returned = sum((row['_returned'] for row in tickets if row['_returned'] is not None), Decimal(0))
    pnl = sum((row['_pnl'] for row in tickets if row['_pnl'] is not None), Decimal(0))
    return dict(bets=len(tickets), **{name: counts[name] for name in STATUSES},
        stake_total=_float(stake_total), stake_settled=_float(stake_settled), stake_open=_float(stake_open),
        stake_refunded=_float(stake_refunded), stake_resolved=_float(stake_settled+stake_refunded),
        returned=_float(returned), pnl=_float(pnl), roi=_float(100*pnl/stake_settled) if stake_settled else None)


def build(entries, intents=(), stake=1.0, now=None):
    """Return unique simulated tickets and daily accounting, without I/O.

    Every explicitly saved proposal and valid pre-match user choice participates.
    Unranked possibilities in a mathematical distribution do not add tickets.
    Daily grouping uses the complete group's original Kinshasa match day.
    Distinct groups sharing some matches are separate bets, not independent
    evidence. A void group is refunded in full by this simulation's convention.
    """
    current = datetime.now(timezone.utc) if now is None else now
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise ValueError('Une horloge avec fuseau horaire est nécessaire.')
    current = current.astimezone(timezone.utc)
    money = _amount(stake)
    exclusions, contexts, prepared = Counter(), {}, []
    for entry in entries:
        try:
            context = _context(entry, current)
            proposals = _proposals(entry, context, exclusions)
            prepared.append((context['created'], entry['id'], entry, context, proposals))
            # A real journal has one immutable forecast per ID. Conflicting
            # duplicated IDs are not allowed to give an intent a new parent.
            if entry['id'] not in contexts:
                contexts[entry['id']] = (entry, context)
            elif contexts[entry['id']] is not None and contexts[entry['id']][0] != entry:
                contexts[entry['id']] = None
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            reason = str(error)
            exclusions[reason if reason in ('invalid_source','invalid_forecast','future_capture','after_kickoff',
                'mixed_days','invalid_rankings','no_valid_proposals','selection_abstained') else 'invalid_forecast'] += 1

    extras = []
    for _, _, entry, context, _ in prepared:
        try:
            value = _requested(entry, (entry.get('metadata') or {}).get('selection'), context['size'])
            if value:
                extras.append((context['created'], entry['id'], entry, context, [value]))
        except (ValueError, TypeError, KeyError, AttributeError):
            exclusions['invalid_requested_scenario'] += 1

    digest_cache = {}
    for intent in intents:
        try:
            if not isinstance(intent, dict) or not isinstance(intent.get('id'), str) or not intent['id']:
                raise ValueError()
            parent = contexts.get(intent.get('entry_id'))
            if not parent:
                raise ValueError()
            entry, context = parent
            selected_at = _stamp(intent.get('created_at'))
            if not context['created'] <= selected_at <= current or selected_at >= min(context['stamps']):
                raise ValueError()
            if entry['id'] not in digest_cache:
                from probability_scenario_store import forecast_digest
                digest_cache[entry['id']] = forecast_digest(entry)
            if intent.get('forecast_digest') != digest_cache[entry['id']]:
                raise ValueError()
            value = _requested(entry, intent.get('selection'), context['size'])
            if not value:
                raise ValueError()
            extras.append((selected_at, entry['id'], entry, context, [value]))
        except (ValueError, TypeError, KeyError, AttributeError):
            exclusions['invalid_intent'] += 1

    tickets, duplicates = {}, 0
    with localcontext() as decimal_context:
        decimal_context.prec = 512
        for captured, entry_id, entry, context, proposals in sorted(prepared+extras, key=lambda row: (row[0],row[1])):
            for sequence, method, probability in proposals:
                key = _economic_key(context, sequence)
                state = _settlement(entry, context, sequence, current)
                actual_key = (_economic_key(context, tuple(entry['actual'])) if state in ('won','lost') else None)
                raw_issues = entry.get('issues') or []
                issues = ({message for message in raw_issues if isinstance(message,str)}
                          if isinstance(raw_issues,(list,tuple)) else set())
                percent = (f'{100*probability:.6g}' if 0 < 100*probability < .0001 else f'{100*probability:.4f}')
                probability_label = f'{method} : {percent} %'.replace('.', ',')
                if key in tickets:
                    duplicates += 1
                    value = tickets[key]
                    value['_states'].add(state)
                    value['_methods'].add(method)
                    value['_probabilities'].add(probability_label)
                    value['_entry_ids'].add(entry_id)
                    kickoff = _first_kickoff(entry,context)
                    value['kickoff_at'] = (min(value['kickoff_at'],kickoff)
                        if value['kickoff_at'] and kickoff else None)
                    value['_issues'].update(issues)
                    if actual_key is not None:
                        value['_actuals'].add(actual_key)
                    continue
                odds = _odds(sequence)
                tickets[key] = dict(id=sha256(_json(key).encode()).hexdigest(), day=context['day'],
                    entry_id=entry_id, size=context['size'],
                    match_ids=list(context['identifiers']),
                    matches=' · '.join(f'{index}. {fixture["home"]} — {fixture["away"]}'
                                      for index, fixture in enumerate(context['fixtures'],1)),
                    sequence=list(sequence), odds=_float(odds), stake=_float(money),
                    captured_at=captured.isoformat(), kickoff_at=_first_kickoff(entry,context),
                    _states={state}, _methods={method},
                    _probabilities={probability_label}, _entry_ids={entry_id}, _stake=money, _odds=odds,
                    _actuals={actual_key} if actual_key is not None else set(),
                    _fixture_ids=context['identifiers'], _issues=issues)
        values = list(tickets.values())
        for row in values:
            states = row.pop('_states')
            resolved = states & {'won','lost','refunded'}
            actuals = row.pop('_actuals')
            status = ('unresolved' if 'unresolved' in states or len(resolved)>1 or len(actuals)>1 else
                      next(iter(resolved)) if resolved else 'pending')
            if status == 'won':
                returned = (money*row['_odds']).quantize(CENT, rounding=ROUND_HALF_UP)
            elif status == 'lost':
                returned = Decimal(0)
            elif status == 'refunded':
                returned = money
            else:
                returned = None
            pnl = returned-money if returned is not None else None
            actual_by_id = dict(next(iter(actuals))) if status in ('won','lost') and len(actuals)==1 else None
            actual = ([actual_by_id[mid] for mid in row['_fixture_ids']] if actual_by_id is not None else None)
            reasons = {'won':'Scénario exact réalisé.', 'lost':'Scénario exact non réalisé.',
                'pending':'Résultats du groupe attendus.',
                'unresolved':'Résultats non évaluables ou contradictoires.',
                'refunded':'Convention de simulation : remboursement intégral du groupe annulé.'}
            reason = reasons[status]
            issues = row.pop('_issues')
            if status in ('pending','unresolved','refunded') and issues:
                reason += ' ' + ' · '.join(sorted(issues))
            row.update(status=status, return_amount=_float(returned) if returned is not None else None,
                pnl=_float(pnl) if pnl is not None else None,
                methods=sorted(row.pop('_methods')), probability_by_method=' · '.join(sorted(row.pop('_probabilities'))),
                entry_ids=sorted(row.pop('_entry_ids')), actual=actual, reason=reason,
                _returned=returned, _pnl=pnl)
        days = [dict(day=day, **_summary([row for row in values if row['day']==day]))
                for day in sorted({row['day'] for row in values}, reverse=True)]
        summary = _summary(values)
        for row in values:
            for field in ('_stake','_odds','_returned','_pnl','_fixture_ids'):
                row.pop(field)
    return dict(tickets=sorted(values, key=lambda row: (row['day'],row['captured_at'],row['id']), reverse=True),
                days=days, summary=summary, exclusions=dict(exclusions), duplicates_removed=duplicates)
