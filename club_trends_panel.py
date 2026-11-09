"""Club trends from corrected local results, without collecting or predicting."""
import table_views
import csv
from datetime import date, datetime, time, timedelta, timezone
from io import StringIO
from math import ceil
from zoneinfo import ZoneInfo

import streamlit as st

import api_football as api
import club_trends as trends
import club_trends_day as day_trends
from club_recent_sequences import role_columns
from analysis_date import get_selected_day
from halftime_domain import CLASS_ORDER
from ux_theme import render_page_header

ZONE = ZoneInfo('Africa/Kinshasa')
ROLES = {'all': 'Tous les matchs', 'home': 'À domicile', 'away': 'À l’extérieur'}
SORTS = {'matches': 'Historique exploitable', '1st': 'Pourcentage de 1st',
         'Égal': 'Pourcentage de Égal', '2nd': 'Pourcentage de 2nd', 'name': 'Nom du club'}


@st.cache_data(ttl=300, max_entries=1, show_spinner=False)
def _catalogue():
    return trends.catalogue()


@st.cache_data(ttl=300, max_entries=2, show_spinner=False)
def _summary(start, end, seasons, recent, role, signature, as_of, before_day=None):
    return trends.build(start, end, seasons=seasons or None, recent=recent, role=role,
                        now=datetime.fromisoformat(as_of), before_day=before_day)


@st.cache_data(ttl=120, max_entries=8, show_spinner=False)
def _details(team, league, start, end, seasons, role, signature, as_of, offset, limit, before_day=None):
    return trends.details(team, league, start, end, seasons=seasons or None, role=role,
                          now=datetime.fromisoformat(as_of), offset=offset, limit=limit, before_day=before_day)


@st.cache_data(ttl=60, max_entries=4, show_spinner=False)
def _day_calendar(day, signature, refresh_token):
    return day_trends.load(day)


@st.cache_data(ttl=60, max_entries=4, show_spinner=False)
def _period_calendar(start, end, signature, refresh_token):
    return day_trends.load_period(start, end)


def _result_column(label):
    colors = (['#8fc1ff', '#d7b9ff', '#ffc58a'] if st.context.theme.type == 'dark'
              else ['#175da7', '#7040a0', '#934b08'])
    return st.column_config.MultiselectColumn(label, options=list(CLASS_ORDER), color=colors)


def _select(rows, country, league, query, minimum, sort):
    search = query.strip().casefold()
    selected = [row for row in rows if row['matches'] >= minimum
                and (country is None or row['country'] == country)
                and (league is None or row['league'] == league)
                and (not search or search in f"{row['club']} {row['country']} {row['competition']}".casefold())]
    alpha = lambda row: (row['club'].casefold(), row['country'].casefold(),
                         row['competition'].casefold(), row['profile_id'])
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

    return [{'Club': row['club'], 'Pays': row['country'], 'Compétition': row['competition'],
             'Matchs exploitables': row['matches'],
             **{f'{value} (%)': row['percentages'][value] for value in CLASS_ORDER},
             'Tendance dominante': list(row['leaders']), 'Matchs récents': row['recent_matches'],
             'Tendance récente': recent_leaders(row),
             'Récents (ancien → récent)': ' → '.join(row['recent_results']),
             **role_columns(row, row),
             'Résultats exclus': row['excluded'], 'Non terminés': row['not_finished'],
             'Premier résultat': row['first_day'], 'Dernier résultat': row['last_day'],
             'ID club API': row['team_id'], 'ID compétition API': row['league']}
            for row in rows]


def _comparison(row):
    return [{'Résultat': [value], 'Sur la période (%)': row['percentages'][value],
             'Effectif sur la période': row['counts'][value],
             'Matchs récents': row['recent_matches'], 'Récents (%)': row['recent_percentages'][value],
             'Effectif récent': row['recent_counts'][value],
             'Matchs précédents': row['previous_matches'], 'Précédents (%)': row['previous_percentages'][value],
             'Effectif précédent': row['previous_counts'][value],
             'Écart (points)': row['delta_points'][value]} for value in CLASS_ORDER]


def _history_metadata(request, before_day):
    return {'Mode historique': 'Avant la journée choisie' if before_day else 'Période historique complète',
            'Historique strictement avant (Kinshasa)': before_day.isoformat() if before_day else '',
            'Coupure historique exclusive UTC': datetime.combine(before_day, time.min, ZONE)
                .astimezone(timezone.utc).isoformat() if before_day else '',
            'Fin historique demandée incluse': request[1].isoformat()}


def _history_end(request, before_day):
    return min(request[1], before_day - timedelta(days=1)) if before_day else request[1]


def _csv(rows, request, *, match_day=None, before_day=None):
    values = _table(rows)
    for item in values:
        for key in ('Tendance dominante', 'Tendance récente'):
            item[key] = ' / '.join(item[key])
        item.update({'Début inclus (Kinshasa)': request[0].isoformat(),
                     'Fin incluse (Kinshasa)': _history_end(request, before_day).isoformat(),
                     'Rôle': ROLES[request[4]], 'Fenêtre récente': request[3],
                     'Saisons sélectionnées': ', '.join(map(str, request[2])) or 'Toutes',
                     'Consultation UTC': request[6], **_history_metadata(request, before_day)})
        if match_day is not None:
            item['Filtre matchs · journée de Kinshasa'] = match_day.isoformat()
    out = StringIO(newline='')
    writer = csv.DictWriter(out, fieldnames=list(values[0]), delimiter=';')
    writer.writeheader()
    for item in values:
        writer.writerow({key: ("'" + value if isinstance(value, str) and value.startswith(('=', '+', '-', '@'))
                                else value) for key, value in item.items()})
    return out.getvalue().encode('utf-8-sig')


def _render_matches(row, request, report, before_day=None):
    if not st.toggle('Voir les rencontres utilisées et les exclusions', key='club_trends_show_matches'):
        return
    with st.container(horizontal=True):
        limit = st.selectbox('Rencontres par page', [10, 25, 50, 100], index=2, key='club_trends_match_limit')
        args = (row['team_id'], row['league'], *request[:3], request[4], request[5], request[6])
        detail = _details(*args, 0, limit, before_day=before_day)
        total = detail['total']
        pages = max(1, ceil(total / limit))
        context = (row['profile_id'], request, limit, before_day)
        if st.session_state.get('club_trends_match_context') != context:
            st.session_state['club_trends_match_context'] = context
            st.session_state['club_trends_match_page'] = 1
        page = st.selectbox('Page des rencontres', range(1, pages + 1), key='club_trends_match_page',
                            format_func=lambda value: f'Page {value} sur {pages}')
    if page > 1:
        detail = _details(*args, (page - 1) * limit, limit, before_day=before_day)
    if detail.get('revision') != report.get('revision'):
        st.caption('De nouveaux résultats ont été importés. Utilisez « Afficher et actualiser » pour recalculer les tendances.')
    values = [{'Coup d’envoi (Kinshasa)': datetime.fromisoformat(item['kickoff_local']).strftime('%d/%m/%Y %H:%M'),
               'Saison': item['season'], 'Domicile': item['home'], 'Extérieur': item['away'],
               'Rôle du club': ROLES[item['role']], 'Mi-temps': item['ht'] or 'Non reçu',
               'À 90 minutes': item['ft'] or 'Non reçu',
               'Verdict final': [item['value']] if item['included'] else [],
               'Buts marqués 1re période': item.get('goals_for_h1'),
               'Buts encaissés 1re période': item.get('goals_against_h1'),
               'Buts marqués 2e période': item.get('goals_for_h2'),
               'Buts encaissés 2e période': item.get('goals_against_h2'),
               'Compté dans la tendance': item['included'], 'Motif': item['reason']}
              for item in detail['rows']]
    if values:
        table_views.dataframe(values, hide_index=True, width='stretch', height=450, key='club_trends_matches',
            column_config={'Verdict final': _result_column('Verdict final'),
                'Saison': st.column_config.NumberColumn(format='%d')}, table_id='club_trends_panel:_render_matches:dataframe:1')
    st.caption(f'{total} rencontre(s) consultable(s) · Page {page}/{pages}. '
               'Les buts marqués et encaissés concernent le club sélectionné ; le verdict compare les buts des deux équipes réunies.')


def _day_table(rows):
    values = []
    for row in rows:
        local = datetime.fromisoformat(row['kickoff_local'])
        value = {'Match': f"{row['home']} — {row['away']}", 'Date': local.strftime('%d/%m/%Y'),
                 'Heure (Kinshasa)': local.strftime('%H:%M'), 'Pays': row['country'],
                 'Compétition': row['competition'], 'Domicile': row['home'], 'Extérieur': row['away'],
                 'Statut': row['status_label'], 'Minute': row['minute'], 'Score': row['score'],
                 'Mi-temps': row['halftime_score'], 'À 90 minutes': row['regulation_score'],
                 'Verdict final': [row['final_verdict']]}
        for side, label in (('home', 'Domicile'), ('away', 'Extérieur')):
            profile = row.get(side + '_profile')
            value[f'Matchs historiques {label.lower()}'] = profile['matches'] if profile else None
            for result in CLASS_ORDER:
                value[f'{label} · {result} (%)'] = profile['percentages'][result] if profile else None
            value[f'Tendance {label.lower()}'] = list(profile['leaders']) if profile else []
            value[f'{label} · récents selon le rôle choisi'] = ' → '.join(profile['recent_results']) if profile else ''
        value.update(role_columns(row.get('home_profile'), row.get('away_profile')))
        value.update({'ID du match API': row['match_id'], 'ID compétition API': row['league'],
                      'ID club domicile API': row['home_id'], 'ID club extérieur API': row['away_id']})
        values.append(value)
    return values


def _day_csv(values, request, day, calendar, end=None, *, before_day=None):
    end = end or day
    output = []
    for row in values:
        item = {name: ' / '.join(value) if isinstance(value, list) else value for name, value in row.items()}
        local_day = datetime.strptime(item['Date'], '%d/%m/%Y').date().isoformat()
        item.update({'Journée des matchs (Kinshasa)': local_day,
                     'Première journée exportée': day.isoformat(), 'Dernière journée exportée': end.isoformat(),
                     'Début historique inclus': request[0].isoformat(),
                     'Fin historique incluse': _history_end(request, before_day).isoformat(), 'Rôle historique': ROLES[request[4]],
                     'Dernier relevé du calendrier UTC': calendar.get('calendar_updates', {}).get(local_day)
                         or (calendar.get('calendar_updated_at') if day == end else '') or '',
                     'Fenêtre récente par rôle': request[3],
                     'Consultation historique UTC': request[6], **_history_metadata(request, before_day)})
        output.append(item)
    out = StringIO(newline='')
    writer = csv.DictWriter(out, fieldnames=list(output[0]), delimiter=';')
    writer.writeheader()
    for item in output:
        writer.writerow({key: ("'" + value if isinstance(value, str) and value.startswith(('=', '+', '-', '@'))
                                else value) for key, value in item.items()})
    return out.getvalue().encode('utf-8-sig')


def _match_period(day):
    if st.session_state.get('club_trends_period_default') != day:
        st.session_state['club_trends_period_default'] = day
        st.session_state['club_trends_period_start'] = day
        st.session_state['club_trends_period_end'] = day
        st.session_state['club_trends_period_applied'] = (day, day)
    with st.form('club_trends_match_period'):
        with st.container(horizontal=True):
            start = st.date_input('Première journée des matchs', key='club_trends_period_start',
                format='DD/MM/YYYY', min_value=date(1900, 1, 1), max_value=date(2100, 12, 31))
            end = st.date_input('Dernière journée des matchs incluse', key='club_trends_period_end',
                format='DD/MM/YYYY', min_value=date(1900, 1, 1), max_value=date(2100, 12, 31))
        apply = st.form_submit_button('Afficher la période des matchs')
    if apply:
        if start > end:
            st.warning('La première journée des matchs doit précéder ou être égale à la dernière.')
            return None
        st.session_state['club_trends_period_applied'] = start, end
    return st.session_state['club_trends_period_applied']


def _render_day(report, request, day, *, period=None, before_day=None):
    period = period or _match_period(day)
    if period is None:
        return
    start, end = period
    st.subheader(f'Matchs du {start:%d/%m/%Y}' + (f' au {end:%d/%m/%Y}' if start != end else ''),
                 icon=':material/calendar_month:')
    if st.button('Actualiser le relevé local', icon=':material/refresh:', key='club_trends_day_reload'):
        st.session_state['club_trends_day_refresh'] = st.session_state.get('club_trends_day_refresh', 0) + 1
    try:
        signature, refresh = api.data_signature(api.STORE_PATH), st.session_state.get('club_trends_day_refresh', 0)
        calendar = (_day_calendar(start, signature, refresh) if start == end else
                    _period_calendar(start, end, signature, refresh))
    except (trends.TrendsError, api.ApiFootballError, ValueError) as error:
        st.warning(str(error))
        return
    rows = day_trends.enrich(calendar['rows'], report['rows'])
    st.caption(f'{(end-start).days + 1} journée(s), dates incluses · Heures de Kinshasa. '
               'L’export contient tous les matchs filtrés de cette période, quelle que soit la page affichée.')
    missing = [item['day'] for item in calendar.get('days', []) if not item['matches'] and not item['updated_at']]
    if missing:
        st.caption('Journées sans donnée locale ni relevé : ' + ', '.join(missing) + '.')
    if calendar.get('revision') != report.get('revision'):
        st.caption('Le calendrier a reçu de nouvelles données depuis le calcul des tendances. '
                   'Utilisez « Afficher et actualiser » pour recalculer leur historique.')
    if calendar.get('totals', {}).get('unreadable'):
        st.warning(f"{calendar['totals']['unreadable']} rencontre(s) ne peuvent pas être affichées : leurs données sont à vérifier.")
    updated = calendar.get('calendar_updated_at')
    if updated:
        try:
            stamp = datetime.fromisoformat(updated.replace('Z', '+00:00')).astimezone(ZONE)
            st.caption(f'Dernier relevé du calendrier : {stamp:%d/%m/%Y à %H:%M} (Kinshasa).')
        except (TypeError, ValueError):
            st.caption('La date du dernier relevé du calendrier est à vérifier.')
    elif start == end:
        st.caption('Dernier relevé du calendrier non horodaté pour cette journée.')
    st.caption('Les scores et statuts suivent le dernier relevé API enregistré. '
               'Les tendances de chaque équipe utilisent la période, les saisons et le rôle choisis ci-dessus. '
               'Elles décrivent son historique et ne sont pas une prévision de cette rencontre. '
               'Une case vide signifie qu’aucun profil historique ne correspond à ces critères.')
    if not rows:
        st.info('Aucun match enregistré pour cette journée. Le calendrier de la page Matchs permet d’actualiser les rencontres API.')
        return
    with st.container(horizontal=True):
        st.metric('Matchs à cette date', len(rows))
        st.metric('En cours', sum(row['status_group'] == 'live' for row in rows))
        st.metric('Avec historique pour les deux clubs', sum(row['home_has_history'] and row['away_has_history'] for row in rows))
    with st.container(horizontal=True):
        countries = sorted({row['country'] for row in rows}, key=str.casefold)
        if st.session_state.get('club_trends_day_country') not in countries:
            st.session_state['club_trends_day_country'] = None
        country = st.selectbox('Pays des matchs', [None] + countries, key='club_trends_day_country',
                               format_func=lambda value: value or 'Tous les pays')
        leagues = {row['league']: row for row in rows if row['league'] is not None
                   and (country is None or row['country'] == country)}
        if st.session_state.get('club_trends_day_competition') not in leagues:
            st.session_state['club_trends_day_competition'] = None
        league = st.selectbox('Compétition des matchs', [None] + sorted(leagues,
                               key=lambda key: (leagues[key]['country'], leagues[key]['competition'], key)),
                               key='club_trends_day_competition', format_func=lambda key: 'Toutes les compétitions'
                               if key is None else f"{leagues[key]['country']} · {leagues[key]['competition']}")
    status = st.segmented_control('État des matchs', ['Tous', 'À venir', 'En cours', 'Terminés', 'Autres'],
                                   default='Tous', required=True, key='club_trends_day_status')
    query = st.text_input('Rechercher dans les matchs de la journée', type='search', key='club_trends_day_search')
    from calendar_data import filter_rows
    filtered = filter_rows([row for row in rows if country is None or row['country'] == country], league, status, query)
    st.caption(f'{len(filtered)} match(s) après les filtres sur {len(rows)} à cette date. '
               'Les rencontres sans historique restent visibles.')
    if not filtered:
        st.info('Aucun match ne correspond à ces filtres.')
        return
    with st.container(horizontal=True):
        limit = st.selectbox('Matchs par page', [10, 25, 50, 100, 250, 500], index=3, key='club_trends_day_limit')
        context = (start, end, calendar['revision'], country, league, status, query, limit, before_day)
        if st.session_state.get('club_trends_day_context') != context:
            st.session_state['club_trends_day_context'] = context
            st.session_state['club_trends_day_page'] = 1
        pages = max(1, ceil(len(filtered) / limit))
        page = st.selectbox('Page des matchs', range(1, pages + 1), key='club_trends_day_page',
                            format_func=lambda value: f'Page {value} sur {pages}')
    offset = (page - 1) * limit
    from calendar_panel import verdict_column
    columns = {'Match': st.column_config.TextColumn(width='large', pinned=True),
               'Verdict final': verdict_column(),
               'Tendance domicile': _result_column('Tendance domicile'),
               'Tendance extérieur': _result_column('Tendance extérieur')}
    columns.update({f'{label} · {value} (%)': st.column_config.NumberColumn(format='%.1f%%')
                    for label in ('Domicile', 'Extérieur') for value in CLASS_ORDER})
    table_views.dataframe(_day_table(filtered[offset:offset + limit]), hide_index=True, width='stretch', height=500,
                 key='club_trends_day_matches', column_config=columns, table_id='club_trends_panel:_render_day:dataframe:1')
    st.caption(f'Matchs {offset + 1}–{min(offset + limit, len(filtered))} sur {len(filtered)} · '
               'Heures de Kinshasa. Les verdicts finaux utilisent les scores à la pause et à 90 minutes.')
    st.download_button('Exporter tous les matchs filtrés avec leurs tendances (CSV)',
        data=lambda: _day_csv(_day_table(filtered), request, start, calendar, end, before_day=before_day),
        file_name=(f'matchs_tendances_clubs_{start}.csv' if start == end else
                   f'matchs_tendances_clubs_{start}_au_{end}.csv'), mime='text/csv',
        icon=':material/download:', on_click='ignore')


def render():
    render_page_header('Tendances des clubs',
        'Comparer les résultats par mi-temps de chaque club, à domicile et à l’extérieur.',
        icon=':material/groups:')
    st.caption('Source : API-Football · 1st, Égal et 2nd comparent les buts des deux équipes réunies dans chaque période. '
               'Ces fréquences historiques ne sont pas des probabilités futures.')
    view = st.segmented_control('Vue des tendances', ['Tous les clubs', 'Matchs de la journée'],
                                 default='Tous les clubs', required=True, key='club_trends_view')
    try:
        catalogue = _catalogue()
    except (trends.TrendsError, api.ApiFootballError) as error:
        st.warning(str(error))
        return
    selected_day = get_selected_day()
    if st.session_state.get('club_trends_selected_day') != selected_day:
        first = date.fromisoformat(catalogue['first_day']) if catalogue['first_day'] else selected_day
        st.session_state['club_trends_start'] = min(selected_day, max(first, date(selected_day.year, 1, 1)))
        st.session_state['club_trends_end'] = selected_day
        st.session_state['club_trends_selected_day'] = selected_day
        st.session_state.pop('club_trends_request', None)
    period = _match_period(selected_day) if view == 'Matchs de la journée' else None
    if view == 'Matchs de la journée' and period is None:
        return
    test_day = period[0] if period else selected_day
    exclude_day = st.toggle(f'Exclure le {test_day:%d/%m/%Y} des tendances et des récents',
        key='club_trends_before_day',
        help='Recalcule uniquement avec les rencontres antérieures à 00 h de cette journée à Kinshasa. '
             'La journée et les suivantes sont écartées ; les N récents sont complétés avec les matchs précédents. '
             'Pour une période de matchs, la même coupure avant la première journée s’applique à toutes les lignes.')
    before_day = test_day if exclude_day else None
    if before_day:
        st.caption(f'Test : historique arrêté avant le {before_day:%d/%m/%Y} à 00 h (Kinshasa). '
                   'La journée choisie et les suivantes ne comptent dans aucune tendance ni suite récente. '
                   + ('Cette même coupure s’applique à tous les matchs de la période. ' if period and period[0] != period[1] else '')
                   + 'Les scores de la journée restent visibles pour comparer. Les corrections actuellement disponibles sont utilisées.')
    with st.form('club_trends_filters'):
        with st.container(horizontal=True):
            start = st.date_input('Début de la période historique', key='club_trends_start', format='DD/MM/YYYY')
            end = st.date_input('Fin de la période historique incluse', key='club_trends_end', format='DD/MM/YYYY')
        with st.container(horizontal=True):
            seasons = st.multiselect('Saisons API-Football', catalogue['seasons'],
                placeholder='Toutes les saisons disponibles', key='club_trends_seasons',
                help='Une sélection vide inclut toutes les saisons dans la période choisie.')
            role = st.selectbox('Matchs du club', list(ROLES), format_func=ROLES.get, key='club_trends_role')
            recent = st.selectbox('Matchs récents par club', [5, 10, 20, 30], index=1,
                key='club_trends_recent', help='Les N derniers résultats exploitables dans le rôle et la période choisis.')
        submitted = st.form_submit_button('Afficher et actualiser', icon=':material/refresh:', type='primary')
    if submitted or 'club_trends_request' not in st.session_state:
        if start > end:
            st.warning('Le début de la période doit précéder sa fin.')
            return
        try:
            signature = api.data_signature(api.STORE_PATH)
        except api.ApiFootballError as error:
            st.warning(str(error))
            return
        st.session_state['club_trends_request'] = (start, end, tuple(sorted(seasons)), recent, role,
            signature, datetime.now(timezone.utc).replace(second=0, microsecond=0).isoformat())
    request = st.session_state['club_trends_request']
    st.caption('Récents domicile et récents extérieur restent dans deux colonnes séparées, de l’ancien vers le récent. '
               'Chaque colonne contient au maximum les N derniers résultats de ce rôle dans la période historique choisie.')
    try:
        with st.spinner('Lecture des tendances des clubs…'):
            report = _summary(*request, before_day=before_day)
    except (trends.TrendsError, api.ApiFootballError) as error:
        st.warning(str(error))
        return
    as_of = datetime.fromisoformat(request[6]).astimezone(ZONE)
    history_end = _history_end(request, before_day)
    period_label = (f'{request[0]:%d/%m/%Y} → {history_end:%d/%m/%Y}' if history_end >= request[0]
                    else 'aucune journée historique avant la coupure dans la période demandée')
    st.caption(f'Période appliquée : {period_label} · {ROLES[request[4]]} · '
               f'Coups d’envoi antérieurs au {as_of:%d/%m/%Y à %H:%M} (Kinshasa). '
               'Les corrections actuellement enregistrées sont utilisées ; ce tableau ne reconstitue pas les informations connues avant un ancien match.')
    if view == 'Matchs de la journée':
        _render_day(report, request, selected_day, period=period, before_day=before_day)
        return
    day_only = st.toggle(f'Uniquement les clubs avec un match le {selected_day:%d/%m/%Y}',
        key='club_trends_day_only',
        help='Aujourd’hui par défaut. La date des matchs en haut de la page permet de changer de journée. '
             'Le filtre utilise les rencontres enregistrées, tous statuts confondus, sans changer la période historique.')
    profile_rows = report['rows']
    calendar = None
    if day_only:
        try:
            calendar = _day_calendar(selected_day, api.data_signature(api.STORE_PATH),
                                     st.session_state.get('club_trends_day_refresh', 0))
            scope = day_trends.filter_profiles(profile_rows, calendar['rows'])
        except (trends.TrendsError, api.ApiFootballError, ValueError) as error:
            st.warning(str(error))
            return
        profile_rows = scope['rows']
        st.caption(f'Filtre matchs du {selected_day:%d/%m/%Y} (Kinshasa) : '
                   f"{len(calendar['rows'])} rencontre(s) enregistrée(s). "
                   'Les tendances conservent la période, les saisons et le rôle historique choisis ci-dessus.')
        if scope['missing_profiles']:
            st.caption(f"{scope['missing_profiles']} profil(s) club / compétition de cette journée sans historique "
                       'pour ces critères. Ces rencontres restent visibles dans « Matchs de la journée ».')
        if calendar.get('totals', {}).get('unreadable'):
            st.warning(f"{calendar['totals']['unreadable']} rencontre(s) du calendrier ont des données à vérifier.")
        if not calendar['rows']:
            st.info('Aucun match enregistré pour cette journée. Choisissez une autre date ou actualisez le calendrier dans Matchs.')
            return
    with st.container(horizontal=True):
        st.metric('Clubs', len({row['team_id'] for row in profile_rows}) if day_only else report['totals']['clubs'])
        st.metric('Profils club / compétition', len(profile_rows) if day_only else report['totals']['profiles'])
        if day_only:
            st.metric('Matchs à cette date', len(calendar['rows']))
        else:
            st.metric('Rencontres exploitables distinctes', report['totals']['matches'])
            st.metric('Résultats exclus distincts', report['totals']['excluded'])
    if not profile_rows:
        st.info('Aucun profil historique disponible pour les clubs de cette journée avec ces critères.' if day_only else
                'Aucune rencontre disponible dans cette période et ces saisons.')
        return
    with st.container(horizontal=True):
        countries = sorted({row['country'] for row in profile_rows}, key=str.casefold)
        if st.session_state.get('club_trends_country') not in countries:
            st.session_state['club_trends_country'] = None
        country = st.selectbox('Pays', [None] + countries,
            format_func=lambda value: value or 'Tous les pays', key='club_trends_country')
        indexed_leagues = {row['league']: row for row in profile_rows if country is None or row['country'] == country}
        if st.session_state.get('club_trends_competition') not in indexed_leagues:
            st.session_state['club_trends_competition'] = None
        league = st.selectbox('Compétition', [None] + sorted(indexed_leagues,
            key=lambda key: (indexed_leagues[key]['country'], indexed_leagues[key]['competition'], key)),
            format_func=lambda key: 'Toutes les compétitions' if key is None else
                f"{indexed_leagues[key]['country']} · {indexed_leagues[key]['competition']}", key='club_trends_competition')
        sort = st.selectbox('Trier par', list(SORTS), format_func=SORTS.get, key='club_trends_sort')
        minimum = st.number_input('Minimum de matchs exploitables', min_value=0, value=0, step=1,
                                   key='club_trends_minimum')
    query = st.text_input('Rechercher un club, un pays ou une compétition', key='club_trends_search', type='search')
    rows = _select(profile_rows, country, league, query, minimum, sort)
    if not rows:
        st.info('Aucun club ne correspond à ces filtres.')
        return
    with st.container(horizontal=True):
        limit = st.selectbox('Profils par page', [10, 25, 50, 100, 250, 500], index=3,
                             key='club_trends_summary_limit')
        context = (request, country, league, query, minimum, sort, limit, day_only, before_day,
                   selected_day if day_only else None, calendar['revision'] if calendar else None)
        if st.session_state.get('club_trends_summary_context') != context:
            st.session_state['club_trends_summary_context'] = context
            st.session_state['club_trends_summary_page'] = 1
        pages = max(1, ceil(len(rows) / limit))
        page = st.selectbox('Page des profils', range(1, pages + 1), key='club_trends_summary_page',
                            format_func=lambda value: f'Page {value} sur {pages}')
    offset = (page - 1) * limit
    colors = ['#8fc1ff', '#d7b9ff', '#ffc58a'] if st.context.theme.type == 'dark' else ['#175da7', '#7040a0', '#934b08']
    columns = {f'{value} (%)': st.column_config.ProgressColumn(format='%.1f%%', min_value=0,
               max_value=100, color=color) for value, color in zip(CLASS_ORDER, colors)}
    columns.update({'Tendance dominante': _result_column('Tendance dominante'),
                    'Tendance récente': _result_column('Tendance récente'),
                    'Club': st.column_config.TextColumn(width='medium', pinned=True)})
    table_views.dataframe(_table(rows[offset:offset + limit]), hide_index=True, width='stretch', height=500,
                 key='club_trends_summary', column_config=columns, table_id='club_trends_panel:render:dataframe:1')
    st.caption(f'Profils {offset + 1}–{min(offset + limit, len(rows))} sur {len(rows)} · '
               'Une ligne par club et compétition. '
               'Un même match peut contribuer aux profils des deux clubs : les lignes ne sont pas des observations indépendantes. '
               'Les petits effectifs restent visibles ; les égalités en tête sont conservées.')
    st.download_button('Exporter tous les profils filtrés (CSV)',
        data=lambda: _csv(rows, request, match_day=selected_day if day_only else None, before_day=before_day),
        file_name=(f'tendances_clubs_matchs_{selected_day}_{request[4]}.csv' if day_only else
                   f'tendances_clubs_{request[0]}_{request[1]}_{request[4]}.csv'),
        mime='text/csv', icon=':material/download:', on_click='ignore')
    indexed = {row['profile_id']: row for row in rows}
    if st.session_state.get('club_trends_profile') not in indexed:
        st.session_state['club_trends_profile'] = rows[0]['profile_id']
    profile = st.selectbox('Club et compétition à examiner', list(indexed), key='club_trends_profile',
        format_func=lambda key: f"{indexed[key]['club']} · {indexed[key]['country']} · {indexed[key]['competition']}")
    row = indexed[profile]
    st.subheader('Évolution récente du club', icon=':material/trending_up:')
    st.caption(f"Les {row['recent_matches']} derniers résultats exploitables sont comparés aux "
               f"{row['previous_matches']} précédents de la période, pour {ROLES[request[4]].lower()}. "
               'Aucun match n’appartient aux deux groupes.')
    table_views.dataframe(_comparison(row), hide_index=True, width='stretch', key='club_trends_comparison',
        column_config={'Résultat': _result_column('Résultat'),
            **{name: st.column_config.NumberColumn(format='%.1f%%') for name in
               ('Sur la période (%)', 'Récents (%)', 'Précédents (%)')},
            'Écart (points)': st.column_config.NumberColumn(format='%+.1f',
                help='Fréquence récente moins fréquence des matchs précédents, en points de pourcentage.')}, table_id='club_trends_panel:render:dataframe:2')
    if not row['previous_matches']:
        st.info('Pas de matchs antérieurs au groupe récent dans cette période : l’écart reste indisponible.')
    try:
        _render_matches(row, request, report, before_day=before_day)
    except (trends.TrendsError, api.ApiFootballError) as error:
        st.warning(str(error))
    if report.get('exclusion_reasons'):
        st.subheader('Exclusions de la période', icon=':material/info:')
        st.caption('Rencontres distinctes, avant les filtres du tableau. Les matchs non terminés restent hors pourcentages.')
        table_views.dataframe([{'Motif': reason, 'Rencontres': count} for reason, count in
                      sorted(report['exclusion_reasons'].items(), key=lambda item: (-item[1], item[0]))],
                     hide_index=True, width='stretch', table_id='club_trends_panel:render:dataframe:3')
