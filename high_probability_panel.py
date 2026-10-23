"""Explore today's highest model probabilities across all competitions.

Rendering and filtering only read the local scan. Calendar refresh, scenario
preparation, and the immutable pre-match journal entry are explicit actions.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json
from zoneinfo import ZoneInfo

import streamlit as st

import api_football as api
import high_probability_engine as engine
import high_probability_workspace as workspace
import scenario_tables
from probability_reliability_panel import render_probability_reliability
from ux_theme import render_page_header


LOCAL_ZONE = ZoneInfo('Africa/Kinshasa')
VALUES = ('1st', 'Égal', '2nd')
OUTCOMES = {'1st': '1st · Avant la pause',
            'Égal': 'Égal · Autant dans chaque période', '2nd': '2nd · Après la pause'}
COLORED_OUTCOMES = {'1st': ':blue[1st]', 'Égal': ':violet[Égal]', '2nd': ':orange[2nd]'}
MINIMUM_KEYS = {'1st': 'hp_minimum_1st', 'Égal': 'hp_minimum_equal', '2nd': 'hp_minimum_2nd'}
ERRORS = (api.ApiFootballError, ValueError, OSError)


def _now():
    return datetime.now(LOCAL_ZONE)


def _stamp(value):
    value = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _id(row):
    return str(row['fixture']['match_id'])


def _competition(fixture):
    return ' · '.join(str(value) for value in (fixture.get('country'),
                    fixture.get('competition') or fixture.get('league')) if value)


def _generated(value):
    try:
        return _stamp(value).astimezone(LOCAL_ZONE).strftime('%d/%m à %H:%M:%S')
    except (ValueError, TypeError):
        return 'indisponible'


def _remember_minimum(outcome):
    thresholds = dict(st.session_state.get('hp_thresholds', {}))
    thresholds[outcome] = st.session_state[MINIMUM_KEYS[outcome]]
    st.session_state['hp_thresholds'] = thresholds


def _reset_match_page():
    st.session_state['hp_page'] = 0


def _is_future(fixture, now):
    try:
        stamp = _stamp(fixture['timestamp'])
        return stamp > now and stamp.astimezone(LOCAL_ZONE).date() == now.date()
    except (KeyError, ValueError, TypeError):
        return False


def _rows(report, now):
    if report.get('day') != now.date().isoformat():
        return []
    return sorted((row for row in report.get('rows', []) if _is_future(row['fixture'], now)),
                  key=lambda row: (-row['max_probability'], _stamp(row['fixture']['timestamp']), int(_id(row))))


def _fingerprint(report, rows, minimum, outcome, query):
    data = {'day': report.get('day'), 'before': report.get('before'),
            'history_fingerprint': report.get('history_fingerprint'), 'version': report.get('version'),
            'rows': [(row['fixture'], row['probabilities'], row.get('baseline'),
                      row.get('history_fingerprint')) for row in rows],
            'minimum': minimum, 'outcome': outcome, 'query': query}
    return sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _table_rows(rows, *, offset=0, outcome=None):
    values = []
    for rank, row in enumerate(rows, offset + 1):
        fixture = row['fixture']
        searched = outcome or row.get('search_outcome') or row['choice']
        probability = row['probabilities'][searched]
        baseline = (row.get('baseline') or {}).get(searched)
        values.append({'Rang': rank,
                       'Heure (Kinshasa)': _stamp(fixture['timestamp']).astimezone(LOCAL_ZONE).strftime('%H:%M'),
                       'Match': f"{fixture['home']} — {fixture['away']}",
                       'Compétition': _competition(fixture),
                       'Résultat recherché': [searched],
                       'Probabilité recherchée (%)': 100 * probability,
                       'Tendance du championnat (%)': 100 * baseline if baseline is not None else None,
                       'Écart à la tendance (points)': 100 * (probability - baseline) if baseline is not None else None,
                       **{value: 100 * row['probabilities'][value] for value in VALUES},
                       'Résultat le plus probable': [row['choice']],
                       'Maximum (%)': 100 * row['max_probability']})
    return values


def _show_table(rows, *, key, offset=0, outcome=None):
    st.dataframe(_table_rows(rows, offset=offset, outcome=outcome), hide_index=True, width='stretch',
                 column_config={**{value: st.column_config.NumberColumn(f'{value} (%)', format='%.2f')
                                   for value in VALUES},
                                'Maximum (%)': st.column_config.NumberColumn(format='%.2f'),
                                'Probabilité recherchée (%)': st.column_config.NumberColumn(format='%.2f'),
                                'Tendance du championnat (%)': st.column_config.NumberColumn(format='%.2f'),
                                'Écart à la tendance (points)': st.column_config.NumberColumn(format='%+.2f'),
                                'Résultat recherché': st.column_config.MultiselectColumn(
                                    options=list(VALUES), color=['#1D4ED8', '#6D48B5', '#B45309']),
                                'Résultat le plus probable': st.column_config.MultiselectColumn(
                                    options=list(VALUES), color=['#1D4ED8', '#6D48B5', '#B45309'])}, key=key)


def _prepare(report, chosen, outcome, minimum):
    try:
        with st.spinner('Calcul des trois scénarios du groupe…'):
            prepared = workspace.prepare_group(report, chosen, outcome=outcome, minimum=minimum)
        prepared.setdefault('metadata', {}).setdefault('selection', {}).setdefault('search', {}).update(
            minimum=minimum, outcome=outcome, query=st.session_state.get('hp_search', '').strip())
        st.session_state['hp_prepared'] = prepared
        st.session_state['hp_prepared_kind'] = 'ranking'
        st.session_state.pop('hp_saved', None)
    except ERRORS as error:
        st.error(str(error))


def _clear_prepared(*, kind=None):
    if kind is None or st.session_state.get('hp_prepared_kind', 'ranking') == kind:
        for key in ('hp_prepared', 'hp_saved', 'hp_prepared_kind'):
            st.session_state.pop(key, None)


def _remember_mixed_ids():
    st.session_state['hp_mix_ids'] = list(st.session_state.get('hp_mix_widget_ids', []))


def _remember_mixed_choice(identity):
    choices = dict(st.session_state.get('hp_mix_choices', {}))
    choices[identity] = st.session_state[f'hp_mix_choice_{identity}']
    st.session_state['hp_mix_choices'] = choices


def _mixed_builder(report, rows, now):
    """A separate basket: ranking filters never narrow or reset its choices."""
    st.subheader('Composer mon scénario mixte', icon=':material/tune:')
    st.caption('Choisissez de 2 à 20 matchs parmi toutes les compétitions, puis un résultat pour chacun. '
               'Ce panier est indépendant du classement et de ses filtres.')
    ordered = sorted(rows, key=lambda row: (_stamp(row['fixture']['timestamp']), int(_id(row))))
    indexed = {_id(row): row for row in ordered}
    day = now.date().isoformat()
    if st.session_state.get('hp_mix_day') != day:
        st.session_state['hp_mix_day'] = day
        st.session_state['hp_mix_ids'] = []
        st.session_state['hp_mix_choices'] = {}
        st.session_state.pop('hp_mix_widget_ids', None)
    selected = [identity for identity in st.session_state.get('hp_mix_ids', []) if identity in indexed]
    st.session_state['hp_mix_ids'] = selected
    if 'hp_mix_widget_ids' not in st.session_state:
        st.session_state['hp_mix_widget_ids'] = selected
    else:
        current = st.session_state['hp_mix_widget_ids']
        kept = [identity for identity in current if identity in indexed]
        if kept != current:
            st.session_state['hp_mix_widget_ids'] = kept
    choices = dict(st.session_state.get('hp_mix_choices', {}))
    with st.container(border=True):
        chosen = st.multiselect('Matchs de mon scénario mixte', list(indexed),
            format_func=lambda identity: (
                f"{_stamp(indexed[identity]['fixture']['timestamp']).astimezone(LOCAL_ZONE):%H:%M} · "
                f"{indexed[identity]['fixture']['home']} — {indexed[identity]['fixture']['away']} · "
                f"{_competition(indexed[identity]['fixture'])}"),
            key='hp_mix_widget_ids', max_selections=20, select_all=False,
            placeholder='Ajouter les matchs, toutes compétitions confondues',
            on_change=_remember_mixed_ids)
        chosen = [identity for identity in indexed if identity in chosen]
        st.session_state['hp_mix_ids'] = chosen
        for position, identity in enumerate(chosen, 1):
            row = indexed[identity]
            fixture = row['fixture']
            widget_key = f'hp_mix_choice_{identity}'
            if choices.get(identity) not in VALUES:
                choices[identity] = row['choice']
            if st.session_state.get(widget_key) not in VALUES:
                st.session_state[widget_key] = choices[identity]
            with st.container(border=True):
                st.markdown(f"**{position}. {fixture['home']} — {fixture['away']}**")
                st.caption(f"{_stamp(fixture['timestamp']).astimezone(LOCAL_ZONE):%H:%M} · {_competition(fixture)}")
                choice = st.segmented_control(f'Résultat pour le match {position}', VALUES,
                    format_func=lambda value, probabilities=row['probabilities']:
                        f"{COLORED_OUTCOMES[value]} · {100 * probabilities[value]:.2f} %",
                    key=widget_key, required=True, on_change=_remember_mixed_choice, args=(identity,))
                choices[identity] = choice
                baseline = (row.get('baseline') or {}).get(choice)
                probability = row['probabilities'][choice]
                if baseline is None:
                    st.caption(f'Probabilité choisie : {100 * probability:.2f} % · Tendance du championnat indisponible.')
                else:
                    st.caption(f'Probabilité choisie : {100 * probability:.2f} % · '
                               f'Tendance du championnat : {100 * baseline:.2f} % · '
                               f'Écart : {100 * (probability - baseline):+.2f} points')
        st.session_state['hp_mix_choices'] = choices
        context = (_fingerprint(report, rows, 0, None, ''), tuple((identity, choices[identity]) for identity in chosen))
        if st.session_state.get('hp_mix_context') != context:
            st.session_state['hp_mix_context'] = context
            _clear_prepared(kind='mixed')
        if not rows:
            st.info('Aucun match à venir avec des probabilités exploitables pour composer un scénario.')
        if st.button('Calculer mon scénario mixte', key='hp_mix_prepare', icon=':material/schema:',
                     type='primary', disabled=not 2 <= len(chosen) <= 20):
            try:
                with st.spinner('Calcul du scénario choisi et des trois premiers scénarios…'):
                    prepared = workspace.prepare_group(report, chosen,
                                                       choices={identity: choices[identity] for identity in chosen})
                st.session_state['hp_prepared'] = prepared
                st.session_state['hp_prepared_kind'] = 'mixed'
                st.session_state.pop('hp_saved', None)
            except ERRORS as error:
                st.error(str(error))


def _prepared_detail(report, indexed, now):
    prepared = st.session_state.get('hp_prepared')
    if not prepared:
        return
    saved = st.session_state.get('hp_saved')
    source = saved if saved else prepared
    fixtures = source.get('fixtures', [])
    if any(not _is_future(fixture, now) or str(fixture['match_id']) not in indexed for fixture in fixtures):
        _clear_prepared()
        st.info('Le groupe a expiré : un coup d’envoi est passé ou le calendrier a changé. Choisissez un nouveau groupe.')
        return
    st.subheader(f'Votre analyse · {len(fixtures)} matchs', icon=':material/schema:')
    st.caption('Ordre des matchs : heure du coup d’envoi, puis identifiant en cas d’égalité.')
    intent = source.get('scenario_intent') or {}
    competitions = intent.get('competitions') or source.get('metadata', {}).get('competitions', {})
    ordered = []
    for fixture in fixtures:
        row = indexed[str(fixture['match_id'])]
        labels = competitions.get(str(fixture.get('league')), {})
        displayed = dict(fixture)
        for name in ('country', 'competition'):
            displayed[name] = fixture.get(name) or labels.get(name) or row['fixture'].get(name)
        if not displayed.get('competition'):
            displayed['competition'] = (f"Compétition n°{fixture['league']}" if fixture.get('league')
                                        else 'Compétition indisponible')
        ordered.append(dict(row, fixture=displayed))
    selection = intent.get('selection') or source.get('metadata', {}).get('selection', {})
    requested = selection.get('requested_scenario') or source.get('requested_scenario')
    if not saved or saved.get('created', True) or intent:
        snapshots = {str(item['match_id']): item for item in selection.get('match_probabilities', [])}
        ordered = [dict(row, **{name: value for name, value in snapshots.get(_id(row), {}).items()
                              if name != 'match_id'}) for row in ordered]
        searched = selection.get('search', {}).get('outcome')
        _show_table(ordered, key='hp_selected_probabilities', outcome=searched if searched in VALUES else None)
    st.caption('Le seuil concerne chaque match. La probabilité d’un scénario complet est plus faible : '
               'elle combine les probabilités des résultats de tous les matchs, sous une hypothèse d’indépendance.')
    if requested:
        st.subheader('Scénario recherché', icon=':material/manage_search:')
        scenario_tables.render_sequence(requested['sequence'])
        st.metric('Probabilité du scénario recherché', f"{100 * requested['probability']:.2f} %")
        st.caption('Ce scénario suit le résultat recherché pour chaque match. Il peut être différent des trois scénarios les mieux classés ci-dessous.')
    scenario_tables.render_matrix(saved['methods'] if saved else prepared['methods'], key='hp_scenario_matrix',
                                  title='Scénarios enregistrés avant match' if saved else 'Les trois scénarios les mieux classés · Clubs')
    if saved:
        if intent:
            st.success('Scénario enregistré sur la prévision figée du groupe. '
                       'Son résultat sera suivi dans l’historique.')
            if saved.get('forecast_created') is False:
                st.caption('Ce groupe avait déjà une prévision : ses probabilités d’origine sont conservées pour ce nouveau choix.')
        elif saved.get('created') is False:
            st.info('Ce groupe avait déjà été enregistré. Sa première prévision est conservée et affichée ci-dessus.')
        else:
            st.success('Analyse enregistrée avant match. Ses scénarios restent figés et les résultats seront suivis dans l’historique.')
        if st.button('Voir le suivi de cette analyse', icon=':material/arrow_forward:', key='hp_open_journal'):
            if saved.get('group_kind') == 'mixed':
                day = now.date().isoformat()
                st.session_state['daily_context'] = (day, 'today', day)
                st.session_state['daily_view'] = 'today'
                st.session_state['daily_selected'] = 'intent:' + intent['id'] if intent else saved['id']
                st.switch_page('ui_pages/daily.py')
            else:
                st.switch_page('ui_pages/journal.py', query_params={'analysis': saved['id']})
    elif st.button('Enregistrer avant match', icon=':material/bookmark_add:', type='primary', key='hp_record'):
        try:
            with st.spinner('Confirmation des rencontres dans l’API et enregistrement…'):
                st.session_state['hp_saved'] = workspace.record_group(prepared)
            st.rerun()
        except ERRORS as error:
            st.error(str(error))


@st.fragment(run_every=60)
def render_high_probabilities():
    now = _now()
    render_page_header('Fortes probabilités',
                       'Les plus hauts pourcentages du jour, toutes compétitions confondues.',
                       eyebrow='MATCHS DU JOUR · CLASSEMENT PAR PROBABILITÉ', icon=':material/trending_up:')
    st.caption('Modèle Clubs · API-Football · Heure de Kinshasa · Rencontres à venir uniquement.')
    st.caption('Ces pourcentages sont des estimations du modèle, pas des taux de réussite mesurés.')
    try:
        report = workspace.load_scan()
    except ERRORS as error:
        st.error(str(error))
        return
    if st.button('Actualiser les matchs du jour', icon=':material/refresh:', key='hp_refresh'):
        try:
            with st.spinner('Actualisation du calendrier du jour et des probabilités…'):
                report = workspace.refresh_scan()
            _clear_prepared()
        except ERRORS as error:
            st.error(str(error))
    if not report:
        st.info('Aucun calendrier local disponible. Actualisez les matchs du jour pour lancer la recherche.')
        return
    rows = _rows(report, now)
    diagnostic = report.get('diagnostics') or {}
    st.caption(f"Matchs du {now.strftime('%d/%m/%Y')} · Calcul local : {_generated(report.get('generated_at'))} · "
               f"Calendrier API : {_generated(report.get('calendar_updated_at'))}")
    if report.get('day') != now.date().isoformat():
        _clear_prepared()
        for key in ('hp_mix_ids', 'hp_mix_choices', 'hp_mix_widget_ids', 'hp_mix_day'):
            st.session_state.pop(key, None)
        st.info('La journée a changé. Actualisez le calendrier pour rechercher uniquement les matchs d’aujourd’hui.')
        return
    current_report = dict(report, rows=rows)
    st.subheader('Trois classements indépendants', icon=':material/leaderboard:')
    st.caption('Chaque classement examine tous les matchs évaluables, même si le résultat recherché n’est pas leur résultat le plus probable.')
    with st.container(horizontal=True):
        for category in VALUES:
            ranked = engine.filter_rows(current_report, minimum=0, outcome=category)
            category_minimum = st.session_state.get('hp_thresholds', {}).get(category, 0) / 100
            best = f"{100 * ranked[0]['search_probability']:.2f} %" if ranked else '—'
            with st.container(border=True):
                st.metric(f'Meilleur {category}', best)
                above_fifty = sum(row['probabilities'][category] >= .5 for row in rows)
                above_category = sum(row['probabilities'][category] >= category_minimum for row in rows)
                st.caption(f'{above_fifty} match(s) ≥ 50 % · {above_category} au seuil de {category_minimum * 100:g} %')
    with st.container(border=True):
        if st.session_state.get('hp_outcome') not in VALUES:
            st.session_state['hp_outcome'] = '1st'
        outcome = st.segmented_control('Résultat recherché', list(OUTCOMES), format_func=OUTCOMES.get,
                                       required=True, key='hp_outcome')
        st.session_state.setdefault(MINIMUM_KEYS[outcome], st.session_state.get('hp_thresholds', {}).get(outcome, 0))
        with st.container(horizontal=True):
            minimum = st.number_input('Probabilité minimale du résultat recherché (%)', min_value=0, max_value=100,
                                      step=1, key=MINIMUM_KEYS[outcome], on_change=_remember_minimum,
                                      args=(outcome,)) / 100
            query = st.text_input('Rechercher un club ou une compétition', placeholder='Club, pays ou compétition',
                                  key='hp_search')
    st.caption('Le seuil est mémorisé séparément pour 1st, Égal et 2nd. Réglez-le à 50 % pour ne conserver que les résultats estimés à au moins une chance sur deux.')
    qualified = engine.filter_rows(current_report, minimum=minimum, outcome=outcome)
    visible = engine.filter_rows(current_report, minimum=minimum, outcome=outcome)
    needle = query.strip().casefold()
    if needle:
        visible = [row for row in visible if needle in (
            row['fixture']['home'] + ' ' + row['fixture']['away'] + ' ' + _competition(row['fixture'])).casefold()]
    context = _fingerprint(report, rows, minimum, outcome, needle)
    if st.session_state.get('hp_context') != context:
        st.session_state['hp_context'] = context
        _clear_prepared(kind='ranking')
        for key in ('hp_manual', 'hp_page'):
            st.session_state.pop(key, None)
    group_context = (context, st.session_state.get('hp_top_count', 2), tuple(st.session_state.get('hp_manual', [])))
    if st.session_state.get('hp_group_context') != group_context:
        st.session_state['hp_group_context'] = group_context
        _clear_prepared(kind='ranking')
    with st.container(horizontal=True):
        st.metric('Matchs du jour dans le calendrier', diagnostic.get('day_count', len(rows) + len(report.get('excluded', []))))
        st.metric('Matchs à venir évaluables', len(rows))
        st.metric(f'{outcome} · Au moins {minimum * 100:g} %', len(qualified))
        st.metric('Sous le seuil', len(rows) - len(qualified))
    st.caption(f'{len(visible)} rencontre(s) après les filtres · Classement décroissant sur la probabilité de {outcome}. '
               'La quantité d’historique ne sert pas de score de classement.')
    if not visible:
        st.info(f'Aucune rencontre ne correspond à ces filtres et au seuil de {minimum * 100:g} %. '
                'Le seuil reste inchangé ; aucun match moins probable n’est ajouté automatiquement.')
    else:
        with st.container(horizontal=True):
            saved_size = st.session_state.get('hp_page_size', 10)
            if type(saved_size) is not int or saved_size not in (10, 25, 50):
                st.session_state['hp_page_size'] = 10
            per_page = st.selectbox('Matchs par page', [10, 25, 50], key='hp_page_size',
                                    on_change=_reset_match_page)
            pages = max(1, (len(visible) + per_page - 1) // per_page)
            saved_page = st.session_state.get('hp_page', 0)
            if type(saved_page) is not int or not 0 <= saved_page < pages:
                st.session_state['hp_page'] = 0
            # Labels must stay stable when the page count changes: otherwise
            # Streamlit can restore an old label as text instead of an index.
            page = st.selectbox('Page des matchs', range(pages), format_func=lambda value: f'Page {value + 1}',
                                key='hp_page')
        _show_table(visible[page * per_page:(page + 1) * per_page], key='hp_ranked_matches', offset=page * per_page)
        st.caption(f'{page * per_page + 1}–{min((page + 1) * per_page, len(visible))} sur {len(visible)} matchs.')
        st.caption('La tendance du championnat vient des résultats historiques disponibles avant les matchs. '
                   'Un écart positif indique ce que les profils des clubs ajoutent à cette tendance ; '
                   'ce n’est pas une mesure de réussite future.')
        st.subheader('Construire un groupe', icon=':material/playlist_add:')
        st.caption('Choisissez de 2 à 20 matchs. Les compétitions peuvent être mélangées.')
        with st.container(border=True):
            with st.container(horizontal=True, vertical_alignment='bottom'):
                count = st.number_input('Nombre de matchs parmi les premiers', min_value=2, max_value=20,
                                        value=2, step=1, key='hp_top_count')
                if st.button('Analyser les premiers matchs', icon=':material/auto_awesome:', key='hp_analyze_top',
                             disabled=len(visible) < count):
                    _prepare(report, [_id(row) for row in visible[:count]], outcome, minimum)
            if len(visible) < count:
                st.caption(f'{count} matchs nécessaires ; {len(visible)} disponible(s) avec ces filtres.')
            indexed_visible = {_id(row): row for row in visible}
            manual = st.multiselect('Ou choisir les matchs du groupe', list(indexed_visible),
                format_func=lambda value: f"{outcome} · {100 * indexed_visible[value]['search_probability']:.2f} % · "
                    f"{indexed_visible[value]['fixture']['home']} — {indexed_visible[value]['fixture']['away']} · "
                    f"{_competition(indexed_visible[value]['fixture'])}", max_selections=20, key='hp_manual',
                placeholder='Sélectionnez de 2 à 20 matchs', select_all=False)
            if st.button('Analyser ma sélection', icon=':material/schema:', key='hp_analyze_manual',
                         disabled=not 2 <= len(manual) <= 20):
                _prepare(report, manual, outcome, minimum)
    _mixed_builder(report, rows, now)
    _prepared_detail(report, {_id(row): row for row in rows}, _now())
    excluded = report.get('excluded') or []
    if excluded:
        with st.expander('Rencontres écartées et couverture des données'):
            st.caption('Les rencontres terminées, commencées, reportées ou sans historique suffisant '
                       'ne sont pas proposées pour une nouvelle analyse avant match.')
            st.dataframe([{'Match': ' — '.join(str((item.get('fixture') or {}).get(name, ''))
                                             for name in ('home', 'away')).strip(' —') or str(item.get('match_id', '—')),
                           'Raison': item.get('detail') or item.get('reason', 'Indisponible')}
                          for item in excluded], hide_index=True, width='stretch', key='hp_excluded')
    render_probability_reliability()
