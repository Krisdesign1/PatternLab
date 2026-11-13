"""Club trends from corrected local results, without collecting or predicting."""
import table_views
import csv
from datetime import date, datetime, time, timedelta, timezone
from io import StringIO
from math import ceil, isfinite
from zoneinfo import ZoneInfo

import streamlit as st

import api_football as api
import club_trends as trends
import club_trends_day as day_trends
from club_recent_sequences import role_columns, comparison_role_columns, role_leaders
from analysis_date import get_selected_day
from halftime_domain import CLASS_ORDER
from ux_theme import render_page_header

ZONE = ZoneInfo('Africa/Kinshasa')
ROLES = {'all': 'Tous les matchs', 'home': 'À domicile', 'away': 'À l’extérieur'}
SORTS = {'matches': 'Historique exploitable', '1st': 'Pourcentage de 1st',
         'Égal': 'Pourcentage de Égal', '2nd': 'Pourcentage de 2nd', 'name': 'Nom du club'}
PERCENT_ROLES = {'home': 'Domicile', 'away': 'Extérieur'}
PERCENT_MODES = {'exact': 'Exactement', 'minimum': 'Au moins',
                 'maximum': 'Au plus', 'range': 'Entre deux valeurs'}


@st.cache_data(ttl=300, max_entries=1, show_spinner=False)
def _catalogue():
    return trends.catalogue()


@st.cache_data(ttl=300, max_entries=2, show_spinner=False)
def _summary(start, end, seasons, recent, role, signature, as_of, before_day=None):
    """Cache profiles including independent full-history home/away frequencies."""
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


def _request_comparison_refresh():
    st.session_state['club_trends_comparison_refresh_requested'] = True


def _comparison_snapshot(request, start, end):
    """Keep both states stable across display/filter changes until explicit refresh."""
    context = (request, start, end)
    if st.session_state.get('club_trends_comparison_context') != context:
        report = trends.build_comparison(request[0], request[1], comparison_start=start,
            comparison_end=end, seasons=request[2] or None, recent=request[3], role=request[4],
            now=datetime.fromisoformat(request[6]))
        st.session_state['club_trends_comparison_snapshot'] = report
        st.session_state['club_trends_comparison_context'] = context
    return st.session_state['club_trends_comparison_snapshot']


def _comparison_metadata(comparison, verdict_filters=()):
    if not comparison:
        return {}
    return {'Mode historique': 'Comparaison avant / après',
            'Filtres et sélection appliqués à': ('Avant pour la sélection historique et les pourcentages ; '
                'verdicts : colonnes choisies' if verdict_filters else 'Avant uniquement'),
            'Première journée comparée': comparison['comparison_start'],
            'Dernière journée comparée incluse': comparison['comparison_end'],
            'Coupure Avant exclusive UTC': comparison['before_cutoff_utc'],
            'Coupure Après exclusive UTC': comparison['after_cutoff_utc'],
            'Consultation comparaison UTC': comparison['as_of'],
            'Fin historique Après incluse': min(comparison['after']['end'], comparison['comparison_end']),
            'Révision historique commune Avant / Après': comparison['revision']}


def _comparison_config(show_after):
    columns = {}
    for label in PERCENT_ROLES.values():
        for phase in ('avant', 'après') if show_after else ('avant',):
            name = f'Tendance {label.lower()} {phase}'
            columns[name] = _result_column(name)
        for result in CLASS_ORDER:
            for state in ('avant', 'après') if show_after else ('avant',):
                name = f'{label} · {result} {state} (%)'
                columns[name] = st.column_config.NumberColumn(format='%.1f%%')
            if show_after:
                columns[f'{label} · {result} écart (points)'] = st.column_config.NumberColumn(
                    format='%+.1f', help='Après moins Avant, en points, à la précision affichée.')
    return columns


def _comparison_table(rows, comparison, *, show_after=True):
    after = {row['profile_id']: row for row in comparison['after']['rows']}
    values = []
    identity = {'Club', 'Pays', 'Compétition', 'ID club API', 'ID compétition API'}
    for row, original in zip(rows, _table(rows)):
        value = {name: original[name] for name in ('Club', 'Pays', 'Compétition')}
        for side in PERCENT_ROLES:
            value.update(comparison_role_columns(row, after.get(row['profile_id']), side, show_after=show_after))
        # The general profile and chosen-role window also belong to Before.
        role_fields = set(_role_columns(row, 'home')) | set(_role_columns(row, 'away')) | set(role_columns(row, row))
        value.update({'Avant · ' + name: item for name, item in original.items()
                      if name not in identity and name not in role_fields})
        value.update({name: original[name] for name in ('ID club API', 'ID compétition API')})
        first = ('Club', 'Pays', 'Compétition', *[
            f'Tendance {label.lower()} {phase}' for label in PERCENT_ROLES.values()
            for phase in (('avant', 'après') if show_after else ('avant',))])
        value = {**{name: value[name] for name in first},
                 **{name: item for name, item in value.items() if name not in first}}
        values.append(value)
    return values


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


def _role_columns(profile, side):
    """Expose the exact denominator and three frequencies used by role filters."""
    label = PERCENT_ROLES[side]
    return {f'Matchs historiques {label.lower()}': profile.get(side + '_matches', 0) if profile else None,
            **{f'{label} · {result} (%)': profile.get(side + '_percentages', {}).get(result)
               if profile else None for result in CLASS_ORDER}}


def _history_filters(*, comparison=False):
    """Apply role minimums explicitly, without rebuilding historical profiles."""
    with st.form('club_trends_support_form'):
        with st.container(horizontal=True):
            home = st.number_input('Minimum de matchs à domicile', min_value=0, value=0, step=1,
                key='club_trends_min_home', persist_state='session')
            away = st.number_input('Minimum de matchs à l’extérieur', min_value=0, value=0, step=1,
                key='club_trends_min_away', persist_state='session')
        st.caption('Minimums sur les colonnes « Matchs historiques domicile / extérieur » ; '
                   '0 désactive le critère. Les deux minimums doivent être respectés.'
                   + (' La comparaison utilise les effectifs Avant.' if comparison else ''))
        apply = st.form_submit_button('Appliquer le minimum d’historique')
    if apply or 'club_trends_support_applied' not in st.session_state:
        st.session_state['club_trends_support_applied'] = (home, away)
    minima = st.session_state['club_trends_support_applied']
    if any(minima):
        st.caption(f'Minimums appliqués : domicile ≥ {minima[0]} · extérieur ≥ {minima[1]}.')
    return minima


def _filter_history_counts(rows, minima, *, fixtures=False):
    """Test exact role denominators, using Before profiles in comparison mode."""
    if not any(minima):
        return rows
    def matches(row):
        for side, minimum in zip(PERCENT_ROLES, minima):
            if minimum <= 0:
                continue
            profile = row.get(side + '_profile') if fixtures else row
            count = profile.get(side + '_matches') if profile else None
            if count is None or not isfinite(count) or count < minimum:
                return False
        return True
    return [row for row in rows if matches(row)]


def _history_filter_metadata(minima):
    return {'Minimum historique domicile': minima[0],
            'Minimum historique extérieur': minima[1]}


def _percentage_filters(*, comparison=False):
    options = [f'{side}:{result}' for side in PERCENT_ROLES for result in CLASS_ORDER]
    def label(key):
        side, result = key.split(':')
        return f'{PERCENT_ROLES[side]} · {result}'
    with st.expander('Filtrer les pourcentages domicile / extérieur',
                     expanded=bool(st.session_state.get('club_trends_pct_selected'))):
        selected = st.multiselect('Pourcentages à filtrer', options, format_func=label,
            key='club_trends_pct_selected', persist_state='session',
            placeholder='Choisir 1st, Égal ou 2nd à domicile et/ou à l’extérieur')
        st.caption('Domicile : matchs du club chez lui. Extérieur : matchs du club en déplacement. '
                   'Dans les matchs de la journée, chaque critère concerne l’équipe du rôle indiqué. '
                   'Les six fréquences utilisent la période et les saisons appliquées ci-dessus. '
                   'Tous les critères choisis doivent être respectés. '
                   'La comparaison utilise les pourcentages arrondis à une décimale, comme dans le tableau.')
        if comparison:
            st.caption('Les critères portent uniquement sur Avant. Afficher Après ne change pas les lignes sélectionnées.')
        criteria = []
        invalid = False
        for key in options:
            if key not in selected:
                continue
            side, result = key.split(':')
            prefix = 'club_trends_pct_' + side + '_' + result
            with st.container(horizontal=True):
                mode = st.selectbox('Condition · ' + label(key), list(PERCENT_MODES),
                    format_func=PERCENT_MODES.get, key=prefix + '_mode', persist_state='session')
                value = st.number_input(('Minimum (%) · ' if mode == 'range' else 'Pourcentage (%) · ') + label(key),
                    min_value=0.0, max_value=100.0, value=50.0, step=0.1, format='%.1f',
                    key=prefix + '_value', persist_state='session')
                upper = st.number_input('Maximum (%) · ' + label(key), min_value=0.0, max_value=100.0,
                    value=100.0, step=0.1, format='%.1f', key=prefix + '_upper', persist_state='session') if mode == 'range' else value
            lower = 0.0 if mode == 'maximum' else value
            upper = 100.0 if mode == 'minimum' else upper
            lower, upper = float(f'{lower:.1f}'), float(f'{upper:.1f}')
            if not (0 <= lower <= upper <= 100):
                st.warning(f'{label(key)} : le minimum doit être inférieur ou égal au maximum.')
                invalid = True
            criteria.append((side, result, lower, upper))
        if selected:
            st.caption('Un pourcentage absent ne vaut pas 0 % : la ligne est écartée uniquement '
                       'si un filtre actif nécessite cette valeur. Retirez un critère pour le désactiver.')
    return None if invalid else tuple(criteria)


def _filter_percentages(rows, criteria, *, fixtures=False):
    """Inclusive AND filters, using the same one-decimal precision as the table."""
    def matches(row):
        for side, result, lower, upper in criteria:
            profile = row.get(side + '_profile') if fixtures else row
            value = profile.get(side + '_percentages', {}).get(result) if profile else None
            if value is None or not isfinite(value) or not lower <= float(f'{value:.1f}') <= upper:
                return False
        return True
    return [row for row in rows if matches(row)]


def _percentage_metadata(criteria):
    parts = []
    for side, result, lower, upper in criteria:
        value = f'{lower:.1f} %' if lower == upper else f'{lower:.1f} à {upper:.1f} % inclus'
        parts.append(f'{PERCENT_ROLES[side]} · {result} : {value}')
    return {'Filtres de pourcentages par rôle': ' ET '.join(parts) or 'Aucun',
            'Précision des filtres (%)': '0.1 (arrondi à une décimale)'}


def _verdict_options(*, fixtures=False, comparison=False):
    if comparison:
        columns = tuple(f'Tendance {label.lower()} {phase}' for label in PERCENT_ROLES.values()
                        for phase in ('avant', 'après'))
    else:
        columns = (('Tendance domicile', 'Tendance extérieur') if fixtures else
                   ('Tendance dominante', 'Tendance récente'))
    return columns + (('Verdict final',) if fixtures else ())


def _verdict_filters(*, fixtures=False, comparison=False):
    prefix = 'club_trends_verdict_' + ('day' if fixtures else 'clubs') + ('_compare' if comparison else '')
    options = _verdict_options(fixtures=fixtures, comparison=comparison)
    with st.expander('Filtrer 1st, Égal ou 2nd',
                     expanded=bool(st.session_state.get(prefix + '_columns'))):
        selected = st.multiselect('Colonnes de verdict à filtrer', options,
            key=prefix + '_columns', persist_state='session', placeholder='Choisir les tendances ou le verdict final')
        criteria = []
        for column in options:
            if column not in selected:
                continue
            result = st.segmented_control('Résultat · ' + column, ['Tous', *CLASS_ORDER],
                default='Tous', required=True, key=prefix + '_' + column, persist_state='session')
            if result in CLASS_ORDER:
                criteria.append((column, result))
        st.caption('Les critères choisis se cumulent. En cas d’égalité en tête, '
                   'la ligne est conservée si le résultat recherché figure parmi les tendances.')
    if criteria:
        st.caption('Filtres de verdicts actifs : ' + ' ET '.join(f'{column} = {result}' for column, result in criteria)
                   + '. Les critères Après restent actifs même si leurs colonnes sont masquées.')
    return tuple(criteria)


def _filter_verdicts(rows, criteria, *, fixtures=False, comparison=None):
    if not criteria:
        return rows
    options = _verdict_options(fixtures=fixtures, comparison=bool(comparison))
    if any(column not in options or result not in CLASS_ORDER for column, result in criteria):
        raise ValueError('Critère de verdict indisponible dans cette vue.')
    after = {row['profile_id']: row for row in comparison['after']['rows']} if comparison else {}
    roles = {f'Tendance {label.lower()}' + suffix: (side, phase)
             for side, label in PERCENT_ROLES.items()
             for suffix, phase in (('', 'before'), (' avant', 'before'), (' après', 'after'))}
    def matches(row):
        for column, result in criteria:
            if column == 'Verdict final':
                values = [row.get('final_verdict')]
            elif column == 'Tendance dominante':
                values = row['leaders']
            elif column == 'Tendance récente':
                counts = row['recent_counts']
                maximum = max(counts.values(), default=0)
                values = [value for value in CLASS_ORDER if maximum > 0 and counts.get(value) == maximum]
            else:
                side, phase = roles[column]
                profile = (row.get(side + ('_after_profile' if phase == 'after' else '_profile')) if fixtures
                           else after.get(row['profile_id']) if phase == 'after' else row)
                values = role_leaders(profile, side)
            if result not in values:
                return False
        return True
    return [row for row in rows if matches(row)]


def _verdict_metadata(criteria):
    return {'Filtres de verdicts': ' ET '.join(f'{column} = {result}' for column, result in criteria) or 'Aucun',
            'Correspondance des tendances': 'Inclut les égalités en tête ; critères cumulatifs'}


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
             **_role_columns(row, 'home'), **_role_columns(row, 'away'),
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


def _csv(rows, request, *, match_day=None, before_day=None, percentage_filters=(), comparison=None,
         verdict_filters=(), history_minima=(0, 0)):
    values = _comparison_table(rows, comparison) if comparison else _table(rows)
    for item in values:
        for key, value in list(item.items()):
            if isinstance(value, list):
                item[key] = ' / '.join(value)
        item.update({'Début inclus (Kinshasa)': request[0].isoformat(),
                     'Fin incluse (Kinshasa)': _history_end(request, before_day).isoformat(),
                     'Rôle': ROLES[request[4]], 'Fenêtre récente': request[3],
                     'Saisons sélectionnées': ', '.join(map(str, request[2])) or 'Toutes',
                     'Consultation UTC': request[6], **_history_metadata(request, before_day),
                     **_percentage_metadata(percentage_filters), **_verdict_metadata(verdict_filters),
                     **_history_filter_metadata(history_minima),
                     **_comparison_metadata(comparison, verdict_filters)})
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


def _day_table(rows, *, comparison=False, show_after=True):
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
            if comparison:
                value.update(comparison_role_columns(profile, row.get(side + '_after_profile'), side,
                                                      show_after=show_after))
            else:
                value.update(_role_columns(profile, side))
            if not comparison:
                counts = profile.get(side + '_counts', {}) if profile else {}
                maximum = max(counts.values(), default=0)
                value[f'Tendance {label.lower()}'] = [result for result in CLASS_ORDER
                    if maximum and counts.get(result) == maximum]
            value[f'{label} · récents selon le rôle choisi' + (' avant' if comparison else '')] = ' → '.join(profile['recent_results']) if profile else ''
        if not comparison:
            value.update(role_columns(row.get('home_profile'), row.get('away_profile')))
        value.update({'ID du match API': row['match_id'], 'ID compétition API': row['league'],
                      'ID club domicile API': row['home_id'], 'ID club extérieur API': row['away_id']})
        if comparison:
            first = ('Match', 'Date', 'Heure (Kinshasa)', 'Verdict final', *[
                f'Tendance {label.lower()} {phase}' for label in PERCENT_ROLES.values()
                for phase in (('avant', 'après') if show_after else ('avant',))])
            value = {**{name: value[name] for name in first},
                     **{name: item for name, item in value.items() if name not in first}}
        values.append(value)
    return values


def _day_csv(values, request, day, calendar, end=None, *, before_day=None, percentage_filters=(),
             comparison=None, verdict_filters=(), history_minima=(0, 0)):
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
                     'Consultation historique UTC': request[6], **_history_metadata(request, before_day),
                     **_percentage_metadata(percentage_filters), **_verdict_metadata(verdict_filters),
                     **_history_filter_metadata(history_minima),
                     **_comparison_metadata(comparison, verdict_filters)})
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


def _render_day(report, request, day, *, period=None, before_day=None, comparison=None, show_after=True):
    period = period or _match_period(day)
    if period is None:
        return
    start, end = period
    st.subheader(f'Matchs du {start:%d/%m/%Y}' + (f' au {end:%d/%m/%Y}' if start != end else ''),
                 icon=':material/calendar_month:')
    if st.button('Actualiser le relevé local', icon=':material/refresh:', key='club_trends_day_reload',
                 on_click=_request_comparison_refresh if comparison else None):
        st.session_state['club_trends_day_refresh'] = st.session_state.get('club_trends_day_refresh', 0) + 1
    try:
        if comparison and 'calendar' in comparison:
            calendar = comparison['calendar']
        else:
            signature, refresh = api.data_signature(api.STORE_PATH), st.session_state.get('club_trends_day_refresh', 0)
            calendar = (_day_calendar(start, signature, refresh) if start == end else
                        _period_calendar(start, end, signature, refresh))
            if comparison:
                comparison['calendar'] = calendar
    except (trends.TrendsError, api.ApiFootballError, ValueError) as error:
        st.warning(str(error))
        return
    rows = day_trends.enrich(calendar['rows'], report['rows'])
    if comparison:
        after = {row['profile_id']: row for row in comparison['after']['rows']}
        for row in rows:
            for side in PERCENT_ROLES:
                row[side + '_after_profile'] = after.get(f"{row[side + '_id']}:{row['league']}")
        pending = sum(row['status_group'] != 'finished' or row['final_verdict'] not in CLASS_ORDER for row in rows)
        st.caption('Même base pour Avant et Après ; pourcentages et tri basés sur Avant. '
                   'Le verdict final reste celui du calendrier. L’export conserve les deux états, '
                   'même si les colonnes Après sont masquées.')
        if pending:
            st.caption(f'Après provisoire : {pending} match(s) de la période sans résultat final exploitable. '
                       'Seuls les résultats exploitables entrent dans les tendances Après.')
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
               'Les pourcentages Domicile utilisent les matchs de cette équipe chez elle ; '
               'les pourcentages Extérieur utilisent les déplacements de cette équipe, '
               'sur la période et les saisons appliquées. '
               'Ces fréquences décrivent son historique et ne sont pas une prévision de cette rencontre. '
               'Une case vide signifie qu’aucun profil historique ne correspond à ces critères.')
    if not rows:
        st.info('Aucun match enregistré pour cette journée. Le calendrier de la page Matchs permet d’actualiser les rencontres API.')
        return
    with st.container(horizontal=True):
        st.metric('Matchs à cette date', len(rows))
        st.metric('En cours', sum(row['status_group'] == 'live' for row in rows))
        st.metric('Avec historique pour les deux clubs', sum(
            bool(row.get('home_profile') and row['home_profile'].get('home_matches', 0)) and
            bool(row.get('away_profile') and row['away_profile'].get('away_matches', 0)) for row in rows))
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
    history_minima = _history_filters(comparison=bool(comparison))
    filtered = _filter_history_counts(filtered, history_minima, fixtures=True)
    percentage_filters = _percentage_filters(comparison=bool(comparison))
    if percentage_filters is None:
        return
    filtered = _filter_percentages(filtered, percentage_filters, fixtures=True)
    verdict_filters = _verdict_filters(fixtures=True, comparison=bool(comparison))
    filtered = _filter_verdicts(filtered, verdict_filters, fixtures=True, comparison=comparison)
    st.caption(f'{len(filtered)} match(s) après les filtres sur {len(rows)} à cette date. '
               + ('Base historique selon Avant. ' if comparison else '')
               + ('Les valeurs historiques requises par les filtres doivent être disponibles.' if percentage_filters or any(history_minima) else
                  'Les rencontres sans historique restent visibles en l’absence de filtre sur leur tendance.'))
    if not filtered:
        st.info('Aucun match ne correspond à ces filtres.')
        return
    with st.container(horizontal=True):
        limit = st.selectbox('Matchs par page', [10, 25, 50, 100, 250, 500], index=3,
                             key='club_trends_day_limit', persist_state='session')
        context = (request, start, end, calendar['revision'], country, league, status, query, limit, before_day,
                   percentage_filters, verdict_filters, history_minima)
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
    if comparison:
        columns.update(_comparison_config(show_after))
    displayed = _day_table(filtered[offset:offset + limit], comparison=bool(comparison), show_after=show_after)
    if comparison:
        table_views.dataframe(displayed, hide_index=True, width='stretch', height=500,
            key='club_trends_day_matches_compare', column_config=columns, table_id='club_trends_panel:day:before_after')
    else:
        table_views.dataframe(displayed, hide_index=True, width='stretch', height=500,
            key='club_trends_day_matches', column_config=columns, table_id='club_trends_panel:_render_day:dataframe:1')
    st.caption(f'Matchs {offset + 1}–{min(offset + limit, len(filtered))} sur {len(filtered)} · '
               'Heures de Kinshasa. Les verdicts finaux utilisent les scores à la pause et à 90 minutes.')
    st.download_button('Exporter tous les matchs filtrés avec leurs tendances (CSV)',
        data=lambda: _day_csv(_day_table(filtered, comparison=bool(comparison)), request, start, calendar, end,
                              before_day=before_day, percentage_filters=percentage_filters, comparison=comparison,
                              verdict_filters=verdict_filters, history_minima=history_minima),
        file_name=('avant_apres_' if comparison else '') + (f'matchs_tendances_clubs_{start}.csv' if start == end else
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
    compare = st.toggle('Comparer avant / après sur les mêmes matchs', key='club_trends_compare',
        persist_state='session', help='Les filtres de pourcentages utilisent Avant. Après ajoute uniquement les résultats '
        'exploitables de la journée ou de la période choisie, dans les bornes historiques appliquées.')
    show_after = True
    if compare:
        exclude_day = True
        show_after = st.toggle('Afficher Après et les écarts', value=True,
                              key='club_trends_compare_show_after', persist_state='session')
    else:
        exclude_day = st.toggle(f'Exclure le {test_day:%d/%m/%Y} des tendances et des récents',
            key='club_trends_before_day', persist_state='session',
            help='Recalcule uniquement avec les rencontres antérieures à 00 h de cette journée à Kinshasa. '
                 'La journée et les suivantes sont écartées ; les N récents sont complétés avec les matchs précédents. '
                 'Pour une période de matchs, la même coupure avant la première journée s’applique à toutes les lignes.')
    before_day = test_day if exclude_day else None
    if before_day and not compare:
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
                key='club_trends_recent', help='Jusqu’aux N derniers résultats exploitables dans le rôle et la période choisis. '
                'Ce réglage n’exclut pas les historiques plus courts ; utilisez les minimums de matchs domicile / extérieur pour cela.')
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
    if compare and st.button('Actualiser la comparaison', icon=':material/refresh:',
                             key='club_trends_comparison_refresh'):
        _request_comparison_refresh()
    refresh_requested = st.session_state.pop('club_trends_comparison_refresh_requested', False)
    if compare and (submitted or refresh_requested):
        try:
            request = (*request[:5], api.data_signature(api.STORE_PATH), datetime.now(timezone.utc).isoformat())
        except api.ApiFootballError as error:
            st.warning(str(error))
            return
        st.session_state['club_trends_request'] = request
    st.caption('Récents domicile et récents extérieur restent dans deux colonnes séparées, de l’ancien vers le récent. '
               'Chaque colonne contient au maximum les N derniers résultats de ce rôle dans la période historique choisie.')
    try:
        with st.spinner('Lecture des tendances des clubs…'):
            comparison = _comparison_snapshot(request, test_day, period[1] if period else selected_day) if compare else None
            report = comparison['before'] if comparison else _summary(*request, before_day=before_day)
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
    if comparison:
        first, last = date.fromisoformat(comparison['comparison_start']), date.fromisoformat(comparison['comparison_end'])
        after_end = min(request[1], last)
        st.caption(f'Avant : historique strictement antérieur au {first:%d/%m/%Y}. '
                   f'Après : historique jusqu’au {after_end:%d/%m/%Y} inclus, avec les résultats disponibles '
                   f'au {as_of:%d/%m/%Y à %H:%M} (Kinshasa). '
                   'Les filtres de pourcentages portent sur Avant. Les filtres de verdicts utilisent les colonnes choisies ; '
                   'afficher ou masquer Après conserve la liste et la page. '
                   'La comparaison est conservée jusqu’à une actualisation explicite ou un changement de période.')
        if first != last:
            st.caption('Pour cette période, Avant s’arrête avant la première journée et Après à la dernière. '
                       'Les deux bornes sont communes à toutes les lignes.')
        if request[1] < last:
            st.caption('La fin historique appliquée précède la fin de la période comparée : '
                       'Après reste limité à cette fin historique. Modifiez-la puis appliquez pour inclure davantage de jours.')
    if view == 'Matchs de la journée':
        _render_day(report, request, selected_day, period=period, before_day=before_day,
                    comparison=comparison, show_after=show_after)
        return
    day_only = st.toggle(f'Uniquement les clubs avec un match le {selected_day:%d/%m/%Y}',
        key='club_trends_day_only',
        help='Aujourd’hui par défaut. La date des matchs en haut de la page permet de changer de journée. '
             'Le filtre utilise les rencontres enregistrées, tous statuts confondus, sans changer la période historique.')
    profile_rows = report['rows']
    calendar = None
    if day_only:
        try:
            if comparison and 'calendar' in comparison:
                calendar = comparison['calendar']
            else:
                calendar = _day_calendar(selected_day, api.data_signature(api.STORE_PATH),
                                         st.session_state.get('club_trends_day_refresh', 0))
                if comparison:
                    comparison['calendar'] = calendar
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
    history_minima = _history_filters(comparison=bool(comparison))
    rows = _filter_history_counts(rows, history_minima)
    percentage_filters = _percentage_filters(comparison=bool(comparison))
    if percentage_filters is None:
        return
    rows = _filter_percentages(rows, percentage_filters)
    verdict_filters = _verdict_filters(comparison=bool(comparison))
    rows = _filter_verdicts(rows, verdict_filters, comparison=comparison)
    st.caption(f'{len(rows)} profil(s) après les filtres sur {len(profile_rows)}. '
               'Les pourcentages Domicile et Extérieur restent propres à chaque rôle, '
               'indépendamment du choix « Matchs du club » pour les autres colonnes.')
    if not rows:
        st.info('Aucun club ne correspond à ces filtres.')
        return
    with st.container(horizontal=True):
        limit = st.selectbox('Profils par page', [10, 25, 50, 100, 250, 500], index=3,
                             key='club_trends_summary_limit', persist_state='session')
        context = (request, country, league, query, minimum, sort, limit, day_only, before_day,
                   selected_day if day_only else None, calendar['revision'] if calendar else None,
                   percentage_filters, verdict_filters, history_minima)
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
    columns.update({f'{label} · {value} (%)': st.column_config.NumberColumn(format='%.1f%%')
                    for label in PERCENT_ROLES.values() for value in CLASS_ORDER})
    columns.update({'Tendance dominante': _result_column('Tendance dominante'),
                    'Tendance récente': _result_column('Tendance récente'),
                    'Club': st.column_config.TextColumn(width='medium', pinned=True)})
    if comparison:
        columns.update(_comparison_config(show_after))
        columns.update({'Avant · Tendance dominante': _result_column('Avant · Tendance dominante'),
                        'Avant · Tendance récente': _result_column('Avant · Tendance récente')})
    values = (_comparison_table(rows[offset:offset + limit], comparison, show_after=show_after) if comparison else
              _table(rows[offset:offset + limit]))
    if comparison:
        table_views.dataframe(values, hide_index=True, width='stretch', height=500,
            key='club_trends_summary_compare', column_config=columns, table_id='club_trends_panel:clubs:before_after')
    else:
        table_views.dataframe(values, hide_index=True, width='stretch', height=500,
            key='club_trends_summary', column_config=columns, table_id='club_trends_panel:render:dataframe:1')
    st.caption(f'Profils {offset + 1}–{min(offset + limit, len(rows))} sur {len(rows)} · '
               'Une ligne par club et compétition. '
               'Un même match peut contribuer aux profils des deux clubs : les lignes ne sont pas des observations indépendantes. '
               'Les petits effectifs restent visibles ; les égalités en tête sont conservées.')
    st.download_button('Exporter tous les profils filtrés (CSV)',
        data=lambda: _csv(rows, request, match_day=selected_day if day_only else None, before_day=before_day,
                          percentage_filters=percentage_filters, comparison=comparison, verdict_filters=verdict_filters,
                          history_minima=history_minima),
        file_name=('avant_apres_' if comparison else '') + (f'tendances_clubs_matchs_{selected_day}_{request[4]}.csv' if day_only else
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
