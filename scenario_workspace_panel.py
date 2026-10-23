"""A date-first path from real fixtures to explained, contextual scenarios."""
from datetime import datetime, timezone
import json
from zoneinfo import ZoneInfo

import streamlit as st

import workspace_sources as sources
import catboost_scenarios as ml
import context_neighbors as neighbors
import context_neighbors_panel
import contextual_validation
import calendar_panel as calendar
import journal_tracking as tracking
import prediction_journal as journal
import competition_readiness as readiness
import competition_browser
import scenario_presentation as presentation
import scenario_tables
from contextual_scenarios import forecast, evaluate
from league_patterns import repeated_scenarios
from ux_theme import render_page_header

LOCAL_ZONE = ZoneInfo('Africa/Kinshasa')
LABELS = {'TIMED': 'À venir', 'SCHEDULED': 'À confirmer', 'FINISHED': 'Terminé',
          'IN_PLAY': 'En cours', 'PAUSED': 'Mi-temps', 'POSTPONED': 'Reporté',
          'CANCELLED': 'Annulé', 'SUSPENDED': 'Suspendu', 'AWARDED': 'Attribué'}


def _signature():
    return sources.data_signature(sources.selected_source())


@st.cache_data(ttl=60, max_entries=12, show_spinner=False)
def _snapshot(day, signature, source):
    return sources.local_snapshot(source, day)


@st.cache_data(max_entries=8, show_spinner=False)
def _history(signature, source):
    return sources.load_matches(source)


@st.cache_data(ttl=60, max_entries=8, show_spinner=False)
def _readiness_history(signature, source):
    # A local read, independent from the explicit prediction/import action.
    # Cache across group selections; never call an API or construct a forecast.
    return sources.load_matches(source)[0]


def _show_group_readiness(chosen, source, league):
    before = min(_local_stamp(row['timestamp']).astimezone(timezone.utc).date().isoformat() for row in chosen)
    try:
        rows = _readiness_history(_signature(), source)
        state = readiness.assess_history(rows, league, len(chosen), before)
    except (sources.ERRORS + (ml.ModelError, ValueError, OSError)) as error:
        st.warning('État de l’historique indisponible : ' + str(error))
        return
    st.badge('Historique prêt pour ce groupe' if state['eligible'] else 'Historique à compléter pour ce groupe',
             color='green' if state['eligible'] else 'orange', icon=':material/database:')
    with st.container(horizontal=True):
        st.metric('Groupes historiques complets', f"{state['history_groups']} / {state['min_groups']} minimum")
        st.metric('Résultats utilisables avant les matchs', state['history_matches'])
    st.caption(state['reason'])
    if not state['eligible']:
        st.info(f"Au moins {state['missing_matches']} résultat(s) antérieur(s) supplémentaire(s) sont nécessaires "
                f"pour ce groupe de {state['size']} matchs. "
                'Le bouton de classement peut compléter les saisons accessibles dans l’API.')
    st.caption('Cet état concerne Clubs + répétitions et vérifie la quantité d’historique. '
               'Les performances réellement mesurées sont présentées dans le journal des prédictions.')


@st.cache_data(max_entries=16, show_spinner=False)
def _estimate(result, fixtures, source):
    return forecast(result, fixtures)


@st.cache_data(max_entries=12, show_spinner=False)
def _evaluate(result, source):
    return evaluate(result)


@st.cache_data(max_entries=16, show_spinner=False)
def _ml_estimate(rows, fixtures, source, generation):
    return ml.forecast(rows, fixtures, source)


@st.cache_data(max_entries=16, show_spinner=False)
def _context_estimate(rows, fixtures, source, generation):
    return neighbors.forecast(rows, fixtures, source)


def _local_stamp(timestamp):
    parsed = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(LOCAL_ZONE)


def _today():
    return datetime.now(LOCAL_ZONE).date()


def _require_today(day, fixtures=()):
    if day != _today() or any(_local_stamp(row['timestamp']).date() != day for row in fixtures):
        raise ValueError('La journée a changé ou la sélection contient un match d’une autre date. '
                         'Actualisez les matchs du jour avant de relancer le classement.')


def _day_snapshot(snapshot, day):
    """Project local display data onto today's Kinshasa day without changing storage."""
    def in_day(row):
        try:
            return _local_stamp(row['timestamp']).date() == day
        except (KeyError, ValueError, TypeError, AttributeError):
            return False

    fixtures = [row for row in snapshot['fixtures'] if in_day(row)]
    display = calendar.display_rows(snapshot, day) if 'matches' in snapshot else None
    counts = {}
    for row in display if display is not None else fixtures:
        league = str(row['league'])
        counts[league] = counts.get(league, 0) + 1
    competitions = {}
    for league, count in counts.items():
        info = snapshot['competitions'].get(league)
        if info is None:
            continue
        competitions[league] = dict(info, matches=count,
            upcoming=sum(str(row['league']) == league and row.get('is_upcoming', False) for row in fixtures),
            usable=sum(str(row['league']) == league and row.get('is_historical', False) for row in fixtures))
    projected = dict(snapshot, day=day.isoformat(), fixtures=fixtures, competitions=competitions,
                     day_count=sum(counts.values()), upcoming=[row for row in fixtures if row.get('is_upcoming')])
    if display is not None:
        ids = {row['match_id'] for row in display}
        projected['matches'] = [row for row in snapshot['matches']
                                if isinstance(row, dict) and isinstance(row.get('fixture'), dict)
                                and str(row['fixture'].get('id')) in ids]
    return projected


def _match_table(fixtures):
    return [{'Ordre': i + 1, 'Heure à Kinshasa': _local_stamp(row['timestamp']).strftime('%H:%M'),
             'Domicile': row['home'], 'Extérieur': row['away']}
            for i, row in enumerate(fixtures)]


def _history_result(rows, league, size):
    selected = [row for row in rows if str(row['league']) == str(league)]
    return repeated_scenarios(selected, size).get(str(league),
            {'size': size, 'matches': [], 'blocks': [], 'pending': [], 'ranking': [], 'simultaneous': False})


def _show_results(payload):
    estimate, fixtures, result = payload['forecast'], payload['fixtures'], payload['result']
    st.subheader('Les scénarios de votre groupe', icon=':material/schema:')
    if not estimate['eligible']:
        if payload.get('engine') == 'context_neighbors':
            st.warning(estimate.get('diagnostic') or 'L’historique disponible est insuffisant pour ce groupe.')
            return
        st.warning('L’historique disponible ne permet pas encore de classer ce groupe : '
                   f"{estimate['training_groups']} groupes complets avant la date des matchs. "
                   'Il en faut au moins 20, avec 30 matchs répartis sur 3 jours.')
        return
    registered = payload.get('journal')
    if registered:
        if registered['state'] == 'recorded':
            st.success(registered['message'])
            if not registered.get('created', False):
                st.caption('Le journal conserve la première prévision de ce groupe. '
                           'Une nouvelle analyse affichée ici ne remplace pas les scénarios déjà enregistrés.')
            st.page_link('ui_pages/journal.py', label='Voir mon analyse enregistrée', icon=':material/history_edu:',
                         query_params={'analysis': registered['id']})
        elif registered['state'] == 'failed':
            st.warning('Analyse calculée, mais non enregistrée dans le suivi avant match : ' + registered['message'])
        else:
            st.caption(registered['message'])
    if payload.get('engine') == 'context_neighbors':
        context_neighbors_panel.render_result(payload)
        return
    learned = payload.get('engine') == 'catboost'
    st.caption(('CatBoost entraîné sur cette compétition, avec une correction des répétitions choisie sur validation. '
                if learned else 'Modèle combinant les habitudes des clubs et les répétitions de cette compétition. ') +
               'Les pourcentages concernent le scénario complet, dans l’ordre de votre sélection.')
    presentation.render_leader(estimate)
    presentation.render_analysis_matrix(payload)
    presentation.render_diagnostic(estimate,fixtures,engine=payload.get('engine','statistical'))
    st.caption('1st : plus de buts avant la pause · Égal : autant dans chaque mi-temps · '
               '2nd : plus de buts après la pause. Les trois probabilités ne totalisent pas nécessairement 100 %.')
    with st.container(horizontal=True):
        st.badge(f"{estimate['training_groups']} groupes historiques", icon=':material/history:', color='gray')
        st.badge(f"{estimate['personalized_count']}/{len(fixtures)} affiches avec profils des deux clubs",
                 icon=':material/sports_soccer:', color='blue')
    if not learned and estimate['personalized_count'] < len(fixtures):
        st.caption('Les affiches dont les clubs ont trop peu de précédents utilisent la tendance de la compétition.')
    if learned:
        if estimate.get('alpha') == 0:
            st.caption('La validation a retenu une force de répétition nulle pour cette compétition : '
                       'les précédents restent affichés, mais ne corrigent pas les probabilités CatBoost.')
        st.caption('La couverture indique les clubs avec au moins trois précédents dans leur rôle. '
                   'CatBoost utilise aussi les autres caractéristiques pour les clubs peu documentés. '
                   'Le protocole de validation porte sur des groupes de quatre matchs ; les autres longueurs '
                   'disposent d’un classement calculé, sans mesure de performance dédiée.')
    st.caption('Consultez les résultats de validation et de test pour mesurer les performances des scénarios complets.'
               if learned else 'Ces estimations restent expérimentales. Le test historique de cette source permet '
               'de comparer l’apport des clubs et des répétitions sur les mêmes groupes.')
    with st.expander('Comprendre les scénarios et leurs précédents'):
        st.dataframe(_match_table(fixtures), hide_index=True, width='stretch')
        st.write(f"Historique utilisé : {estimate['training_matches']} matchs, "
                 f"sur les jours strictement antérieurs au {estimate['before']} (UTC).")
        st.dataframe([
            {'Scénario': ' → '.join(row['sequence']), 'Apparitions': row['observed'],
             'Attendu selon les fréquences par position (lissé)': row['expected'],
             ('CatBoost avant correction (%)' if learned else 'Clubs seuls (%)'): 100 * row['club_probability'],
             'Répétitions seules (%)': 100 * row['frequency_probability']}
            for row in estimate['ranking']
        ], hide_index=True, width='stretch')
        st.caption('Les précédents décrivent des groupes de cette compétition. Ils ne correspondent pas '
                   'nécessairement aux mêmes clubs. Le rapport aux fréquences par position ajuste le modèle ; '
                   'il ne constitue pas un test de significativité.')
    report_key = 'workspace_evaluation'
    if learned:
        st.page_link('ui_pages/models.py', label='Consulter la validation et le test réservé', icon=':material/fact_check:')
    if not learned and st.button('Voir les résultats du test historique', icon=':material/fact_check:', key='workspace_evaluate'):
        with st.spinner('Comparaison des méthodes sur les mêmes dates…'):
            st.session_state[report_key] = {'context': payload['context'],
                                          'evaluation': _evaluate(result, payload['source'])}
    comparison = st.session_state.get(report_key)
    if not learned and comparison and comparison['context'] == payload['context']:
        evaluation = comparison['evaluation']
        if not evaluation['summary']['tested']:
            st.info('L’historique est encore trop court pour tester des groupes après la période d’apprentissage.')
        else:
            names = {'frequency': 'Répétitions seules', 'clubs': 'Clubs seuls', 'combined': 'Clubs + répétitions'}
            st.dataframe([{'Méthode': names[name], 'Groupes testés': row['tested'],
                           'Scénario exact dans les 3 choix': row['top3'],
                           'Erreur probabiliste moyenne': row['loss']}
                          for name, row in evaluation['summary']['models'].items()],
                         hide_index=True, width='stretch')
            st.caption('Une réussite exige tous les résultats dans le bon ordre. L’erreur probabiliste '
                       'est meilleure quand elle est plus petite. Seuls les jours précédents servent à apprendre.')
    st.download_button('Sauvegarder mon analyse',
                       json.dumps({'source': payload['source'], 'engine': payload.get('engine', 'statistical'),
                                   'fixtures': fixtures, 'forecast': estimate},
                                  ensure_ascii=False, indent=2),
                       file_name='patternlab_scenarios.json', mime='application/json',
                       icon=':material/download:', key='workspace_download')


def render_workspace():
    render_page_header('Préparer mes scénarios',
                       'Consultez les matchs du jour, suivez les scores et choisissez une compétition pour préparer vos scénarios.',
                       eyebrow='RÉPÉTITIONS · CLUBS · COMPÉTITIONS', icon=':material/schema:')
    try:
        source = sources.selected_source()
    except sources.ERRORS as error:
        st.error(str(error))
        st.page_link('ui_pages/settings.py', label='Ouvrir les données et réglages', icon=':material/tune:')
        return
    st.caption(f'Source active : {source}')
    with st.container(horizontal=True):
        st.page_link('ui_pages/automation.py', label='Compétitions prêtes et données manquantes',
                     icon=':material/database:')
        st.page_link('ui_pages/journal.py', label='Résultats des prévisions avant match',
                     icon=':material/monitoring:')
    auto = st.toggle('Suivre les scores automatiquement · toutes les 60 secondes', value=False, key='workspace_auto',
                     help='Une requête API par minute pendant que cette page est ouverte, uniquement pour aujourd’hui. '
                          'Les prédictions restent déclenchées par leur bouton.')
    if auto:
        _live_workspace(source)
    else:
        _manual_workspace(source)


@st.fragment(run_every=60)
def _live_workspace(source):
    _render_workspace(source, auto=True)


@st.fragment(run_every=60)
def _manual_workspace(source):
    _render_workspace(source, auto=False)


def _render_workspace(source, auto=False):
    today = _today()
    if st.session_state.get('workspace_day') != today:
        st.session_state['workspace_day'] = today
    for name in ('workspace_forecast', 'workspace_evaluation'):
        previous = st.session_state.get(name)
        if previous and previous.get('context', (None,))[0] != today.isoformat():
            st.session_state.pop(name, None)
    with st.container(border=True):
        controls = st.columns([1.2, 2.3, 1.1], vertical_alignment='bottom')
        with controls[0]:
            st.date_input('Date des matchs · aujourd’hui', min_value=today, max_value=today,
                          disabled=True, key='workspace_day', format='DD/MM/YYYY',
                          help='La journée en cours est définie par le fuseau Africa/Kinshasa.')
            day = today
        if auto and not st.session_state.get('workspace_refresh', False):
            calendar.maybe_refresh(source, day, today)
        try:
            snapshot = _day_snapshot(_snapshot(day, _signature(), source), day)
        except sources.ERRORS as error:
            st.error(str(error))
            st.page_link('ui_pages/settings.py', label='Ouvrir les données et réglages', icon=':material/tune:')
            return
        competitions = snapshot['competitions']
        options = sorted(competitions, key=lambda key: (-competitions[key]['matches'],
                         competitions[key]['country'], competitions[key]['name']))
        league_key=f'workspace_league_{source}_{day}'
        requested=st.session_state.pop('workspace_requested_league',None)
        if requested and requested.get('context') == (source,day.isoformat()) and requested.get('league') in competitions:
            st.session_state[league_key]=requested['league']
        league_options = [calendar.ALL] + options if options else []
        # A first cloud session starts empty. Streamlit preserves its None
        # selection when the first refresh subsequently supplies competitions.
        if options and st.session_state.get(league_key) not in league_options:
            st.session_state[league_key] = calendar.ALL
        elif not options and st.session_state.get(league_key) is not None:
            st.session_state[league_key] = None
        with controls[1]:
            league = st.selectbox('Compétition', league_options,
                index=0 if options else None,
                format_func=lambda key: 'Toutes les compétitions' if key == calendar.ALL else
                    f"{competitions[key]['country']} · {competitions[key]['name']} "
                    f"({competitions[key]['matches']} matchs)",
                placeholder='Actualiser pour charger les compétitions', key=league_key)
            if options and league not in league_options:
                league = calendar.ALL
        with controls[2]:
            if st.button('Actualiser', icon=':material/refresh:', width='stretch', key='workspace_refresh'):
                calendar.mark_attempt(source, day)
                try:
                    with st.spinner('Actualisation des matchs de la journée…'):
                        refreshed = sources.refresh_day(source, day)
                    st.session_state['workspace_refresh_notice'] = {
                        'source': source,
                        'message': f"{refreshed['day_count']} matchs disponibles pour cette date, toutes compétitions confondues."}
                    calendar.update_journal()
                    st.rerun()
                except sources.ERRORS as error:
                    st.error(str(error))
                    if auto:
                        st.session_state['calendar_auto_error'] = {
                            'context': (source, day.isoformat()), 'message': str(error)}
        stamp = snapshot.get('calendar_updated_at')
        try:
            when = _local_stamp(stamp).strftime('%d/%m à %H:%M:%S') if stamp else 'date inconnue, utilisez Actualiser'
        except (ValueError, TypeError, AttributeError):
            when = 'date de mise à jour indisponible'
        st.caption(f"Heures de Kinshasa · Relevé du calendrier : {when} · Source : {source}")
        st.caption('Les scores et minutes proviennent du dernier relevé API. '
                   'Les prolongations et tirs au but sont indiqués séparément des scores à 90 minutes.')
    notice = st.session_state.pop('workspace_refresh_notice', None)
    if notice and notice.get('source') == source:
        st.success(notice['message'])
    metrics = st.columns(3)
    metrics[0].metric('Matchs à cette date', snapshot['day_count'], icon=':material/calendar_today:',
                      help='Total de la journée, toutes compétitions confondues. Choisissez Toutes les compétitions pour les consulter.')
    metrics[1].metric('Compétitions disponibles', len(competitions), icon=':material/sports_soccer:')
    metrics[2].metric('Résultats dans l’historique', snapshot['usable_count'], icon=':material/history:')
    if not options:
        st.info('Aucun match enregistré localement pour cette date. Utilisez Actualiser pour interroger '
                'les compétitions accessibles à votre compte.')
        st.page_link('ui_pages/settings.py', label='Configurer la connexion aux données', icon=':material/tune:')
        return
    try:
        browser_rows=_readiness_history(_signature(),source)
        if league == calendar.ALL:
            requested=competition_browser.render(snapshot,browser_rows,day,selected_league=league)
        else:
            with st.expander('Comparer les compétitions de cette journée',icon=':material/trophy:'):
                requested=competition_browser.render(snapshot,browser_rows,day,selected_league=league)
        if requested is not None and requested in competitions and requested != league:
            st.session_state['workspace_requested_league']={'context':(source,day.isoformat()),'league':requested}
            st.rerun()
    except (sources.ERRORS + (ValueError,OSError)) as error:
        st.warning('Vue des compétitions indisponible : '+str(error))
    filtered, status, query = calendar.render_calendar(snapshot, day, league)
    if league == calendar.ALL:
        return
    displays = {row['match_id']: row for row in filtered}
    # Old/minimal snapshots can lack raw display rows; forecast fixtures stay score-free.
    raw_available = 'matches' in snapshot
    selected_day_rows = [row for row in snapshot['fixtures'] if row['league'] == league
                         and _local_stamp(row['timestamp']).date() == day
                         and (not raw_available or row['match_id'] in displays)]
    st.subheader('Les matchs de cette compétition', icon=':material/checklist:')
    st.caption(f"{len(selected_day_rows)} matchs affichés pour {competitions[league]['name']} · "
               f"{snapshot['day_count']} matchs au total dans les {len(competitions)} compétitions de la journée. "
               'Changez le filtre Compétition pour consulter les autres affiches.')
    st.caption('Sélectionnez entre 2 et 20 matchs de cette compétition. Leur ordre est fixé par l’heure de début.')
    chosen = []
    now = datetime.now(timezone.utc)
    for offset in range(0, len(selected_day_rows), 2):
        cards = st.columns(2)
        for column, row in zip(cards, selected_day_rows[offset:offset+2]):
            with column.container(border=True, height='stretch'):
                future = row['is_upcoming'] and _local_stamp(row['timestamp']).astimezone(timezone.utc) > now
                enabled = future
                with st.container(horizontal=True, horizontal_alignment='distribute'):
                    st.badge(_local_stamp(row['timestamp']).strftime('%H:%M'), icon=':material/schedule:', color='gray')
                    display = displays.get(row['match_id'])
                    st.caption((display['status_label'] + (' · ' + display['minute'] if display['minute'] else ''))
                               if display else LABELS.get(row['status'], row['status']))
                st.markdown(f"**{row['home']}** — **{row['away']}**")
                if display and display['score'] != '—':
                    st.markdown('### ' + display['score'])
                    st.caption(f"Mi-temps : {display['halftime_score']} · À 90 minutes : {display['regulation_score']}")
                selected = st.checkbox('Ajouter au groupe', disabled=not enabled,
                                       key=f"workspace_match_{source}_{day}_{league}_{row['match_id']}")
                if selected and enabled:
                    chosen.append(row)
                if not enabled:
                    st.caption('À actualiser' if row['status'] in ('TIMED', 'SCHEDULED') else
                               'Terminé · résultats disponibles dans le calendrier' if row['is_historical'] else
                               LABELS.get(row['status'], 'Match indisponible'))
    unknown = [row for row in filtered if not row['teams_confirmed']]
    if unknown:
        st.dataframe(calendar.table_rows(unknown), hide_index=True, width='stretch')
        st.caption('Ces affiches restent visibles ; leurs clubs ne sont pas confirmés pour une prédiction.')
    if unknown:
        st.caption('Certaines affiches n’ont pas encore leurs deux clubs confirmés et ne peuvent pas être sélectionnées.')
    count = len(chosen)
    report, model_error = None, None
    try:
        report = ml.load_report(source, league)
    except ml.ModelError as error:
        model_error = str(error)
    context_report, context_error = None, None
    try:
        context_report = contextual_validation.load_report(league)
    except (ValueError, OSError) as error:
        context_error = str(error)
    engine = st.selectbox('Moteur de classement', ['statistical', 'catboost', 'context_neighbors'],
                          format_func=lambda value: {'statistical': 'Clubs + répétitions',
                                                     'catboost': 'CatBoost entraîné',
                                                     'context_neighbors': 'Répétitions par contexte'}[value],
                          key=f'workspace_engine_{source}_{league}')
    if model_error:
        st.warning(model_error)
    if report and engine == 'catboost':
        st.caption(f"Modèle de cette compétition appris jusqu’au {report['deployment']['trained_through']} (UTC). "
                   'Il est utilisable pour des groupes dont tous les matchs suivent ce jour.')
    if not report:
        if engine == 'catboost':
            st.info('CatBoost est indisponible pour cette compétition. Entraînez le modèle ou choisissez '
                    'explicitement le moteur Clubs + répétitions.')
        st.page_link('ui_pages/models.py', label='Entraîner CatBoost et consulter ses résultats', icon=':material/model_training:')
    if engine == 'context_neighbors':
        if context_error:
            st.warning(context_error)
        elif not context_report:
            st.info('Les réglages de contexte sont indisponibles pour cette compétition. '
                    'Lancez leur évaluation dans Modèles et validation.')
        else:
            configuration = context_report['deployment']
            st.caption('Réglages choisis sur validation, avec les résultats jusqu’au '
                       f'{configuration["selection_through"]} inclus (UTC). '
                       'Les voisins seront recherchés uniquement avant le premier jour de votre groupe.')
        st.page_link('ui_pages/models.py', label='Consulter ou évaluer les répétitions par contexte',
                     icon=':material/manage_search:')
    model_generation = (report['deployment']['generation'] if report and engine == 'catboost' else
                        context_report['deployment']['generation'] if context_report and engine == 'context_neighbors' else None)
    with st.container(border=True):
        st.markdown(f'**Votre groupe · {count} match' + ('s**' if count != 1 else '**'))
        if count:
            st.dataframe(_match_table(chosen), hide_index=True, width='stretch')
        else:
            st.caption('Ajoutez les matchs à analyser depuis les cartes ci-dessus.')
        if count > 20:
            st.warning('Le groupe est limité à 20 matchs. Retirez des matchs pour continuer.')
        elif count < 2:
            st.caption(f'Encore {2-count} match(s) à sélectionner pour lancer le classement.')
        if 2 <= count <= 20 and engine == 'statistical':
            _show_group_readiness(chosen, source, league)
        action = st.button('Classer mes scénarios', icon=':material/schema:', type='primary',
                           width='stretch', disabled=not 2 <= count <= 20 or engine == 'catboost' and not report
                           or engine == 'context_neighbors' and not context_report,
                           key='workspace_predict')
        st.caption('L’historique de la compétition est complété au premier calcul si nécessaire. '
                   'Cette préparation utilise jusqu’à deux requêtes ; les calculs suivants restent locaux.')
        st.caption('Pour les matchs du jour encore à venir, la première prévision est enregistrée automatiquement. '
                   'Une requête API vérifie les affiches juste avant cet enregistrement. '
                   'Les rencontres commencées ne peuvent pas être sélectionnées.')
    context = (day.isoformat(), league, tuple(row['match_id'] for row in chosen), _signature(),
               engine, model_generation, status, query, source)
    if action:
        st.session_state.pop('workspace_forecast', None)
        try:
            _require_today(day, chosen)
            rows, audit = _history(_signature(), source)
            _require_today(day, chosen)
            info = competitions[league]
            current = info.get('current_season')
            missing = sources.missing_history(source, info['code'], [current - 1, current]) \
                if current and info.get('code') else []
            if missing and info.get('code'):
                _require_today(day, chosen)
                with st.spinner('Préparation des précédents des clubs et des scénarios de la compétition…'):
                    sources.ensure_history(source, info['code'], missing)
                rows, audit = _history(_signature(), source)
            result = _history_result(rows, league, count)
            fixtures = [{key: row[key] for key in ('match_id', 'league', 'home', 'away', 'timestamp', 'provider',
                                                  'home_id', 'away_id', 'season') if key in row}
                        for row in chosen]
            _require_today(day, fixtures)
            with st.spinner('Classement des scénarios de votre groupe…'):
                if engine == 'catboost':
                    estimate = _ml_estimate(rows, fixtures, source, model_generation)
                elif engine == 'context_neighbors':
                    estimate = _context_estimate(rows, fixtures, source, model_generation)
                else:
                    estimate = _estimate(result, fixtures, source)
            _require_today(day, fixtures)
            registered = None
            if estimate['eligible']:
                try:
                    with st.spinner('Vérification avant match et enregistrement des méthodes comparables…'):
                        registered = tracking.register(rows, fixtures, estimate, engine, competition=info)
                except (sources.ERRORS + (journal.JournalError, ValueError, OSError)) as error:
                    registered = dict(state='failed', message=str(error))
            _require_today(day, fixtures)
            context = (day.isoformat(), league, tuple(row['match_id'] for row in chosen), _signature(),
                       engine, model_generation, status, query, source)
            st.session_state['workspace_forecast'] = {
                'context': context, 'source': source, 'engine': engine,
                'forecast': estimate, 'fixtures': fixtures, 'result': result, 'journal': registered}
        except (sources.ERRORS + (ml.ModelError, ValueError)) as error:
            st.error(str(error))
            if engine == 'context_neighbors':
                st.caption('La sélection est conservée. Vérifiez les dates, les données et la compatibilité de la campagne figée.')
            else:
                st.caption('La sélection est conservée. Pour CatBoost, vérifiez la date et la compatibilité de la campagne figée. '
                           'Le classement concerne uniquement les matchs du jour encore à venir.')
    previous = st.session_state.get('workspace_forecast')
    if previous and day == _today() and previous['context'] == context:
        _show_results(previous)
    elif previous:
        st.caption('La sélection ou les données ont changé. Relancez le classement pour obtenir les scénarios de ce groupe.')
