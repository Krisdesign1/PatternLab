"""Pure browser rows for exact archived manual calculations.

These display records are not prediction-journal entries. In particular they
carry no official methods, evaluations, or cohort evidence. Only the manual
archive's read-only result projection supplies their observed outcomes.
"""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import journal_browser_data as browser
import manual_analysis_store as archive
import manual_scenario_progress
import manual_proposal_archive as proposals
import scenario_outcomes


ZONE = ZoneInfo('Africa/Kinshasa')
ENGINES = {'statistical': 'Clubs + répétitions', 'catboost': 'CatBoost entraîné',
           'context_neighbors': 'Répétitions par contexte'}


def _stamp(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)


def _now(value):
    value = datetime.now(timezone.utc) if value is None else value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise archive.ManualAnalysisError('L’heure de consultation doit préciser son fuseau.')
    return value.astimezone(timezone.utc)


def _unavailable(entry):
    """Unavailable storage is not evidence that a result is missing or late."""
    reason = 'Le catalogue local des résultats est indisponible.'
    return dict(id=entry['id'], status='unavailable', status_label='Résultats locaux indisponibles',
        actual=None, top1=None, top3=None,
        scenario_verdicts=[dict(rank=index, sequence=deepcopy(item['sequence']),
                               probability=item['probability'], matches_all=None)
                           for index, item in enumerate(entry['forecast']['ranking'], 1)],
        matches=[dict(deepcopy(fixture), status='unavailable', reason=reason, actual=None)
                 for fixture in entry['fixtures']], issues=[reason])


def _outcome(entry, evaluation):
    status = evaluation['status']
    state = {'non_evaluable': 'unresolved', 'unavailable': 'unavailable'}.get(status, status)
    matches, actual = [], None
    coverage = proposals.coverage(entry)
    if status == 'settled':
        actual = deepcopy(evaluation['actual'])
        following = manual_scenario_progress.project(entry, evaluation)
        for item in following['rows']:
            if item['state'] == 'hit':
                for name, cell in item['cells'].items():
                    if cell['rank']:
                        method = name if entry.get('comparison_methods') else entry['engine']
                        matches.append(dict(method=method, rank=cell['rank'], sequence=deepcopy(item['sequence'])))
        state = 'hit' if matches else 'miss' if coverage['complete'] else 'unavailable'
    if coverage.get('abstained'):
        state = 'unavailable'
    label = ('Aucun scénario proposé' if coverage.get('abstained') else
             'Comparaison incomplète' if status == 'settled' and state == 'unavailable' and not coverage['complete']
             else scenario_outcomes.LABELS[state])
    return dict(state=state, label=label, matches=matches, actual=actual)


def _row(entry, observations, current):
    # evaluate validates the archive identity and all immutable source fields.
    # No synthetic prediction-journal entry is constructed for this projection.
    evaluation = archive.evaluate(entry, [] if observations is None else observations, now=current)
    if observations is None:
        evaluation = _unavailable(entry)
    fixtures = deepcopy(entry['fixtures'])
    stamps = [_stamp(fixture['timestamp']) for fixture in fixtures]
    created = _stamp(entry['created_at'])
    if created > current:
        raise archive.ManualAnalysisError('Un calcul ne peut pas être consulté avant son enregistrement.')
    leagues = sorted({fixture['league'] for fixture in fixtures}, key=int)
    league = leagues[0] if len(leagues) == 1 else 'mixed'
    info = entry.get('competition') or {}
    competition = info.get('name') or info.get('competition') or (
        league if len(leagues) == 1 else 'Compétitions multiples')
    country = entry.get('country') or info.get('country') or 'Pays non précisé'
    observed = sum(match['status'] == 'settled' and match.get('actual') in archive.CLASS_ORDER
                   for match in evaluation['matches'])
    status = {'non_evaluable': 'unresolved', 'unavailable': 'unresolved'}.get(
        evaluation['status'], evaluation['status'])
    state = status
    if status == 'pending':
        state = 'in_progress' if observed else 'awaiting_results' if min(stamps) <= current else 'pending'
    local = [stamp.astimezone(ZONE) for stamp in stamps]
    saved_methods = proposals.methods(entry)
    selected_name = proposals.ENGINE_METHOD[entry['engine']]
    selected = saved_methods.get(selected_name, {}).get('ranking') or []
    primary_name = selected_name if selected else next((name for name, method in saved_methods.items()
                                                        if method.get('ranking')), None)
    first = saved_methods[primary_name]['ranking'][0] if primary_name else None
    primary_method, primary_label = primary_name, {
        'frequency':'Répétitions', 'clubs':'Clubs', 'combined':'Combinaison',
        'catboost':'CatBoost', 'context_neighbors':'Contextes'}.get(primary_name, 'Aucune proposition')
    if entry.get('proposal_selection') is None:
        first = entry['forecast']['ranking'][0]
        primary_method, primary_label = entry['engine'], ENGINES[entry['engine']]
    identity = 'manual:' + entry['id']
    following = manual_scenario_progress.project(entry, evaluation)
    row = dict(id=identity, entry_id=identity, archive_id=entry['id'], record_kind='manual_calculation',
        source=entry['source'], league=league, leagues=leagues, competition=deepcopy(competition), country=country,
        size=len(fixtures), origin='manual', origin_label=browser.ORIGINS['manual'],
        journal_status=status, state=state, status=state,
        status_label=evaluation['status_label'] if evaluation['status'] == 'unavailable' else browser.STATES[state],
        created_at=created.isoformat(), created_local=created.astimezone(ZONE).strftime('%d/%m/%Y à %H:%M'),
        kickoff_range=f"{min(local).strftime('%H:%M')}–{max(local).strftime('%H:%M')}",
        day=entry['day'], fixtures=fixtures,
        clubs=[name for fixture in fixtures for name in (fixture['home'], fixture['away'])],
        match_names=[f"{fixture['home']} — {fixture['away']}" for fixture in fixtures],
        observed_matches=observed, expected_matches=len(fixtures), progress=observed, total=len(fixtures),
        primary_method=primary_method, primary_method_name=primary_label,
        primary_sequence=deepcopy(first['sequence']) if first else None,
        primary_probability=first['probability'] if first else None,
        primary_version=deepcopy(entry['forecast'].get('version')), primary_generation=deepcopy(entry.get('model_generation')),
        scenario_outcome=_outcome(entry, evaluation),
        comparison_complete=following['comparison_complete'], proposal_coverage=deepcopy(following['coverage']),
        forecast=deepcopy(entry['forecast']), evaluation=deepcopy(evaluation))
    if following['contradicted_count'] and status != 'settled':
        row['result_explanation'] = dict(blocking=True,
            label=f"{following['contradicted_count']}/{len(following['rows'])} scénarios déjà contredits",
            summary='Comparaison partielle des résultats confirmés ; le groupe reste incomplet.')
        if row['scenario_outcome']['state']=='pending':
            row['scenario_outcome']['label']='Évaluation complète en attente'
    if not following['comparison_complete']:
        partial = row.get('result_explanation', {}).get('summary')
        partial_label = row.get('result_explanation', {}).get('label')
        row['result_explanation'] = dict(blocking=True,
            label='Comparaison initiale incomplète' + (' · ' + partial_label if partial_label else ''),
            summary=('Seuls les choix du moteur sélectionné sont conservés. '
                     'Cette archive ne mesure pas la réussite de toutes les propositions affichées.'
                     + (' ' + partial if partial else '')))
    return row


def build_rows(entries, raw_fixtures, now=None):
    """Project validated archive entries into the common analyses browser.

    The caller selects the archived day; this function retains every complete
    group passed to it. ``raw_fixtures`` is already-loaded local provider data,
    not an API client. ``None`` means that local catalogue could not be read;
    an empty iterable means it was read successfully without observations.
    IDs are namespaced, and copies never mutate archived choices or scores.
    """
    current = _now(now)
    entries = list(entries)
    if any(not isinstance(entry, dict) for entry in entries):
        raise archive.ManualAnalysisError('Les calculs doivent conserver leur structure archivée.')
    # Materialize once: a generator must provide the same observations to every
    # calculation, including recalculations sharing the same fixture IDs.
    observations = None if raw_fixtures is None else list(
        raw_fixtures.values() if isinstance(raw_fixtures, dict) else raw_fixtures)
    rows, identities = [], set()
    for entry in entries:
        row = _row(entry, observations, current)
        if row['id'] in identities:
            raise archive.ManualAnalysisError('Un calcul ne peut pas être affiché deux fois.')
        identities.add(row['id'])
        rows.append(row)
    return sorted(sorted(rows, key=lambda row: row['id']),
                  key=lambda row: _stamp(row['created_at']), reverse=True)


def summary(rows):
    """Count display groups and verified results, without model statistics.

    The keys match the common browser's progress summary. Rows may share
    matches; these display totals are never independent model observations.
    """
    rows = list(rows)
    counts = Counter(row['journal_status'] for row in rows)
    return dict(total=len(rows), **{state: counts[state] for state in ('pending', 'settled', 'void', 'unresolved')},
                observed_matches=sum(row['observed_matches'] for row in rows),
                expected_matches=sum(row['expected_matches'] for row in rows))
