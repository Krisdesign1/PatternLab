"""Native, read-only weekly follow-up from an already loaded protocol report."""
import table_views
from datetime import datetime
import json
from zoneinfo import ZoneInfo

import streamlit as st

from causal_confirmation_followup import build


METHOD_NAMES = {'candidate': 'Candidat', 'clubs': 'Clubs', 'frequency': 'Fréquences du championnat'}
REASON_NAMES = {
    'open_week': 'Semaine encore ouverte',
    'unknown_census': 'Recensements incomplets',
    'capture_window_not_closed': 'Fenêtres de capture non clôturées',
    'unresolved_results': 'Résultats en attente ou non évaluables',
    'no_evaluable_results': 'Aucun résultat admissible évalué',
}
STATUS_NAMES = {
    'settled': 'Évalués', 'pending': 'En attente', 'unresolved': 'Non évaluables',
    'void': 'Annulés du suivi', 'late_capture': 'Captures tardives',
    'missing_ack': 'Accusés absents', 'unattested_capture': 'Captures non attestées',
    'inadmissible_capture': 'Autres captures non admissibles',
}


def _day(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).strftime('%d/%m/%Y')


def _cutoff(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(
        ZoneInfo('Africa/Kinshasa')).strftime('%d/%m/%Y à %H:%M')


def _weekly_table(weeks):
    return [{
        'Début de semaine UTC': _day(row['week_start']),
        'Fin exclue UTC': _day(row['week_end']),
        'Recensements conservés / 7': row['anchored_days'],
        'Fenêtres clôturées / 7': row['closed_days'],
        'Cibles recensées': row['targets_known'],
        'Captures conservées': row['captures_known'],
        'Cibles connues sans capture': row['missing_captures_known'],
        'Couverture des cibles connues (%)': row['capture_coverage']*100
            if row['capture_coverage'] is not None else None,
        'Mêmes matchs évalués': row['counts']['evaluated'],
        'Prévisions hors bilan': row['counts']['excluded'],
        'Admissible pour l’intervalle final': 'Oui' if row['eligible'] else 'Non',
        'Motifs': ' · '.join(REASON_NAMES.get(reason, reason) for reason in row['reasons']) or 'Conditions remplies',
    } for row in weeks]


def _metrics_table(metrics, *, week=None):
    output = []
    for method, row in metrics.items():
        value = {'Méthode': METHOD_NAMES[method], 'Mêmes matchs évalués': row['count'],
                 'Erreur logarithmique ↓': row['logloss'], 'Brier ↓': row['brier'],
                 'Choix corrects (%)': row['accuracy']*100}
        if week is not None:
            value = {'Début de semaine UTC': _day(week), **value}
        output.append(value)
    return output


def render(protocol):
    st.subheader('Suivi hebdomadaire', icon=':material/date_range:')
    st.caption('Suivre les captures et comparer les méthodes sur les mêmes rencontres, '
               'avec le candidat et les règles conservés avant le début de cette période.')
    try:
        value = build(protocol)
    except (ValueError, KeyError, TypeError) as error:
        st.error(f'Suivi hebdomadaire impossible : {error}')
        return
    if not value['registered']:
        st.info('Ce protocole ne possède pas de suivi de huit semaines préenregistré. '
                'Ses prévisions et ses résultats restent consultables dans les autres vues.')
        return

    plan, coverage, counts = value['plan'], value['coverage'], value['counts']
    table_views.dataframe([{
        'Protocole': value['protocol_id'],
        'Début inclus UTC': _day(plan['confirmation_start']),
        'Fin exclue UTC': _day(plan['confirmation_end']),
        'Coupure fixe des résultats (Kinshasa)': _cutoff(plan['result_cutoff_utc']),
        'Comparaison principale': 'Erreur logarithmique du candidat moins Clubs',
    }], hide_index=True, width='stretch', key='confirmation_followup_identity', table_id='causal_confirmation_followup_panel:render:dataframe:1')
    table_views.dataframe([{'Suivi': label, 'Nombre': number} for label, number in (
        ('Journées prévues', coverage['registered_days']),
        ('Recensements conservés', coverage['anchored_days']),
        ('Fenêtres de capture clôturées', coverage['closed_days']),
        ('Journées non recensées, y compris à venir', coverage['unknown_census_days']),
        ('Cibles recensées', coverage['targets_known']),
        ('Captures conservées', coverage['captures_known']),
        ('Cibles connues sans capture', coverage['missing_captures_known']),
        ('Mêmes matchs évalués', counts['evaluated']),
        ('Prévisions hors bilan', counts['excluded']),
        ('Semaines admissibles pour l’intervalle final', coverage['eligible_weeks']),
    )], hide_index=True, width='stretch', key='confirmation_followup_counts', table_id='causal_confirmation_followup_panel:render:dataframe:2')
    st.caption('Un jour non recensé ne signifie pas zéro match. La couverture porte sur les cibles '
               'effectivement recensées ; les journées futures restent à venir.')

    st.markdown('**Captures et résultats par semaine**')
    table_views.dataframe(_weekly_table(value['weeks']), hide_index=True, width='stretch',
                 key='confirmation_followup_weeks', table_id='causal_confirmation_followup_panel:render:dataframe:3')
    exclusions = [{'Motif': STATUS_NAMES.get(key, key), 'Nombre': number}
                  for key, number in counts['exclusion_reasons'].items() if number]
    if exclusions:
        table_views.dataframe(exclusions, hide_index=True, width='stretch', key='confirmation_followup_exclusions', table_id='causal_confirmation_followup_panel:render:dataframe:4')
    st.caption('Les captures manquées, les scores absents et les résultats administratifs sont comptés '
               'séparément. Ils ne deviennent pas des choix incorrects.')

    if counts['evaluated']:
        st.markdown('**Résultats observés sur les mêmes rencontres**')
        table_views.dataframe(_metrics_table(value['metrics']), hide_index=True, width='stretch',
                     key='confirmation_followup_metrics', table_id='causal_confirmation_followup_panel:render:dataframe:5')
        primary = value['primary']
        table_views.dataframe([{'Comparaison principale': 'Candidat − Clubs',
                       'Mêmes matchs évalués': primary['count'],
                       'Écart logarithmique observé ↓': primary['estimate']}],
                     hide_index=True, width='stretch', key='confirmation_followup_primary', table_id='causal_confirmation_followup_panel:render:dataframe:6')
        st.caption('Un écart logarithmique négatif indique une erreur observée plus faible pour le candidat. '
                   'Ce bilan porte sur les matchs capturés et évaluables, sans extrapolation à tous les matchs programmés.')
        weekly_metrics = [row for week in value['weeks']
                          for row in _metrics_table(week['metrics'], week=week['week_start'])]
        if weekly_metrics:
            table_views.dataframe(weekly_metrics, hide_index=True, width='stretch',
                         key='confirmation_followup_weekly_metrics', table_id='causal_confirmation_followup_panel:render:dataframe:7')
    else:
        st.info('Aucun résultat admissible évalué pour cette période : les erreurs et les taux de réussite restent indisponibles.')

    inference = value['inference']
    st.markdown('**Lecture finale prévue**')
    st.info(inference['reason'])
    table_views.dataframe([{'Coupure des résultats (Kinshasa)': _cutoff(inference['fixed_result_cutoff_utc']),
                   'Semaines admissibles': inference['blocks'],
                   'Semaines requises': inference['min_blocks'],
                   'Intervalle disponible': 'Oui, approximatif' if inference['available'] else 'Non'}],
                 hide_index=True, width='stretch', key='confirmation_followup_inference', table_id='causal_confirmation_followup_panel:render:dataframe:8')
    if inference['available']:
        interval = inference['confidence_interval']
        table_views.dataframe([{'Comparaison': 'Candidat − Clubs · erreur logarithmique',
                       'Niveau nominal (%)': interval['confidence']*100,
                       'Borne basse': interval['lower'], 'Borne haute': interval['upper'],
                       'Nature': 'Intervalle approximatif par blocs hebdomadaires'}],
                     hide_index=True, width='stretch', key='confirmation_followup_interval', table_id='causal_confirmation_followup_panel:render:dataframe:9')
    st.caption('Huit semaines est une condition opérationnelle de ce suivi, sans garantie de puissance statistique. '
               'L’intervalle par semaines est approximatif et suppose des blocs comparables ; des dépendances '
               'peuvent persister entre les semaines. Aucun avantage durable ni remplacement automatique du modèle n’est déclaré.')
    st.caption('Les corrections de résultats reçues après la coupure fixe restent hors de cette lecture finale. '
               'La consultation ne lance ni appel API, ni capture, ni entraînement.')
    st.download_button('Télécharger le suivi hebdomadaire',
                       json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
                       file_name='patternlab_suivi_hebdomadaire_'+value['protocol_id'][:12]+'.json',
                       mime='application/json', key='confirmation_followup_download')
