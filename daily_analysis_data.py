"""Read-only projections of daily selections and their immutable forecasts."""
from datetime import date, datetime
from math import isfinite
from copy import deepcopy

import journal_performance as performance
import prediction_journal as journal
import scenario_outcomes


STATE_LABELS = {'pending': 'En attente', 'settled': 'Réalisé',
                'unresolved': 'À vérifier', 'void': 'Annulé'}


def day_of(entry):
    fixtures = entry.get('fixtures') or []
    try:
        if len(fixtures) != entry['size'] or not fixtures:
            return None
        days = {journal._stamp(row['timestamp']).astimezone(performance.LOCAL_ZONE).date() for row in fixtures}
        return next(iter(days)) if len(days) == 1 else None
    except (journal.JournalError, KeyError, TypeError, ValueError):
        return None


def project(entries, selections=()):
    """Use only the support saved at selection time, never today's histories."""
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
        saved = saved or metadata
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
                           selection=deepcopy(selection), competitions=deepcopy(competitions)))
    return sorted(output, key=lambda row: (-(row['support'] if row['support'] is not None else -1),
                                           row['entry']['created_at'], row['id']))


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
