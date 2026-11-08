"""Read-only history of probability-search choices and frozen match forecasts.

Several choices can share a forecast. This projection preserves that distinction
and never reconstructs an earlier prediction with the current model.
"""
from copy import deepcopy
from datetime import datetime, timezone
from math import isclose
from zoneinfo import ZoneInfo

import daily_analysis_data as daily
import probability_reliability as reliability


VERSION = 'high-probability-history-v1'
ZONE = ZoneInfo('Africa/Kinshasa')
VALUES = ('1st', 'Égal', '2nd')
POLICIES = daily.PROBABILITY_POLICIES


def _match_rows(row):
    entry = row['entry']
    fixtures = entry['fixtures']
    try:
        positions = reliability._positions(entry['methods']['clubs'], fixtures)
    except (ValueError, TypeError, KeyError, AttributeError):
        positions = [None] * len(fixtures)
    selected = {str(item.get('match_id')): item for item in
                row['selection'].get('match_probabilities', []) if isinstance(item, dict)}
    requested = row.get('searched_scenario') or {}
    sequence = requested.get('sequence') or []
    observed = entry.get('observed') or []
    explanations = {str(item.get('match_id')): item for item in
                    (entry.get('result_explanation') or {}).get('matches', []) if isinstance(item, dict)}
    result = []
    for index, (fixture, position) in enumerate(zip(fixtures, positions)):
        saved = selected.get(str(fixture['match_id']), {})
        probabilities = deepcopy(position['probabilities']) if position else None
        # A contradictory saved display must not acquire a freshly invented
        # percentage. Missing legacy display metadata can use the frozen model.
        if probabilities is not None and saved.get('probabilities') is not None:
            try:
                display = reliability._probabilities(saved['probabilities'])
                if any(not isclose(display[key], probabilities[key], rel_tol=1e-9, abs_tol=1e-12)
                       for key in VALUES):
                    probabilities = None
            except (ValueError, TypeError):
                probabilities = None
        choice = saved.get('search_outcome')
        if choice not in VALUES:
            choice = sequence[index] if index < len(sequence) and sequence[index] in VALUES else None
        checked = reliability._result(entry, fixture)
        state = ('void' if checked.get('reason') == 'void' else
                 'unresolved' if checked['state'] == 'excluded' else checked['state'])
        actual = checked.get('actual') if state == 'evaluated' else None
        observation = next((value for value in observed if isinstance(value, dict)
                            and str(value.get('match_id')) == str(fixture['match_id'])), {}) if actual else {}
        verdict = ('unavailable' if choice is None else
                   ('hit' if actual == choice else 'miss') if state == 'evaluated' else state)
        result.append(dict(order=index + 1, match_id=str(fixture['match_id']), home=fixture['home'], away=fixture['away'],
            competition=daily.competition_name(row, fixture['league']), kickoff=fixture['timestamp'],
            probabilities=probabilities, searched_outcome=choice,
            searched_probability=probabilities.get(choice) if probabilities is not None else None,
            actual=actual, ht=observation.get('ht'), ft=observation.get('ft'), state=state,
            verdict=verdict, created_at=row.get('selection_created_at', entry['created_at']),
            result_explanation=deepcopy(explanations.get(str(fixture['match_id']), {}))))
    return result


def build(entries, intents=(), now=None):
    """Project genuine saved probability-search choices, including future games.

    ``entries`` and ``intents`` come from their verified local stores. Scores
    are the latest saved journal observations; no provider or model is queried.
    All returned values are detached from the inputs.
    """
    current = datetime.now(timezone.utc) if now is None else now
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise ValueError('Une horloge avec fuseau horaire est nécessaire.')
    output, excluded = [], 0
    for source in daily.project(entries, scenario_intents=intents):
        if source['selection'].get('policy') not in POLICIES:
            continue
        try:
            created = reliability._stamp(source['entry']['created_at'])
            chosen = reliability._stamp(source.get('selection_created_at', source['entry']['created_at']))
            if (chosen > current or created > chosen
                    or any(chosen >= reliability._stamp(fixture['timestamp'])
                           for fixture in source['entry']['fixtures'])):
                raise ValueError('Choix hors de la période avant match.')
            future_ids = {str(fixture['match_id']) for fixture in source['entry']['fixtures']
                          if reliability._stamp(fixture['timestamp']) > current}
            if ((future_ids and source['entry'].get('status') == 'settled') or any(
                    str(item.get('match_id')) in future_ids
                    for item in source['entry'].get('observed', []) if isinstance(item, dict))):
                raise ValueError('Résultat enregistré avant le coup d’envoi réel.')
            row = deepcopy(source)
            row['match_rows'] = _match_rows(row)
            output.append(row)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            excluded += 1
    output.sort(key=lambda row: (reliability._stamp(row.get('selection_created_at', row['entry']['created_at'])),
                                 row['id']), reverse=True)
    return dict(version=VERSION, rows=output, choices=len(output), excluded=excluded,
                forecasts=len({row['entry']['id'] for row in output}),
                fixtures=len({match['match_id'] for row in output for match in row['match_rows']}))
