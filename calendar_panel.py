"""Browse API-Football display data without passing scores into model fixtures."""
from datetime import datetime, timezone
from time import monotonic
from zoneinfo import ZoneInfo

import streamlit as st

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
    import journal_tracking
    import prediction_journal
    try:
        journal_tracking.reconcile_local()
    except (prediction_journal.JournalError, ValueError) + sources.ERRORS as error:
        st.warning('Calendrier actualisé ; rapprochement du journal à vérifier : ' + str(error))


def maybe_refresh(source, day, today):
    """Only opt-in callers invoke this; reserve failures as well as successes."""
    context = (source, day.isoformat())
    if day != today:
        st.caption('Le suivi automatique est actif uniquement pour la journée en cours.')
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
    return [{'Heure': datetime.fromisoformat(row['timestamp']).replace(tzinfo=timezone.utc).astimezone(ZONE).strftime('%H:%M'),
             'Pays': row['country'], 'Compétition': row['competition'],
             'Domicile': row['home'], 'Score': row['score'], 'Extérieur': row['away'],
             'Statut': row['status_label'], 'Minute': row['minute'],
             'Mi-temps': row['halftime_score'], 'À 90 minutes': row['regulation_score']}
            for row in rows]


def render_calendar(snapshot, day, league):
    rows = display_rows(snapshot, day)
    st.subheader('Le calendrier de la journée', icon=':material/scoreboard:')
    status = st.segmented_control('Afficher les matchs', ['Tous', 'En cours', 'À venir', 'Terminés', 'Autres'],
                                   default='Tous', selection_mode='single', required=True, key='calendar_status')
    query = st.text_input('Rechercher un club, un pays ou une compétition', type='search',
                          placeholder='Ex. Arsenal, France, Ligue 1', key='calendar_query')
    filtered = calendar_data.filter_rows(rows, None if league == ALL else league, status, query)
    if rows:
        live = sum(row['status_group'] == 'live' for row in rows)
        st.caption(f'{len(filtered)} matchs affichés · {len(rows)} rencontres dans le calendrier · '
                   f'{live} en cours au dernier relevé. Les filtres ne limitent pas les données reçues.')
    if league == ALL:
        if filtered:
            st.dataframe(table_rows(filtered), hide_index=True, width='stretch', height=550,
                         key='calendar_table')
            st.caption('Toutes les lignes sont consultables en faisant défiler le tableau. '
                       'Choisissez une compétition ci-dessus pour préparer un groupe de prédiction.')
        else:
            st.info('Aucun match ne correspond à ces filtres dans le relevé local. '
                    'Utilisez Actualiser pour demander les derniers scores.')
    return filtered, status, query
