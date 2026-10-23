"""Read-only projections of daily selections and their immutable forecasts."""
from datetime import date, datetime
from math import isclose, isfinite
from copy import deepcopy

import journal_performance as performance
import prediction_journal as journal
import prospective_models as models
import scenario_outcomes


STATE_LABELS = {'pending': 'En attente', 'settled': 'Réalisé',
                'unresolved': 'À vérifier', 'void': 'Annulé'}
PROBABILITY_POLICIES = ('high-probability-v1', 'high-probability-v2', 'high-probability-v3')


def searched_scenario(entry, selection):
    """Describe the saved search target independently of the model's top three.

    Only the saved full distribution and final, settled sequence are consulted.
    A pending result never inherits a stale success from an earlier revision.
    """
    requested = selection.get('requested_scenario')
    if requested is None:
        return None
    result = dict(sequence=None, probability=None, state='unavailable',
                  label='Scénario recherché à vérifier')
    try:
        sequence = requested['sequence']
        probability = requested['probability']
        if (not isinstance(sequence, list) or len(sequence) != entry['size']
                or any(value not in ('1st', 'Égal', '2nd') for value in sequence)
                or isinstance(probability, bool) or not isinstance(probability, (int, float))
                or not isfinite(probability) or not 0 <= probability <= 1):
            return result
        expected = models.probability_for_sequence(entry['methods']['clubs'], sequence)
        if not isclose(probability, expected, rel_tol=1e-9, abs_tol=1e-12):
            return result
        result.update(sequence=list(sequence), probability=probability)
        status = entry.get('status')
        if status == 'settled':
            actual = entry.get('actual')
            if (not isinstance(actual, list) or len(actual) != entry['size']
                    or any(value not in ('1st', 'Égal', '2nd') for value in actual)):
                return result
            hit = actual == sequence
            result.update(state='hit' if hit else 'miss',
                          label='Scénario recherché réalisé' if hit else 'Scénario recherché non réalisé')
        elif status == 'pending':
            result.update(state='pending', label='Scénario recherché en attente')
        elif status == 'void':
            result.update(state='void', label='Scénario recherché annulé')
        elif status == 'unresolved':
            result.update(state='unresolved', label='Scénario recherché à vérifier')
    except (KeyError, TypeError, ValueError):
        pass
    return result


def day_of(entry):
    fixtures = entry.get('fixtures') or []
    try:
        if len(fixtures) != entry['size'] or not fixtures:
            return None
        days = {journal._stamp(row['timestamp']).astimezone(performance.LOCAL_ZONE).date() for row in fixtures}
        return next(iter(days)) if len(days) == 1 else None
    except (journal.JournalError, KeyError, TypeError, ValueError):
        return None


def project(entries, selections=(), scenario_intents=()):
    """Use only the support saved at selection time, never today's histories."""
    entries = list(entries)
    indexed = {}
    for selection in selections:
        if isinstance(selection, dict) and isinstance(selection.get('entry_id'), str):
            indexed.setdefault(selection['entry_id'], selection)
    output = []
    for entry in entries:
        if entry.get('source') != 'API-Football' or entry.get('status') not in STATE_LABELS:
            continue
        metadata = entry.get('metadata') or {}
        saved = indexed.get(entry['id'])
        if saved is None and not metadata.get('daily_policy'):
            continue
        # A later support planner may reuse a probability-search forecast.
        # Preserve the criteria recorded with that original prediction.
        saved = metadata if metadata.get('daily_policy') in PROBABILITY_POLICIES else saved or metadata
        selection = saved.get('selection') or metadata.get('selection') or {}
        competitions = saved.get('competitions') or metadata.get('competitions') or {}
        day = day_of(entry)
        if day is None:
            continue
        support = selection.get('data_support_index')
        if isinstance(support, bool) or not isinstance(support, (int, float)) or not isfinite(support) or not 0 <= support <= 100:
            support = None
        output.append(dict(id=entry['id'], entry=entry, day=day, status=entry['status'],
                           status_label=STATE_LABELS[entry['status']], support=support,
                           scenario_outcome=scenario_outcomes.summarize(entry),
                           searched_scenario=searched_scenario(entry, selection),
                           selection=deepcopy(selection), competitions=deepcopy(competitions)))
    if scenario_intents:
        import probability_scenario_store as intents
        by_id = {entry['id']: entry for entry in entries}
        linked, personal = set(), []
        for intent in scenario_intents:
            entry = by_id.get(intent.get('entry_id'))
            if (not entry or entry.get('source') != 'API-Football' or entry.get('status') not in STATE_LABELS
                    or intent.get('forecast_digest') != intents.forecast_digest(entry)):
                continue
            day = day_of(entry)
            selection = intent.get('selection') or {}
            if day is None or not isinstance(selection, dict):
                continue
            searched = searched_scenario(entry, selection)
            if searched is None:
                continue
            linked.add(entry['id'])
            personal.append(dict(id='intent:'+intent['id'], entry=entry, day=day, status=entry['status'],
                status_label=STATE_LABELS[entry['status']], support=None,
                scenario_outcome=scenario_outcomes.summarize(entry), searched_scenario=searched,
                selection=deepcopy(selection), competitions=deepcopy(intent.get('competitions') or {}),
                scenario_intent=deepcopy(intent), selection_created_at=intent['created_at']))
        # A selected scenario is an analysis of the existing forecast, not a new forecast observation.
        output = [row for row in output if row['entry']['id'] not in linked] + personal
    return sorted(output, key=lambda row: (-(row['support'] if row['support'] is not None else -1),
                                           row.get('selection_created_at', row['entry']['created_at']), row['id']))


def on_day(rows, day, *, today):
    if type(day) is not date or type(today) is not date or day > today:
        raise ValueError('Choisissez aujourd’hui ou une journée passée.')
    return [row for row in rows if row['day'] == day]


def competition_name(row, league):
    info = row['competitions'].get(str(league), {})
    if isinstance(info, str):
        return info
    name = info.get('name') or info.get('competition') or str(league)
    country = info.get('country')
    return f'{country} · {name}' if country else name
