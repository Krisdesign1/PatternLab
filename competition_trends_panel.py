"""Native descriptive competition tables; no prediction or collection action."""
import table_views
from datetime import date, datetime, timezone
from math import ceil
from zoneinfo import ZoneInfo

import streamlit as st

import api_football as api
import competition_trends as trends
from analysis_date import get_selected_day
from halftime_domain import CLASS_ORDER
from ux_theme import render_page_header

ZONE = ZoneInfo('Africa/Kinshasa')
SORTS = {'matches': 'Historique exploitable', '1st': 'Pourcentage de 1st',
         'Égal': 'Pourcentage de Égal', '2nd': 'Pourcentage de 2nd', 'name': 'Nom de compétition'}


@st.cache_data(ttl=300, max_entries=1, show_spinner=False)
def _catalogue():
    return trends.catalogue()


def _signature():
    return api.data_signature(api.STORE_PATH)


@st.cache_data(ttl=300, max_entries=4, show_spinner=False)
def _summary(start, end, seasons, recent, signature, as_of):
    return trends.build(start, end, seasons=seasons or None, recent=recent,
                        now=datetime.fromisoformat(as_of))


@st.cache_data(ttl=120, max_entries=8, show_spinner=False)
def _details(league, start, end, seasons, signature, as_of, offset, limit):
    return trends.details(league, start, end, seasons=seasons or None,
                          now=datetime.fromisoformat(as_of), offset=offset, limit=limit)


def _result_column(label):
    colors = (['#8fc1ff', '#d7b9ff', '#ffc58a'] if st.context.theme.type == 'dark'
              else ['#175da7', '#7040a0', '#934b08'])
    return st.column_config.MultiselectColumn(label, options=list(CLASS_ORDER), color=colors)


def _select(rows, country, query, minimum, sort):
    search = query.strip().casefold()
    selected = [row for row in rows if row['matches'] >= minimum
                and (country is None or row['country'] == country)
                and (not search or search in f"{row['country']} {row['competition']}".casefold())]
    alpha = lambda row: (row['country'].casefold(), row['competition'].casefold(), str(row['league']))
    if sort in CLASS_ORDER:
        return sorted(selected, key=lambda row: (-(row['percentages'][sort]
                      if row['percentages'][sort] is not None else -1), -row['matches'], *alpha(row)))
    return sorted(selected, key=alpha if sort == 'name' else lambda row: (-row['matches'], *alpha(row)))


def _table(rows):
    def recent_leaders(row):
        if not row['recent_matches']:
            return []
        maximum = max(row['recent_counts'].values())
        return [value for value in CLASS_ORDER if row['recent_counts'][value] == maximum]

    return [{'Pays': row['country'], 'Compétition': row['competition'],
             'Matchs exploitables': row['matches'],
             **{f'{value} (%)': row['percentages'][value] for value in CLASS_ORDER},
             'Tendance dominante': list(row['leaders']),
             'Matchs récents': row['recent_matches'], 'Tendance récente': recent_leaders(row),
             'Résultats exclus': row['excluded']}
            for row in rows]


def _comparison(row):
    return [{'Résultat': [value], 'Sur la période (%)': row['percentages'][value],
             'Matchs récents': row['recent_matches'], 'Récents (%)': row['recent_percentages'][value],
             'Matchs précédents': row['previous_matches'], 'Précédents (%)': row['previous_percentages'][value],
             'Écart (points)': row['delta_points'][value]} for value in CLASS_ORDER]


def _render_matches(league, request, report):
    if not st.toggle('Voir les rencontres utilisées et les exclusions', key='trends_show_matches'):
        return
    with st.container(horizontal=True):
        limit = st.selectbox('Rencontres par page', [10, 25, 50, 100], index=2, key='trends_match_limit')
        detail = _details(league, *request[:3], request[4], request[5], 0, limit)
        total = detail['total']
        pages = max(1, ceil(total / limit))
        context = (league, request, limit)
        if st.session_state.get('trends_match_context') != context:
            st.session_state['trends_match_context'] = context
            st.session_state['trends_match_page'] = 1
        page = st.selectbox('Page des rencontres', range(1, pages + 1), key='trends_match_page',
                            format_func=lambda value: f'Page {value} sur {pages}')
    if page > 1:
        detail = _details(league, *request[:3], request[4], request[5], (page - 1) * limit, limit)
    if detail.get('revision') != report.get('revision'):
        st.caption('La base a reçu de nouveaux imports depuis le calcul du tableau. '
                   'Utilisez « Afficher et actualiser » pour recalculer les tendances.')
    values = [{'Coup d’envoi (Kinshasa)': datetime.fromisoformat(row['kickoff_local']).strftime('%d/%m/%Y %H:%M'),
               'Saison': row['season'], 'Domicile': row['home'], 'Extérieur': row['away'],
               'Mi-temps': row['ht'] or 'Non reçu', 'À 90 minutes': row['ft'] or 'Non reçu',
               'Verdict final': [row['value']] if row['included'] else [],
               'Compté dans la tendance': row['included'], 'Motif': row['reason']}
              for row in detail['rows']]
    if values:
        table_views.dataframe(values, hide_index=True, width='stretch', height=450, key='trends_matches',
            column_config={'Verdict final': _result_column('Verdict final'),
                'Saison': st.column_config.NumberColumn(format='%d')}, table_id='competition_trends_panel:_render_matches:dataframe:1')
    st.caption(f'{total} rencontre(s) consultable(s) · Page {page}/{pages}. '
               'Les matchs non terminés ne sont pas comptés parmi les résultats exclus.')


def render():
    render_page_header('Tendances des compétitions',
        'Comparer les résultats réels par mi-temps et leur évolution dans chaque compétition.',
        icon=':material/query_stats:')
    st.caption('Source : API-Football · Historique local corrigé à la consultation. '
               'Ces fréquences observées ne sont pas les probabilités du prochain match.')
    try:
        catalogue = _catalogue()
    except (trends.TrendsError, api.ApiFootballError) as error:
        st.warning(str(error))
        return
    selected_day = get_selected_day()
    if st.session_state.get('trends_selected_day') != selected_day:
        first = date.fromisoformat(catalogue['first_day']) if catalogue['first_day'] else selected_day
        st.session_state['trends_start'] = min(selected_day, max(first, date(selected_day.year, 1, 1)))
        st.session_state['trends_end'] = selected_day
        st.session_state['trends_selected_day'] = selected_day
        st.session_state.pop('trends_request', None)
    with st.form('competition_trends_filters'):
        with st.container(horizontal=True):
            start = st.date_input('Début de la période', key='trends_start', format='DD/MM/YYYY')
            end = st.date_input('Fin de la période incluse', key='trends_end', format='DD/MM/YYYY')
        with st.container(horizontal=True):
            seasons = st.multiselect('Saisons API-Football', catalogue['seasons'],
                placeholder='Toutes les saisons disponibles', key='trends_seasons',
                help='Une sélection vide inclut toutes les saisons dans la période choisie.')
            recent = st.selectbox('Matchs récents par compétition', [10, 20, 30, 50, 100], index=2,
                key='trends_recent', help='Les N derniers résultats exploitables, comparés aux matchs plus anciens de la période.')
        submitted = st.form_submit_button('Afficher et actualiser', icon=':material/refresh:', type='primary')
    if submitted or 'trends_request' not in st.session_state:
        if start > end:
            st.warning('Le début de la période doit précéder sa fin.')
            return
        try:
            signature = _signature()
        except api.ApiFootballError as error:
            st.warning(str(error))
            return
        st.session_state['trends_request'] = (start, end, tuple(sorted(seasons)), recent,
            signature, datetime.now(timezone.utc).replace(second=0, microsecond=0).isoformat())
    request = st.session_state['trends_request']
    try:
        with st.spinner('Lecture des tendances locales…'):
            report = _summary(*request)
    except (trends.TrendsError, api.ApiFootballError) as error:
        st.warning(str(error))
        return
    as_of = datetime.fromisoformat(request[5]).astimezone(ZONE)
    st.caption(f'Période appliquée : {request[0]:%d/%m/%Y} → {request[1]:%d/%m/%Y} · '
               f'Coups d’envoi antérieurs au {as_of:%d/%m/%Y à %H:%M} (Kinshasa). '
               'Les résultats corrigés actuellement enregistrés sont utilisés ; '
               'ce tableau ne reconstitue pas les informations connues avant un ancien match.')
    with st.container(horizontal=True):
        st.metric('Compétitions', report['totals']['competitions'])
        st.metric('Résultats exploitables', report['totals']['matches'])
        st.metric('Résultats exclus', report['totals']['excluded'])
    if not report['rows']:
        st.info('Aucune rencontre disponible dans cette période et ces saisons.')
        return
    with st.container(horizontal=True):
        countries = sorted({row['country'] for row in report['rows']}, key=str.casefold)
        country = st.selectbox('Pays', [None] + countries, format_func=lambda value: value or 'Tous les pays',
                               key='trends_country')
        sort = st.selectbox('Trier par', list(SORTS), format_func=SORTS.get, key='trends_sort')
        minimum = st.number_input('Minimum de matchs exploitables', min_value=0, value=0, step=1,
                                   key='trends_minimum')
    query = st.text_input('Rechercher une compétition ou un pays', key='trends_search', type='search')
    rows = _select(report['rows'], country, query, minimum, sort)
    if not rows:
        st.info('Aucune compétition ne correspond à ces filtres.')
        return
    colors = ['#8fc1ff', '#d7b9ff', '#ffc58a'] if st.context.theme.type == 'dark' else ['#175da7', '#7040a0', '#934b08']
    columns = {f'{value} (%)': st.column_config.ProgressColumn(format='%.1f%%', min_value=0,
               max_value=100, color=color) for value, color in zip(CLASS_ORDER, colors)}
    columns['Tendance dominante'] = _result_column('Tendance dominante')
    columns['Tendance récente'] = _result_column('Tendance récente')
    columns['Compétition'] = st.column_config.TextColumn(width='medium', pinned=True)
    table_views.dataframe(_table(rows), hide_index=True, width='stretch', height=500, key='trends_summary', column_config=columns, table_id='competition_trends_panel:render:dataframe:1')
    st.caption(f'{len(rows)} compétition(s) affichée(s). Les pourcentages reposent sur les matchs exploitables ; '
               'un petit effectif reste visible et ne démontre pas une tendance stable. '
               'En cas d’égalité en tête, tous les résultats concernés sont affichés.')
    indexed = {row['league']: row for row in rows}
    if st.session_state.get('trends_competition') not in indexed:
        st.session_state['trends_competition'] = rows[0]['league']
    league = st.selectbox('Compétition à examiner', list(indexed), key='trends_competition',
        format_func=lambda value: f"{indexed[value]['country']} · {indexed[value]['competition']}")
    row = indexed[league]
    st.subheader('Évolution récente', icon=':material/trending_up:')
    if row['first_day'] and row['last_day']:
        st.caption(f"Résultats exploitables du {date.fromisoformat(row['first_day']):%d/%m/%Y} "
                   f"au {date.fromisoformat(row['last_day']):%d/%m/%Y}. "
                   f"{row['not_finished']} rencontre(s) sans résultat sportif final, hors pourcentages.")
    st.caption(f"Les {row['recent_matches']} derniers matchs exploitables sont comparés aux "
               f"{row['previous_matches']} précédents de la même période. Aucun match n’appartient aux deux groupes.")
    table_views.dataframe(_comparison(row), hide_index=True, width='stretch', key='trends_comparison',
        column_config={'Résultat': _result_column('Résultat'),
            **{name: st.column_config.NumberColumn(format='%.1f%%') for name in
               ('Sur la période (%)', 'Récents (%)', 'Précédents (%)')},
            'Écart (points)': st.column_config.NumberColumn(format='%+.1f',
                help='Fréquence récente moins fréquence des matchs précédents, en points de pourcentage.')}, table_id='competition_trends_panel:render:dataframe:2')
    if not row['previous_matches']:
        st.info('Pas de matchs antérieurs au groupe récent dans cette période : l’écart reste indisponible.')
    try:
        _render_matches(league, request, report)
    except (trends.TrendsError, api.ApiFootballError) as error:
        st.warning(str(error))
    if report['exclusion_reasons']:
        st.subheader('Exclusions de la période', icon=':material/info:')
        st.caption('Tous pays et toutes compétitions de la période appliquée, avant les filtres du tableau.')
        table_views.dataframe([{'Motif': reason, 'Rencontres': count} for reason, count in
                      sorted(report['exclusion_reasons'].items(), key=lambda item: (-item[1], item[0]))],
                     hide_index=True, width='stretch', table_id='competition_trends_panel:render:dataframe:3')
