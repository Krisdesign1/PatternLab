"""Descriptive performance of daily forecasts actually frozen before kickoff.

This is a pure projection of saved entries: no API, training, reconciliation,
or retrospective forecast is permitted here. Incomplete groups never fail.
"""
from collections import defaultdict
from math import fsum, isfinite

import daily_analysis_data as data
import journal_performance as performance
import prediction_journal as journal


def summarize(entries, selections=()):
    rows = data.project(entries, selections)
    counts = dict(total=len(rows), pending=0, settled=0, unresolved=0, void=0, invalid=0)
    groups = defaultdict(list)
    all_matches, settled_matches = set(), set()
    for row in rows:
        entry = row['entry']
        ids = {str(item['match_id']) for item in entry['fixtures']}
        try:
            created = journal._stamp(entry['created_at'])
            if len(ids) != entry['size'] or any(created >= journal._stamp(item['timestamp']) for item in entry['fixtures']):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            counts['invalid'] += 1
            continue
        counts[row['status']] += 1
        all_matches.update(ids)
        if row['status'] == 'settled':
            settled_matches.update(ids)
        for name, payload in entry['methods'].items():
            key = (entry['size'], name, payload.get('version') or '', payload.get('generation') or '',
                   row['selection'].get('policy') or entry['metadata'].get('daily_policy') or 'Non conservée')
            evaluation = entry.get('evaluations', {}).get(name)
            valid = (row['status'] == 'settled' and isinstance(evaluation, dict)
                     and type(evaluation.get('top1')) is bool and type(evaluation.get('top3')) is bool
                     and isinstance(evaluation.get('loss'), (int, float))
                     and not isinstance(evaluation.get('loss'), bool)
                     and isfinite(evaluation['loss']) and evaluation['loss'] >= 0)
            groups[key].append(dict(row=row, evaluation=evaluation if valid else None))
    output = []
    for (size, method, version, generation, policy), group in sorted(groups.items()):
        tested = [item for item in group if item['evaluation'] is not None]
        n = len(tested)
        output.append(dict(size=size, method=method, version=version, generation=generation, policy=policy,
            groups=len(group), evaluated=n, pending=sum(item['row']['status'] == 'pending' for item in group),
            excluded=sum(item['row']['status'] in ('void','unresolved') for item in group),
            missing_evaluations=sum(item['row']['status'] == 'settled' and item['evaluation'] is None for item in group),
            top1=sum(item['evaluation']['top1'] for item in tested),
            top3=sum(item['evaluation']['top3'] for item in tested),
            top1_rate=fsum(item['evaluation']['top1'] for item in tested)/n if n else None,
            top3_rate=fsum(item['evaluation']['top3'] for item in tested)/n if n else None,
            loss_per_match=fsum(item['evaluation']['loss'] for item in tested)/(n*size) if n else None,
            days=sorted({item['row']['day'].isoformat() for item in tested}),
            group_ids=[item['row']['id'] for item in group]))
    return dict(counts=counts, unique_matches=len(all_matches), evaluated_unique_matches=len(settled_matches),
                by_method_size=output,
                interpretation='Les taux portent sur les groupes évalués de chaque taille, méthode, version et politique. '
                               'Les groupes peuvent partager des matchs ; ils ne sont pas des observations indépendantes.')
