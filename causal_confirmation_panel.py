"""Read-only confirmation ledger; no capture, API request or fit on display."""
import table_views
from datetime import datetime, timezone
import json
import sqlite3
from zoneinfo import ZoneInfo

import streamlit as st


ZONE = ZoneInfo('Africa/Kinshasa')
PHASE_NAMES = {'awaiting_decision': 'En attente du recensement',
               'between_daily_windows': 'En attente de la prochaine fenêtre quotidienne',
               'capture_window': 'Fenêtre de capture ouverte',
               'window_missed': 'Fenêtre de capture manquée',
               'window_closed': 'Fenêtre de capture terminée',
               'finished': 'Période de capture terminée',
               'not_started': 'Suivi non commencé', 'source_unavailable': 'Source indisponible'}
RESULT_NAMES = {'settled': 'Évalué', 'pending': 'En attente', 'unresolved': 'Non évaluable',
                'void': 'Annulé du suivi', 'late_capture': 'Capture tardive',
                'missing_ack': 'Accusé d’enregistrement absent'}
RESULT_NAMES['unattested_capture']='Capture non attestée'
METHOD_NAMES = {'candidate': 'Candidat', 'clubs': 'Clubs', 'frequency': 'Fréquences du championnat'}
VALUES = ('1st', 'Égal', '2nd')


def _load_report():
    import causal_confirmation
    return causal_confirmation.report()


def _kinshasa(value):
    if value is None:
        return '—'
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(ZONE).strftime('%d/%m/%Y %H:%M:%S')
    except (ValueError, TypeError, AttributeError):
        return str(value)


def _favorite(probabilities):
    if not probabilities:
        return '—'
    return max(VALUES, key=lambda value: probabilities[value])


def _forecast_table(rows):
    output = []
    for row in rows:
        fixture = row['fixture']
        methods = row['methods']
        value = {'Match': fixture['home']+' — '+fixture['away'],
                 'Compétition': ' · '.join(str(v) for v in (fixture.get('country'), fixture.get('competition', fixture['league'])) if v),
                 'Coup d’envoi (Kinshasa)': _kinshasa(fixture['timestamp']),
                 'Capture enregistrée (Kinshasa)': _kinshasa(row.get('captured_at')),
                 '1st (%)': methods['candidate']['1st']*100,
                 'Égal (%)': methods['candidate']['Égal']*100,
                 '2nd (%)': methods['candidate']['2nd']*100,
                 'Favori du candidat': _favorite(methods['candidate']),
                 'Favori Clubs': _favorite(methods['clubs']),
                 'Favori fréquences': _favorite(methods['frequency'])}
        for method in ('clubs', 'frequency'):
            for label in VALUES:
                value[f'{METHOD_NAMES[method]} · {label} (%)'] = methods[method][label]*100
        value.update({'Score à la pause': row.get('ht') if row.get('ht') is not None else '—',
                      'Score à 90 minutes': row.get('ft') if row.get('ft') is not None else '—',
                      'Résultat': row.get('actual') or '—',
                      'Statut': RESULT_NAMES.get(row['status'], row['status']),
                      'Preuve temporelle': row.get('timing_quality', 'Non attestée'),
                      'Motif': ' · '.join(row.get('issues') or [])})
        output.append(value)
    return output


def render():
    st.subheader('Confirmation avant match', icon=':material/verified:')
    st.caption('Suivre les probabilités du candidat et leurs résultats, avec les versions et les heures conservées. '
               'La consultation ne lance ni capture, ni appel API, ni entraînement.')
    with st.form('causal_confirmation_read_form'):
        submitted = st.form_submit_button('Relire le suivi du candidat', key='causal_confirmation_read',
                                           icon=':material/refresh:')
    if submitted:
        st.session_state.pop('causal_confirmation_report', None)
        try:
            st.session_state['causal_confirmation_report'] = _load_report()
        except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as error:
            st.error(f'Lecture impossible : {error}')
    report = st.session_state.get('causal_confirmation_report')
    if report is None:
        st.info('Cliquez sur Relire le suivi du candidat. Aucun historique n’est chargé à l’ouverture de cette vue.')
        return
    protocols = report['protocols']
    st.caption('Relevé du suivi : '+_kinshasa(report['generated_at'])+' · Heures de Kinshasa (UTC+1).')
    if not protocols:
        st.info('Aucun protocole de confirmation n’est enregistré. La consultation n’en crée pas automatiquement.')
    else:
        indexed = {row['id']: row for row in protocols}
        selected = st.selectbox('Protocole suivi', list(indexed), index=len(indexed)-1, key='causal_confirmation_protocol',
                                format_func=lambda value: indexed[value]['confirmation_start'][:10]+' → '
                                +indexed[value]['confirmation_end'][:10]+' · '+value[:12])
        protocol = indexed[selected]
        if not protocol['implementation_compatible']:
            st.warning('Le code installé est incompatible avec ce protocole. Les prévisions conservées restent consultables ; '
                       'ce constat ne justifie aucune nouvelle capture.')
        view = st.segmented_control('Consulter le suivi', ['forecasts', 'diagnostics', 'weekly'],
                                    format_func=lambda value: {'forecasts': 'Prévisions et résultats',
                                                              'diagnostics': 'Qualité des probabilités',
                                                              'weekly': 'Suivi hebdomadaire'}[value],
                                    default='forecasts', required=True, key='causal_confirmation_view')
        if view == 'diagnostics':
            from causal_confirmation_diagnostics_panel import render as render_diagnostics
            render_diagnostics(protocol)
            return
        if view == 'weekly':
            from causal_confirmation_followup_panel import render as render_followup
            render_followup(protocol)
            return
        st.markdown('**'+PHASE_NAMES.get(protocol['phase'], protocol['phase'])+'**')
        table_views.dataframe([{'Protocole': protocol['id'], 'Candidat figé': protocol['candidate_id'],
                       'Enregistrement (Kinshasa)': _kinshasa(protocol['registered_at']),
                       'Début inclus UTC': protocol['confirmation_start'], 'Fin exclue UTC': protocol['confirmation_end'],
                       'Décision quotidienne UTC': f"{protocol['decision_hour_utc']:02d}:00",
                       'Fenêtre de capture (minutes)': protocol['capture_grace_minutes']}],
                     hide_index=True, width='stretch', key='causal_confirmation_identity', table_id='causal_confirmation_panel:render:dataframe:1')
        counters = [('Prévisions conservées', 'forecasts'), ('Captures admissibles', 'admissible'),
                    ('Résultats évalués', 'settled'), ('En attente', 'pending'), ('Non évaluables', 'unresolved'),
                    ('Annulés du suivi', 'void'), ('Captures tardives', 'late'), ('Accusés absents', 'missing_ack')]
        table_views.dataframe([{'Suivi': label, 'Nombre': protocol[key]} for label, key in counters],
                     hide_index=True, width='stretch', key='causal_confirmation_counts', table_id='causal_confirmation_panel:render:dataframe:2')
        if protocol['days']:
            table_views.dataframe([{'Journée UTC': row['day'],
                           'Cibles recensées': str(row['targets']) if row['state'] == 'anchored' and row.get('targets') is not None else 'Non recensé',
                           'Captures': row['captures'],
                           'État du recensement': {'awaiting': 'En attente', 'missed': 'Fenêtre manquée',
                                                   'anchored': 'Recensement conservé',
                                                   'source_unavailable': 'Source indisponible'}.get(row['state'], row['state'])}
                          for row in protocol['days']], hide_index=True, width='stretch', key='causal_confirmation_days', table_id='causal_confirmation_panel:render:dataframe:3')
        if protocol['phase'] == 'awaiting_decision':
            st.info('Le recensement est encore à venir. Aucun nombre de matchs admissibles n’est annoncé pour cette décision.')
        elif protocol['phase'] == 'window_missed':
            st.warning('La fenêtre de capture a été manquée. Une prévision passée ne sera pas recréée après coup.')
        rows = _forecast_table(protocol['rows'])
        if protocol.get('attempts'):
            table_views.dataframe([{'Contrôle (Kinshasa)':_kinshasa(row['observed_at_utc']),
                           'Journée UTC':row.get('day','—'),'Match':row.get('match_id','—'),
                           'Étape':row['kind'],'Motif':row.get('reason','Enregistrement conservé')}
                          for row in protocol['attempts']],hide_index=True,width='stretch',
                         key='causal_confirmation_attempts', table_id='causal_confirmation_panel:render:dataframe:4')
        if rows:
            table_views.dataframe(rows, hide_index=True, width='stretch', key='causal_confirmation_forecasts',
                         column_config={name: st.column_config.NumberColumn(format='%.2f')
                                        for name in rows[0] if name.endswith('(%)')}, table_id='causal_confirmation_panel:render:dataframe:5')
        else:
            st.info('Aucune prévision conservée pour ce protocole au dernier relevé.')
        if protocol['metrics']:
            table_views.dataframe([{'Méthode': METHOD_NAMES.get(name, name), 'Mêmes matchs évalués': value['count'],
                           'Erreur logarithmique ↓': value['logloss'], 'Brier ↓': value['brier'],
                           'Choix corrects (%)': value['accuracy']*100}
                          for name, value in protocol['metrics'].items()], hide_index=True, width='stretch',
                         key='causal_confirmation_metrics', table_id='causal_confirmation_panel:render:dataframe:6')
            st.caption('Comparaison descriptive sur les mêmes matchs évaluables. Les effectifs et les dépendances '
                       'doivent être examinés avant de conclure à un avantage prédictif.')
        else:
            st.info('Aucun résultat admissible évalué : aucune précision ni amélioration ne peut encore être mesurée.')
        st.caption('Les résultats absents, administratifs ou inexploitables ne deviennent pas des pertes. '
                   'Une capture tardive ou sans accusé d’enregistrement reste distincte d’une prévision perdue. '
                   'La preuve de capture avant coup d’envoi ne certifie pas la publication du fournisseur à l’heure fixe de décision.')
    st.download_button('Télécharger le suivi de confirmation', json.dumps(report, ensure_ascii=False, indent=2),
                       file_name='patternlab_confirmation.json', mime='application/json', key='causal_confirmation_download')
