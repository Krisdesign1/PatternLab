"""Exact interpretation of a frozen joint forecast, without fitting or I/O.

The ranking/distribution is validated with the existing bounded algorithms; the
3**N outcome space is counted, never enumerated. This module does not filter group
sizes, change probabilities, choose matches or infer a model from current data.
"""
from math import fsum

import prospective_models as models
from saved_model_comparison import marginals
import scenario_proposal_archive as proposal_archive


VERSION = 'frozen-group-probability-diagnostics-v1'


def _assumptions(distribution):
    kind = distribution['type']
    if kind == 'block_product':
        nested = [text for component in distribution['components']
                  for text in _assumptions(component['method']['distribution'])]
        return list(dict.fromkeys(['Indépendance supposée entre les blocs de compétitions.']+nested))
    return {
        'independent':['Indépendance supposée entre les résultats des matchs.'],
        'frequency':['Répétitions conjointes observées, lissées par des probabilités indépendantes par position.'],
        'corrected':['Probabilités des clubs indépendantes au départ, corrigées par les répétitions conjointes.'],
        'context_mixture':['Base indépendante des clubs mélangée aux scénarios conjoints des contextes voisins.'],
    }[kind]


def inspect(method):
    """Return the top scenario, exact Top3/rest mass, and each leg's marginal.

    ``method`` must be the full saved method payload, not a displayed ranking
    alone. Exact means exact under its saved distribution and assumptions, not a
    calibrated real-world certainty or a statement about model quality.
    """
    if not isinstance(method,dict) or not isinstance(method.get('distribution'),dict):
        raise ValueError('La distribution figée complète est nécessaire.')
    size=method['distribution'].get('size')
    proposal_archive.validate_method(method,size)
    ranking=[dict(sequence=list(row['sequence']),probability=float(row['probability']))
             for row in method['ranking']]
    reference=method.get('reference_ranking', method['ranking'][:3])
    top3=fsum(row['probability'] for row in reference)
    if not 0 <= top3 <= 1+1e-9:
        raise ValueError('La masse du top 3 est invalide.')
    top3=min(top3,1.0)
    # Marginal diagnostics keep the original frozen model contract. Extra
    # explicitly proposed choices never change the historical Top3 metric.
    positions=marginals(dict(method, ranking=reference))
    top1=ranking[0]
    return dict(version=VERSION,size=size,possible_scenarios=3**size,
        distribution_type=method['distribution']['type'],
        top1_sequence=top1['sequence'],top1_probability=top1['probability'],
        ranking=ranking,top3_probability=top3,remaining_probability=1-top3,
        proposed_count=len(ranking),proposed_probability=fsum(row['probability'] for row in ranking),
        top1_match_probabilities=[dict(position=index+1,result=label,probability=positions[index][label])
                                  for index,label in enumerate(top1['sequence'])],
        assumptions=_assumptions(method['distribution']),
        interpretation='Masses exactes du modèle figé; pas une garantie de réalisation ni une validation prédictive.',
        size_warning='Une probabilité par match ne vaut pas la probabilité du scénario entier. '
                     'Les probabilités de scénarios de tailles différentes ne se comparent pas directement.')


def inspect_methods(methods):
    """Keep unavailable/damaged methods explicit without hiding valid methods."""
    if not isinstance(methods,dict):
        raise ValueError('Les méthodes figées doivent être fournies.')
    diagnostics,unavailable={},{}
    for name,payload in methods.items():
        try:
            diagnostics[name]=inspect(payload)
        except (ValueError,TypeError,KeyError,AttributeError,OverflowError,IndexError):
            unavailable[name]='Distribution figée absente, incompatible ou incohérente.'
    return dict(methods=diagnostics,unavailable=unavailable)
