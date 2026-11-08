"""Explicit local calendar exports, independent of the visible day's pagination."""
import table_views
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import streamlit as st

import api_football as api
import calendar_export_data as data
import calendar_export_formats as formats

ZONE = ZoneInfo('Africa/Kinshasa')
PREPARED = 'calendar_export_prepared'
STATUSES = ['Tous', 'En cours', 'À venir', 'Terminés', 'Autres']


@st.cache_data(ttl=120, max_entries=4, show_spinner=False)
def _competitions(start, end, signature):
    return data.competitions(start, end)


def _period(default_day):
    if st.session_state.get('calendar_export_day') != default_day:
        st.session_state['calendar_export_day'] = default_day
        st.session_state['calendar_export_start'] = default_day
        st.session_state['calendar_export_end'] = default_day
        st.session_state.pop(PREPARED, None)
    st.session_state.setdefault('calendar_export_start', default_day)
    st.session_state.setdefault('calendar_export_end', default_day)
    mode = st.segmented_control('Choisir la période à exporter', ['period', 'days'],
        format_func=lambda value: 'Dates de début et de fin' if value == 'period' else 'Nombre de jours',
        default='period', required=True, key='calendar_export_period_mode', persist_state='session')
    with st.container(horizontal=True):
        if mode == 'period':
            start = st.date_input('Première journée incluse', key='calendar_export_start', format='DD/MM/YYYY',
                                  min_value=date(1900, 1, 1), max_value=date(2100, 12, 31), persist_state='session')
        end = st.date_input('Dernière journée incluse', key='calendar_export_end', format='DD/MM/YYYY',
                            min_value=date(1900, 1, 1), max_value=date(2100, 12, 31), persist_state='session')
        if mode == 'days':
            maximum = (end - date(1900, 1, 1)).days + 1
            current = st.session_state.get('calendar_export_day_count', 7)
            st.session_state['calendar_export_day_count'] = min(maximum, max(1, current))
            count = st.number_input('Nombre de journées', min_value=1, max_value=maximum, step=1,
                                    key='calendar_export_day_count', persist_state='session')
            start = end - timedelta(days=int(count) - 1)
    if start > end:
        st.session_state.pop(PREPARED, None)
        st.warning('La première journée doit précéder ou être égale à la dernière.')
        return None
    count = (end - start).days + 1
    st.caption(f'Du {start:%d/%m/%Y} au {end:%d/%m/%Y}, dates incluses : '
               f'{count} journée(s) · Africa/Kinshasa (UTC+1).')
    return start, end


def render_export(default_day, *, selected_league='__all__'):
    st.markdown('**Exporter le calendrier sur plusieurs journées**')
    st.caption('Les réglages de ce bloc définissent l’export, indépendamment des filtres du calendrier. '
               'Seules les données déjà enregistrées sont lues ; aucune collecte API n’est lancée.')
    period = _period(default_day)
    if period is None:
        return
    start, end = period
    try:
        signature = api.data_signature(api.STORE_PATH)
        choices = _competitions(start, end, signature)
    except (data.ExportDataError, api.ApiFootballError) as error:
        st.warning(str(error))
        return
    indexed = {row['league']: row for row in choices}
    key = 'calendar_export_leagues'
    parent = default_day, selected_league
    if st.session_state.get('calendar_export_parent') != parent or key not in st.session_state:
        st.session_state['calendar_export_parent'] = parent
        st.session_state[key] = [selected_league] if selected_league in indexed else []
    elif key in st.session_state:
        st.session_state[key] = [value for value in st.session_state[key] if value in indexed]
    leagues = st.multiselect('Compétitions à exporter', list(indexed), key=key,
        format_func=lambda value: f"{indexed[value]['country']} · {indexed[value]['competition']}",
        placeholder='Toutes les compétitions de la période',
        help='Laissez vide pour exporter toutes les compétitions disponibles dans la période.', persist_state='session')
    with st.container(horizontal=True):
        status = st.selectbox('Matchs à exporter', STATUSES, key='calendar_export_status', persist_state='session')
        kind = st.selectbox('Format du fichier', ['xlsx', 'csv'], key='calendar_export_format',
                             format_func=lambda value: 'Excel (.xlsx)' if value == 'xlsx' else 'CSV (.csv)',
                             persist_state='session')
    query = st.text_input('Filtrer l’export par club, pays ou compétition', key='calendar_export_query',
                          type='search', placeholder='Facultatif', persist_state='session')
    options = start.isoformat(), end.isoformat(), tuple(sorted(leagues)), status, query.strip(), kind
    prepared = st.session_state.get(PREPARED)
    if prepared and prepared['options'] != options:
        st.session_state.pop(PREPARED, None)
        prepared = None
    if st.button('Préparer l’export du calendrier', icon=':material/download:', type='primary',
                  key='calendar_export_prepare'):
        try:
            with st.spinner('Préparation du fichier à partir des données locales…'):
                report = data.build(start, end, leagues=leagues or None, status=status, query=query.strip(),
                                    now=datetime.now(timezone.utc))
                output = formats.to_xlsx(report) if kind == 'xlsx' else formats.to_csv(report)
                prepared = {'options': options, 'metadata': report['metadata'], 'days': report['days'],
                            'preview': report['rows'][:50], 'count': len(report['rows']),
                            'content': output, 'coverage_csv': formats.to_coverage_csv(report),
                            'filename': f'calendrier_{start.isoformat()}_au_{end.isoformat()}_{(end-start).days+1}jours'}
                st.session_state[PREPARED] = prepared
        except (data.ExportDataError, api.ApiFootballError, ValueError, OSError) as error:
            st.session_state.pop(PREPARED, None)
            st.error('Export indisponible : ' + str(error))
            return
    if not prepared:
        return
    metadata = prepared['metadata']
    extracted = datetime.fromisoformat(metadata['exported_at_utc']).astimezone(ZONE)
    st.success(f"Export préparé : {prepared['count']} match(s) sur {metadata['day_count']} journée(s).")
    st.caption(f'Fichier figé le {extracted:%d/%m/%Y à %H:%M:%S} (Kinshasa). '
               'Les mises à jour reçues ensuite nécessitent une nouvelle préparation.')
    if metadata.get('missing_days'):
        st.warning(f"{len(metadata['missing_days'])} journée(s) sans donnée locale ni relevé de calendrier. "
                   'Consultez le tableau des journées ; cela ne signifie pas qu’aucun match ne s’est joué.')
    if metadata.get('unconfirmed_days'):
        st.caption(f"{len(metadata['unconfirmed_days'])} journée(s) contiennent des rencontres stockées "
                   'sans relevé de calendrier daté ; leur couverture complète n’est pas confirmée.')
    if metadata.get('invalid_records') or metadata.get('unassignable_records'):
        st.caption(f"Rencontres de la période illisibles : {len(metadata.get('invalid_records', []))}. "
                   f"Rencontres sans date attribuable dans la base, hors période : {metadata.get('unassignable_records', 0)}.")
    with st.container(horizontal=True):
        st.download_button('Télécharger Excel' if kind == 'xlsx' else 'Télécharger CSV',
            data=prepared['content'], file_name=prepared['filename'] + '.' + kind,
            mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' if kind == 'xlsx' else 'text/csv',
            on_click='ignore', key='calendar_export_download')
        if kind == 'csv':
            st.download_button('Télécharger le suivi des journées (CSV)', data=prepared['coverage_csv'],
                file_name=prepared['filename'] + '_journees.csv', mime='text/csv',
                on_click='ignore', key='calendar_export_coverage_download')
    st.caption('Excel contient les feuilles Matchs, Journées et Paramètres. '
               'Le CSV conserve les dates et l’heure d’extraction dans ses lignes ; '
               'le fichier de suivi des journées précise la couverture locale.')
    table_views.dataframe(prepared['days'], hide_index=True, width='stretch', height=250,
                 key='calendar_export_days_table', table_id='calendar_export_panel:render_export:dataframe:1')
    if prepared['preview']:
        st.caption(f"Aperçu des {len(prepared['preview'])} premières lignes. "
                   f"Le fichier contient l’ensemble des {prepared['count']} lignes sélectionnées.")
        table_views.dataframe(prepared['preview'], hide_index=True, width='stretch', height=300,
                     key='calendar_export_preview', table_id='calendar_export_panel:render_export:dataframe:2')
