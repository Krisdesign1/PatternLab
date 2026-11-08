"""Explore a selected day's highest model probabilities across competitions.

Rendering and filtering only read the local scan. Calendar refresh, scenario
preparation, and the immutable pre-match journal entry are explicit actions.
"""
import table_views
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
from zoneinfo import ZoneInfo

import streamlit as st
import admin_access
from cloud_ui import guarded
from analysis_date import get_selected_day

import api_football as api
import high_probability_engine as engine
import high_probability_workspace as workspace
import scenario_tables
import history_support as support
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


def _open_saved_history():
    saved = st.session_state.get('hp_saved') or {}
    intent = saved.get('scenario_intent') or {}
    st.session_state['hp_history_selected'] = 'intent:' + intent['id'] if intent else saved.get('id')
    st.session_state['hp_view'] = 'groups'
    st.session_state.update(hp_history_day='all', hp_history_state='all', hp_history_search='',
                            hp_history_outcome='all', hp_history_verdict='all')


def _is_future(fixture, now, day=None):
    try:
        stamp = _stamp(fixture['timestamp'])
        target_day = now.astimezone(LOCAL_ZONE).date() if day is None else day
        return stamp > now and stamp.astimezone(LOCAL_ZONE).date() == target_day
    except (KeyError, ValueError, TypeError):
        return False


def _rows(report, now, day=None):
    day = now.astimezone(LOCAL_ZONE).date() if day is None else day
    if report.get('day') != day.isoformat():
        return []
    return sorted((row for row in report.get('rows', []) if _is_future(row['fixture'], now, day)),
                  key=lambda row: (-row['max_probability'], _stamp(row['fixture']['timestamp']), int(_id(row))))


def _show_unavailable(report):
    """Explain missing inputs separately from a user's probability filters."""
    diagnostic = report.get('diagnostics') or {}
    reasons = Counter(item.get('reason') for item in report.get('excluded', []))
    missing_history = sum(reasons[name] for name in (
        'insufficient_league_history', 'insufficient_history_days', 'insufficient_club_history'))
    if diagnostic.get('day_count') == 0:
        st.info('Le calendrier de la date sélectionnée n’est pas chargé sur cette installation. '
                'Utilisez « Actualiser les matchs » pour charger les affiches.')
    elif missing_history:
        st.warning(f'{missing_history} rencontre(s) à venir écartée(s) : '
                   'historique de compétition ou profils des clubs insuffisants. '
                   'Aucun pourcentage personnalisé ne peut être affiché pour ces rencontres.')
        if diagnostic.get('history_count') == 0:
            st.caption('Aucun résultat antérieur exploitable dans les compétitions des rencontres à venir. '
                       'Les résultats du jour ne servent pas à prédire ces mêmes rencontres.')
        st.caption('Il faut au moins 30 résultats de la compétition sur 3 journées UTC antérieures, '
                   'avec 3 précédents à domicile pour le club recevant et 3 à l’extérieur pour son adversaire.')
        st.caption('Actualiser charge le calendrier de la date sélectionnée ; cela n’importe pas les saisons historiques. '
                   'L’historique doit être disponible sur le serveur en ligne pour tous les appareils.')
        st.page_link('ui_pages/settings.py', label='Importer l’historique API-Football', icon=':material/database:')
        st.page_link('ui_pages/automation.py', label='Voir les données manquantes', icon=':material/checklist:')
    elif diagnostic.get('upcoming_count') == 0 or reasons['kickoff_passed']:
        st.info('Aucun match confirmé encore à venir n’est disponible pour ce classement dans le relevé actuel. '
                'Les matchs commencés et terminés ne reçoivent pas de nouvelle prévision avant match.')
    else:
        st.info('Aucun match ne possède actuellement les données nécessaires au calcul. '
                'Les raisons figurent dans « Rencontres écartées et couverture des données ».')


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
        history = support.assess(row)
        counts = history['counts']
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
                       'Maximum (%)': 100 * row['max_probability'],
                       'Résultats de compétition': counts['league_matches'],
                       'Journées historiques': counts['history_days'],
                       'Précédents domicile': counts['home_matches'],
                       'Précédents extérieur': counts['away_matches'],
                       'Historique · filtre prudent': 'Seuils atteints' if history['eligible'] else
                           'Effectifs inconnus' if history['missing'] else 'Sous les seuils'})
    return values


def _show_table(rows, *, key, offset=0, outcome=None):
    table_views.dataframe(_table_rows(rows, offset=offset, outcome=outcome), hide_index=True, width='stretch',
                 column_config={**{value: st.column_config.NumberColumn(f'{value} (%)', format='%.2f')
                                   for value in VALUES},
                                'Maximum (%)': st.column_config.NumberColumn(format='%.2f'),
                                'Probabilité recherchée (%)': st.column_config.NumberColumn(format='%.2f'),
                                'Tendance du championnat (%)': st.column_config.NumberColumn(format='%.2f'),
                                'Écart à la tendance (points)': st.column_config.NumberColumn(format='%+.2f'),
                                'Résultat recherché': st.column_config.MultiselectColumn(
                                    options=list(VALUES), color=['#1D4ED8', '#6D48B5', '#B45309']),
                                'Résultat le plus probable': st.column_config.MultiselectColumn(
                                    options=list(VALUES), color=['#1D4ED8', '#6D48B5', '#B45309'])}, key=key, table_id='high_probability_panel:_show_table:dataframe:1')


def _prepare(report, chosen, outcome, minimum):
    try:
        with st.spinner('Calcul des trois scénarios du groupe…'):
            prepared = workspace.prepare_group(report, chosen, outcome=outcome, minimum=minimum)
        prepared.setdefault('metadata', {}).setdefault('selection', {}).setdefault('search', {}).update(
            minimum=minimum, outcome=outcome, query=st.session_state.get('hp_search', '').strip())
        prepared['metadata']['selection']['history_filter'] = st.session_state.get('hp_support_criteria', {})
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
    """A separate basket within the selected history support population."""
    st.subheader('Composer mon scénario mixte', icon=':material/tune:')
    st.caption('Choisissez de 2 à 20 matchs parmi toutes les compétitions, puis un résultat pour chacun. '
               'Le filtre d’historique s’applique aussi à ce panier ; le résultat recherché, '
               'le seuil de probabilité et la recherche du classement ne le limitent pas.')
    ordered = sorted(rows, key=lambda row: (_stamp(row['fixture']['timestamp']), int(_id(row))))
    indexed = {_id(row): row for row in ordered}
    day = report.get('day')
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
        context = (_fingerprint(report, rows, 0, None, ''), tuple((identity, choices[identity]) for identity in chosen),
                   tuple(sorted(st.session_state.get('hp_support_criteria', {}).items())))
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
                prepared.setdefault('metadata', {}).setdefault('selection', {})['history_filter'] = dict(
                    st.session_state.get('hp_support_criteria', {}))
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
    day = datetime.strptime(report['day'], '%Y-%m-%d').date()
    if (prepared.get('day', report['day']) != report['day'] or
            any(not _is_future(fixture, now, day) or str(fixture['match_id']) not in indexed for fixture in fixtures)):
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
        st.caption('Ce scénario suit le résultat recherché pour chaque match. Il peut être différent des scénarios retenus par le moteur.')
    from adaptive_scenario_proposals import effective_methods
    if saved:
        scenario_tables.render_archive_scope(saved)
    scenario_tables.render_matrix(effective_methods(saved) if saved else prepared['methods'], key='hp_scenario_matrix',
                                  title='Scénarios enregistrés avant match' if saved else 'Classement de référence · Clubs')
    if not saved and not admin_access.can_admin():
        st.caption('Calcul local non enregistré. Une connexion administrateur est nécessaire pour la confirmation API avant match.')
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
        st.button('Voir le suivi de cette analyse', icon=':material/arrow_forward:', key='hp_open_journal',
                  on_click=_open_saved_history)
    elif st.button('Enregistrer avant match', icon=':material/bookmark_add:', type='primary', key='hp_record', disabled=not admin_access.can_admin()):
        admin_access.require_admin()
        try:
            with st.spinner('Confirmation des rencontres dans l’API et enregistrement…'):
                st.session_state['hp_saved'] = workspace.record_group(prepared)
            st.rerun()
        except ERRORS as error:
            st.error(str(error))


@st.fragment(run_every=60)
@guarded
def render_high_probabilities():
    now = _now()
    day = get_selected_day(now)
    previous_day = st.session_state.get('hp_selected_day')
    if previous_day is not None and previous_day != day.isoformat():
        _clear_prepared()
        for key in ('hp_mix_ids', 'hp_mix_choices', 'hp_mix_widget_ids', 'hp_mix_day', 'hp_manual', 'hp_page'):
            st.session_state.pop(key, None)
    st.session_state['hp_selected_day'] = day.isoformat()
    render_page_header('Fortes probabilités',
                       f"Les plus hauts pourcentages du {day:%d/%m/%Y}, toutes compétitions confondues.",
                       eyebrow='DATE SÉLECTIONNÉE · CLASSEMENT PAR PROBABILITÉ', icon=':material/trending_up:')
    view = st.segmented_control('Consulter les fortes probabilités', ['ranking', 'history', 'groups'],
        format_func={'ranking': 'Classement de la date choisie', 'history': 'Historique des probabilités',
                     'groups': 'Mes groupes enregistrés'}.get,
        default='ranking', required=True, key='hp_view', persist_state='page')
    if view == 'history':
        from high_probability_archive_panel import render_archive_history
        render_archive_history(now)
        return
    if view == 'groups':
        from high_probability_history_panel import render_history
        render_history(now)
        return
    st.caption('Modèle Clubs · API-Football · Heure de Kinshasa · Rencontres à venir uniquement.')
    st.caption('Ces pourcentages sont des estimations du modèle, pas des taux de réussite mesurés.')
    st.caption('Les probabilités de tous les matchs calculables sont archivées automatiquement avant match, '
               'indépendamment de vos groupes et des filtres de ce classement.')
    try:
        report = workspace.load_scan(now=now, day=day)
    except ERRORS as error:
        st.error(str(error))
        return
    if st.button('Actualiser les matchs', icon=':material/refresh:', key='hp_refresh', disabled=not admin_access.can_admin()):
        admin_access.require_admin()
        try:
            with st.spinner('Actualisation du calendrier sélectionné et des probabilités…'):
                report = workspace.refresh_scan(day=day)
            _clear_prepared()
        except ERRORS as error:
            st.error(str(error))
    if not report:
        st.info('Aucun calendrier local disponible. Actualisez les matchs pour lancer la recherche.')
        return
    rows = _rows(report, now, day)
    diagnostic = report.get('diagnostics') or {}
    st.caption(f"Matchs du {day.strftime('%d/%m/%Y')} · Calcul local : {_generated(report.get('generated_at'))} · "
               f"Calendrier API : {_generated(report.get('calendar_updated_at'))}")
    if report.get('day') != day.isoformat():
        _clear_prepared()
        for key in ('hp_mix_ids', 'hp_mix_choices', 'hp_mix_widget_ids', 'hp_mix_day'):
            st.session_state.pop(key, None)
        st.info('La journée a changé. Actualisez le calendrier pour rechercher les matchs de la date sélectionnée.')
        return
    if admin_access.can_admin():
        try:
            workspace.archive_scan(report)
        except ERRORS:
            st.warning('L’archivage automatique est indisponible. Les pourcentages ci-dessous restent consultables, '
                       'mais leur conservation dans l’historique n’est pas confirmée.')
    if not rows:
        _show_unavailable(report)
    all_rows = rows
    with st.container(border=True):
        preset = st.selectbox('Historique utilisé pour la sélection', ['prudent', 'minimum', 'custom'],
            format_func={'prudent':'Filtre prudent · 100 résultats, 10 journées, 8 précédents par club',
                         'minimum':'Tous les matchs calculables · minimum du modèle',
                         'custom':'Seuils personnalisés'}.get, key='hp_support_preset')
        criteria = dict(support.PRUDENT if preset == 'prudent' else support.MINIMUM)
        if preset == 'custom':
            with st.container(horizontal=True):
                criteria['league_matches'] = st.number_input('Résultats de compétition minimum', min_value=30,
                    value=100, step=10, key='hp_support_league')
                criteria['history_days'] = st.number_input('Journées historiques minimum', min_value=3,
                    value=10, step=1, key='hp_support_days')
                criteria['team_matches'] = st.number_input('Précédents minimum par club dans son rôle', min_value=3,
                    value=8, step=1, key='hp_support_team')
        st.session_state['hp_support_criteria'] = dict(version=support.VERSION, **criteria)
        rows = support.select(rows, criteria)
        st.caption(f'{len(rows)} / {len(all_rows)} matchs calculables satisfont ces effectifs. '
                   'Ces seuils filtrent la quantité d’historique ; ils ne certifient ni la précision ni la calibration. '
                   'Le tri reste fondé sur la probabilité du résultat choisi.')
        if len(rows) < len(all_rows):
            st.caption('Choisissez « Tous les matchs calculables » pour consulter les profils sous le filtre prudent. '
                       'Aucun pourcentage n’est modifié et les prévisions enregistrées restent inchangées.')
        if all_rows and not rows:
            st.info('Aucun match ne satisfait ces effectifs pour le moment. '
                    'Les autres matchs restent consultables avec « Tous les matchs calculables ».')
    current_report = dict(report, rows=rows)
    st.subheader('Trois classements indépendants', icon=':material/leaderboard:')
    st.caption('Chaque classement examine les matchs retenus par le filtre d’historique, '
               'même si le résultat recherché n’est pas leur résultat le plus probable.')
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
    context = (_fingerprint(report, rows, minimum, outcome, needle), tuple(sorted(criteria.items())))
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
        st.metric('Matchs de cette date dans le calendrier', diagnostic.get('day_count', len(rows) + len(report.get('excluded', []))))
        st.metric('Matchs à venir évaluables', len(rows))
        st.metric(f'{outcome} · Au moins {minimum * 100:g} %', len(qualified))
        st.metric('Sous le seuil', len(rows) - len(qualified))
    st.caption(f'{len(visible)} rencontre(s) après les filtres · Classement décroissant sur la probabilité de {outcome}. '
               'La quantité d’historique ne sert pas de score de classement.')
    if not visible and rows:
        st.info(f'Aucune rencontre ne correspond à ces filtres et au seuil de {minimum * 100:g} %. '
                'Le seuil reste inchangé ; aucun match moins probable n’est ajouté automatiquement.')
    elif visible:
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
            table_views.dataframe([{'Match': ' — '.join(str((item.get('fixture') or {}).get(name, ''))
                                             for name in ('home', 'away')).strip(' —') or str(item.get('match_id', '—')),
                           'Raison': item.get('detail') or item.get('reason', 'Indisponible')}
                          for item in excluded], hide_index=True, width='stretch', key='hp_excluded', table_id='high_probability_panel:render_high_probabilities:dataframe:1')
    render_probability_reliability()
