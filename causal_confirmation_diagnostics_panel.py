"""Native, read-only probability diagnostics from the already loaded ledger."""
import table_views
import json

import streamlit as st

from causal_confirmation_diagnostics import build


METHOD_NAMES = {'candidate': 'Candidat', 'clubs': 'Clubs', 'frequency': 'Fréquences du championnat'}
STATUS_NAMES = {'settled': 'Évalués', 'pending': 'En attente', 'unresolved': 'Non évaluables',
                'void': 'Annulés du suivi', 'late_capture': 'Captures tardives',
                'missing_ack': 'Accusés absents', 'unattested_capture': 'Captures non attestées',
                'inadmissible_capture': 'Autres captures non admissibles'}


def _metrics_table(metrics):
    return [{'Méthode': METHOD_NAMES[method], 'Mêmes matchs évalués': row['count'],
             'Erreur logarithmique ↓': row['logloss'], 'Brier ↓': row['brier'],
             'Choix corrects (%)': row['accuracy']*100} for method, row in metrics.items()]


def _deltas_table(deltas):
    return [{'Référence': METHOD_NAMES[method], 'Mêmes matchs évalués': row['count'],
             'Écart logarithmique ↓': row['logloss'], 'Écart Brier ↓': row['brier'],
             'Écart de réussite (points) ↑': row['accuracy']*100 if row['accuracy'] is not None else None}
            for method, row in deltas.items()]


def _bins_table(bins):
    return [{'Pourcentages annoncés': f"{round(row['lower']*100)} à {round(row['upper']*100)} %"
             + (' inclus' if row['upper_inclusive'] else ' exclu'),
             'Matchs évalués': row['count'],
             'Probabilité moyenne (%)': row['mean_probability']*100 if row['mean_probability'] is not None else None,
             'Fréquence observée (%)': row['observed_frequency']*100 if row['observed_frequency'] is not None else None,
             'Observé − annoncé (points)': row['gap']*100 if row['gap'] is not None else None}
            for row in bins]


def _groups_table(groups, key):
    output = []
    for group in groups:
        for method, metrics in group['metrics'].items():
            output.append({('Journée UTC' if key == 'day' else 'Identifiant de compétition'): group[key],
                           'Méthode': METHOD_NAMES[method], 'Matchs conservés': group['counts']['forecasts'],
                           'Mêmes matchs évalués': metrics['count'], 'Exclus du bilan': group['counts']['excluded'],
                           'Erreur logarithmique ↓': metrics['logloss'], 'Brier ↓': metrics['brier'],
                           'Choix corrects (%)': metrics['accuracy']*100})
        if not group['metrics']:
            output.append({('Journée UTC' if key == 'day' else 'Identifiant de compétition'): group[key],
                           'Méthode': 'Aucun résultat admissible', 'Matchs conservés': group['counts']['forecasts'],
                           'Mêmes matchs évalués': 0, 'Exclus du bilan': group['counts']['excluded']})
    return output


def render(protocol):
    st.subheader('Qualité des probabilités', icon=':material/analytics:')
    st.caption('Mesurer les pourcentages conservés avant match. Cette vue utilise le relevé déjà chargé ; '
               'elle ne récupère aucune donnée et ne modifie pas le modèle.')
    try:
        value = build(protocol)
    except (ValueError, KeyError, TypeError) as error:
        st.error(f'Bilan impossible : {error}')
        return
    counts, sample = value['counts'], value['sample']
    table_views.dataframe([{'Suivi': label, 'Nombre': count} for label, count in (
        ('Prévisions conservées', counts['forecasts']), ('Mêmes matchs évalués', counts['evaluated']),
        ('Prévisions hors bilan', counts['excluded']), ('Journées UTC avec résultats évalués', sample['evaluated_utc_days']),
        ('Compétitions avec résultats évalués', sample['evaluated_leagues']))],
        hide_index=True, width='stretch', key='confirmation_diagnostics_counts', table_id='causal_confirmation_diagnostics_panel:render:dataframe:1')
    st.caption('Les prévisions en attente ou exclues ne comptent pas comme des choix incorrects. '
               'Chaque match compte une fois dans ce protocole ; les protocoles restent séparés.')
    exclusions = [{'Motif': STATUS_NAMES[key], 'Nombre': number}
                  for key, number in counts['exclusion_reasons'].items() if number]
    if exclusions:
        table_views.dataframe(exclusions, hide_index=True, width='stretch', key='confirmation_diagnostics_exclusions', table_id='causal_confirmation_diagnostics_panel:render:dataframe:2')
    if not counts['evaluated']:
        st.info('Aucun résultat admissible évalué : les taux de réussite et les écarts entre méthodes restent indisponibles.')
    else:
        st.markdown('**Comparaison sur les mêmes rencontres**')
        table_views.dataframe(_metrics_table(value['metrics']), hide_index=True, width='stretch',
                     key='confirmation_diagnostics_metrics', table_id='causal_confirmation_diagnostics_panel:render:dataframe:3')
        st.caption('Une erreur logarithmique ou un Brier plus bas indique de meilleures probabilités sur cet échantillon. '
                   'Le Brier est la somme sur les trois résultats. La réussite porte sur le résultat favori.')
        table_views.dataframe(_deltas_table(value['paired_deltas']), hide_index=True, width='stretch',
                     key='confirmation_diagnostics_deltas', table_id='causal_confirmation_diagnostics_panel:render:dataframe:4')
        st.caption('Écart = candidat moins référence. Un écart négatif est favorable pour les deux erreurs ; '
                   'un écart positif est favorable pour la réussite. Ce sont des observations, sans test de significativité.')
        st.markdown('**Les pourcentages annoncés correspondent-ils aux résultats ?**')
        method = st.selectbox('Méthode à examiner', list(METHOD_NAMES), format_func=METHOD_NAMES.get,
                              key='confirmation_diagnostics_method')
        label = st.segmented_control('Probabilités à contrôler', ['1st', 'Égal', '2nd', 'favorite'],
                                     format_func=lambda key: 'Résultat favori' if key == 'favorite' else key,
                                     default='favorite', required=True, key='confirmation_diagnostics_label')
        calibration = value['calibration'][method]
        bins = calibration['favorite_confidence'] if label == 'favorite' else calibration['classes'][label]
        table_views.dataframe(_bins_table(bins), hide_index=True, width='stretch', key='confirmation_diagnostics_bins', table_id='causal_confirmation_diagnostics_panel:render:dataframe:5')
        st.caption('Les tranches sont fixes. Un tiret signifie qu’aucun match n’a été évalué dans cette tranche. '
                   'Exemple : une fréquence observée de 50 % face à une moyenne annoncée de 65 % donne −15 points. '
                   'Quelques matchs ne suffisent pas à qualifier une tranche de fiable.')
        table_views.dataframe([{'Résultat réel': label, 'Matchs évalués': number}
                      for label, number in sample['actual_classes'].items()],
                     hide_index=True, width='stretch', key='confirmation_diagnostics_classes', table_id='causal_confirmation_diagnostics_panel:render:dataframe:6')
    for key, groups, title in [('day', value['by_utc_day'], 'Bilan par journée UTC'),
                                ('league', value['by_league'], 'Bilan par compétition')]:
        if groups:
            st.markdown('**'+title+'**')
            table_views.dataframe(_groups_table(groups, key), hide_index=True, width='stretch',
                         key='confirmation_diagnostics_'+key, table_id='causal_confirmation_diagnostics_panel:render:dataframe:7')
    if value['by_league']:
        st.caption('Le bilan par compétition décrit les résultats observés. Les règles de sélection restent celles enregistrées avant match.')
    if sample['registered_one_day_pilot']:
        st.info('Ce protocole couvre une seule journée UTC. Il ne mesure pas la stabilité des résultats entre plusieurs journées.')
    st.caption('Aucun intervalle de confiance ni test de significativité n’est calculé ici. '
               'Les matchs d’un championnat peuvent partager des facteurs communs. '
               'L’amélioration durable et le remplacement du modèle ne sont pas déclarés par ce bilan.')
    st.caption('Les effectifs historiques des profils ne figurent pas dans ce relevé compact : ils restent non renseignés dans ce bilan. '
               'Ils ne sont pas déduits du niveau des pourcentages.')
    st.caption('L’enregistrement avant match et la disponibilité des sources à l’heure de décision sont deux contrôles distincts. '
               'La reconstruction historique reste conditionnelle aux heures d’import conservées.')
    st.download_button('Télécharger le bilan des probabilités', json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
                       file_name='patternlab_bilan_probabilites_'+str(protocol['id'])[:12]+'.json',
                       mime='application/json', key='confirmation_diagnostics_download')
