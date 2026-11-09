"""Browse API-Football display data without passing scores into model fixtures."""
import table_views
from datetime import datetime, timezone
from time import monotonic
from zoneinfo import ZoneInfo

import streamlit as st
import admin_access

import calendar_data
import workspace_sources as sources

ALL = '__all__'
ZONE = ZoneInfo('Africa/Kinshasa')


def mark_attempt(source, day):
    context = (source, day.isoformat())
    attempts = dict(st.session_state.get('calendar_attempts', {}))
    attempts[context] = monotonic()
    st.session_state['calendar_attempts'] = attempts
    st.session_state.pop('calendar_auto_error', None)


def update_journal():
    admin_access.require_admin()
    import journal_tracking
    import prediction_journal
    try:
        journal_tracking.reconcile_local()
    except (prediction_journal.JournalError, ValueError) + sources.ERRORS as error:
        st.warning('Calendrier actualisé ; rapprochement du journal à vérifier : ' + str(error))


def maybe_refresh(source, day, today):
    """Only opt-in callers invoke this; reserve failures as well as successes."""
    if not admin_access.can_admin():
        return
    admin_access.require_admin()
    context = (source, day.isoformat())
    if day < today:
        st.caption('Pour une journée passée, utilisez Actualiser pour récupérer les résultats corrigés.')
        return
    error = st.session_state.get('calendar_auto_error')
    if error and error['context'] == context:
        st.warning('Suivi automatique en pause : ' + error['message'] +
                   ' Utilisez Actualiser pour réessayer.')
        return
    previous = st.session_state.get('calendar_attempts', {}).get(context)
    if previous is not None and monotonic() - previous < 60:
        return
    mark_attempt(source, day)
    try:
        sources.refresh_day(source, day)
    except sources.ERRORS as error:
        st.session_state['calendar_auto_error'] = {'context': context, 'message': str(error)}
        st.warning('Suivi automatique en pause : ' + str(error) + ' Utilisez Actualiser pour réessayer.')
    else:
        update_journal()


def display_rows(snapshot, day):
    return calendar_data.day_rows(snapshot.get('matches', []), day)


def table_rows(rows):
    return [{'Date': datetime.fromisoformat(row['timestamp']).replace(tzinfo=timezone.utc).astimezone(ZONE).strftime('%d/%m/%Y'),
             'Heure': datetime.fromisoformat(row['timestamp']).replace(tzinfo=timezone.utc).astimezone(ZONE).strftime('%H:%M'),
             'Pays': row['country'], 'Compétition': row['competition'],
             'Domicile': row['home'], 'Score': row['score'], 'Extérieur': row['away'],
             'Verdict final': [row['final_verdict']],
             'Statut': (row['status_label'] + ' · ' + row['final_verdict_reason']
                        if row['final_verdict'] in ('Non évaluable', 'À vérifier')
                        and row['final_verdict_reason'] != row['status_label'] else row['status_label']),
             'Minute': row['minute'],
             'Mi-temps': row['halftime_score'], 'À 90 minutes': row['regulation_score']}
            for row in rows]


def verdict_column():
    colors = (['#8fc1ff', '#d7b9ff', '#ffc58a'] if st.context.theme.type == 'dark'
              else ['#175da7', '#7040a0', '#934b08'])
    return st.column_config.MultiselectColumn(
        'Verdict final', width=130,
        options=['1st', 'Égal', '2nd', 'En attente', 'En cours', 'Non évaluable', 'À vérifier'],
        color=colors + ['gray', 'gray', 'orange', 'orange'],
        help='Résultat observé : 1st = plus de buts avant la pause ; Égal = autant dans chaque '
             'mi-temps ; 2nd = plus de buts après la pause. Hors prolongations et tirs au but. '
             'Un verdict final nécessite un match terminé et deux scores exploitables.')


def render_calendar(snapshot, day, league):
    rows = display_rows(snapshot, day)
    st.subheader('Le calendrier de la journée', icon=':material/scoreboard:')
    status = st.segmented_control('Afficher les matchs', ['Tous', 'En cours', 'À venir', 'Terminés', 'Autres'],
                                   default='Tous', selection_mode='single', required=True, key='calendar_status',
                                   persist_state='session')
    query = st.text_input('Rechercher un club, un pays ou une compétition', type='search',
                          placeholder='Ex. Arsenal, France, Ligue 1', key='calendar_query',
                          persist_state='session')
    filtered = calendar_data.filter_rows(rows, None if league == ALL else league, status, query)
    if rows:
        live = sum(row['status_group'] == 'live' for row in rows)
        st.caption(f'{len(filtered)} matchs affichés · {len(rows)} rencontres dans le calendrier · '
                   f'{live} en cours au dernier relevé. Les filtres ne limitent pas les données reçues.')
    if league == ALL:
        if filtered:
            table_views.dataframe(table_rows(filtered), hide_index=True, width='stretch', height=550,
                         column_config={'Verdict final': verdict_column()},
                         key='calendar_table', table_id='calendar_panel:render_calendar:dataframe:1')
            st.caption('Verdict final : 1st = plus de buts avant la pause · Égal = autant dans chaque mi-temps · '
                       '2nd = plus de buts après la pause. Hors prolongations et tirs au but.')
            st.caption('Toutes les lignes sont consultables en faisant défiler le tableau. '
                       'Choisissez une compétition ci-dessus pour préparer un groupe de prédiction.')
        else:
            st.info('Aucun match ne correspond à ces filtres dans le relevé local. '
                    'Utilisez Actualiser pour demander les derniers scores.')
    export = st.expander('Exporter le calendrier : une ou plusieurs journées', icon=':material/download:',
                          key='calendar_export_open', on_change='rerun')
    if export.open:
        with export:
            from calendar_export_panel import render_export
            render_export(day, selected_league=league)
    return filtered, status, query
