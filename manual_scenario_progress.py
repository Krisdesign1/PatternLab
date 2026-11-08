"""Read-only partial progress; never settle a group or alter saved forecasts.

A known contradiction is useful while another score is missing. It is displayed
separately from matches_all/top1/top3, whose complete-group contract is unchanged.
"""
from collections import Counter

import manual_proposal_archive as proposals
from scenario_tables import build_matrix

VALUES = ('1st', 'Égal', '2nd')
LABELS = {'hit': 'Réalisé', 'miss': 'Non réalisé',
          'contradicted': 'Déjà contredit', 'possible': 'Encore possible',
          'pending': 'En attente', 'void': 'Annulé du suivi',
          'non_evaluable': 'À vérifier', 'unavailable': 'Vérification indisponible'}


def project(entry, evaluation):
    """Describe saved ordered choices using only confirmed per-match outcomes."""
    fixtures = entry['fixtures']
    size = len(fixtures)
    status = evaluation.get('status', 'unavailable')
    if status not in ('settled', 'pending', 'void', 'non_evaluable', 'unavailable'):
        status = 'unavailable'
    observations = evaluation.get('matches', [])
    counts = Counter(str(row.get('match_id')) for row in observations)
    observed = {str(row.get('match_id')):row for row in observations}
    actual = []
    for fixture in fixtures:
        identity = str(fixture['match_id'])
        row = observed.get(identity, {})
        label = row.get('actual')
        actual.append(label if status != 'unavailable' and counts[identity] == 1
                      and row.get('status') == 'settled' and label in VALUES else None)
    # Older presentation callers can supply a complete verified result without
    # the per-match detail. Never use an incomplete group's stale actual list.
    complete = evaluation.get('actual')
    if ('matches' not in evaluation and status == 'settled' and isinstance(complete, list)
            and len(complete) == size and all(v in VALUES for v in complete)):
        actual = list(complete)
    known = sum(value is not None for value in actual)
    if status == 'settled' and known != size:
        # A contradictory/missing per-match detail cannot support a complete
        # verdict merely because a stale caller marked the group settled.
        status, actual, known = 'unavailable', [None]*size, 0
    coverage = proposals.coverage(entry)
    matrix = build_matrix(proposals.methods(entry))
    rows = []
    for position, saved in enumerate(matrix['rows'], 1):
        sequence = saved['sequence']
        if len(sequence) != size or any(value not in VALUES for value in sequence):
            raise ValueError('Les scénarios doivent conserver tous les matchs dans leur ordre.')
        conflicts = [index+1 for index, (choice, result) in enumerate(zip(sequence, actual))
                     if result is not None and choice != result]
        if status in ('void', 'unavailable'):
            state = status
        elif status == 'settled' and known == size:
            state = 'miss' if conflicts else 'hit'
        elif conflicts:
            state = 'contradicted'
        elif status == 'non_evaluable':
            state = 'non_evaluable'
        else:
            state = 'possible' if known else 'pending'
        if state in ('miss', 'contradicted'):
            reason = ' · '.join(f'Match {position} : choix {sequence[position-1]}, résultat {actual[position-1]}'
                                for position in conflicts)
        elif state == 'hit':
            reason = 'Tous les résultats correspondent au scénario enregistré.'
        elif state == 'void':
            reason = 'Le groupe contient un match annulé, abandonné ou attribué.'
        elif state == 'unavailable':
            reason = 'Le catalogue des résultats ne peut pas être lu.'
        elif state == 'non_evaluable':
            reason = 'Un résultat manque ou nécessite une vérification.'
        else:
            missing = size-known
            reason = f'{missing} résultat(s) encore attendu(s).'
            if known:
                reason = 'Aucune contradiction parmi les résultats confirmés. '+reason
        selected = saved['cells'].get(coverage['selected_method'], {'rank': None, 'probability': None})
        rows.append(dict(rank=selected['rank'], sequence=list(sequence), probability=selected['probability'],
                         proposal_index=position, cells=saved['cells'],
                         state=state, label=LABELS[state], reason=reason,
                         contradicted_positions=conflicts))
    contradicted = sum(row['state'] == 'contradicted' for row in rows)
    group_label = {'settled':'Groupe évalué', 'pending':'Groupe incomplet',
                   'void':'Groupe annulé du suivi', 'non_evaluable':'Groupe à vérifier',
                   'unavailable':'Résultats locaux indisponibles'}[status]
    scope = 'proposés' if coverage['complete'] else 'conservés'
    if coverage.get('abstained'):
        summary = 'Aucun scénario proposé : la règle de sélection ne dispose pas d’une qualification historique suffisante pour ce groupe.'
    elif contradicted == len(rows) and rows:
        summary = f'Tous les scénarios {scope} sont déjà contredits par les résultats confirmés.'
        if not coverage['complete']:
            summary += ' Comparaison initiale incomplète.'
    elif contradicted:
        summary = f'{contradicted}/{len(rows)} scénario(s) déjà contredit(s).'
    elif status == 'settled':
        if any(row['state']=='hit' for row in rows):
            summary = f'Un scénario {scope} s’est réalisé.'
            if not coverage['complete']:
                summary += ' Comparaison initiale incomplète.'
        elif coverage['complete']:
            summary = 'Aucun scénario proposé réalisé.'
        else:
            summary = (f'Comparaison incomplète : aucun des {len(rows)} choix du moteur conservé ne s’est réalisé. '
                       'Le résultat des autres propositions initiales est indéterminable.')
    elif status == 'pending' and known:
        summary = 'Les scénarios encore possibles attendent les résultats manquants.'
    else:
        summary = group_label+'.'
    return dict(status=status, known_count=known, total=size, actual=actual,
                progress_label=f'{known}/{size} résultats confirmés', group_label=group_label,
                rows=rows, summary=summary, contradicted_count=contradicted,
                comparison_complete=coverage['complete'], coverage=coverage)
