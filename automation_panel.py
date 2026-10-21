"""Explicit controls and local coverage for the API-Football worker."""
from datetime import datetime, timezone
import json
from zoneinfo import ZoneInfo

import streamlit as st

import api_football as api
import automation_worker as worker
from scripts import automation_service as service
from ux_theme import render_page_header


READINESS = {'ready': 'Prête pour Clubs + répétitions', 'partial': 'Prête pour certaines tailles',
             'insufficient': 'Historique statistique à compléter', 'no_upcoming': 'Sans groupe à venir'}
STATES = {'completed': 'Terminé', 'paused': 'En pause', 'busy': 'Cycle déjà en cours',
          'disabled': 'Désactivé', 'dry_run': 'Simulation locale', 'error': 'Erreur',
          'pending': 'En attente', 'recorded': 'Enregistré', 'insufficient': 'Historique ou modèles insuffisants',
          'ineligible': 'Groupe non admissible', 'changed': 'Affiches modifiées'}
METHODS = {'frequency': 'Répétitions seules', 'clubs': 'Clubs seuls', 'combined': 'Clubs + répétitions',
           'catboost': 'CatBoost', 'context_neighbors': 'Répétitions par contexte'}


@st.cache_data(ttl=30, max_entries=4, show_spinner=False)
def _coverage(signature, configuration, day):
    # Cache the complete local view; country/search filters remain inexpensive.
    return worker.coverage(config=json.loads(configuration))


def _scheduler_status():
    try:
        state = service.service_status()
    except (OSError, ValueError) as error:
        st.warning(f'État du planificateur indisponible : {error}')
        return
    if state.get('running'):
        st.success('Planificateur macOS chargé.')
    elif state.get('installed'):
        st.warning('Le planificateur est installé, mais il n’est pas chargé dans la session macOS.')
    else:
        st.info('Le planificateur macOS n’est pas installé. Les cycles manuels restent disponibles.')
    if state.get('process_running'):
        st.caption('Un processus du planificateur est actuellement en cours.')
    if state.get('enabled') is False:
        st.info('Les cycles sont désactivés dans les réglages ; aucun appel API automatique ne sera effectué.')
    interval = state.get('interval_seconds')
    if isinstance(interval, (int, float)) and interval > 0:
        st.caption(f'Réveil du planificateur : toutes les {interval/60:g} minutes. '
                   'Le statut « chargé » reste actif entre les cycles.')
    if state.get('error'):
        st.warning(str(state['error']))
    if state.get('needs_reinstall'):
        st.warning('Les réglages du planificateur ont changé. Réinstallez l’agent pour appliquer le nouvel intervalle.')
        st.code('.venv/bin/python scripts/automation_service.py --install', language='bash')


def _configuration(config):
    with st.form('automation_config', border=True):
        enabled = st.toggle('Autoriser les cycles automatiques', value=bool(config['enabled']), key='automation_enabled')
        models_enabled = st.toggle('Préparer et figer les modèles des compétitions prêtes',
                                    value=config.get('models_enabled', True), key='automation_models_enabled')
        sizes = st.multiselect('Matchs par groupe', list(range(2, 21)), default=config['sizes'], key='automation_sizes')
        with st.container(horizontal=True):
            interval = st.number_input('Intervalle entre les cycles (minutes)', min_value=1, max_value=1440,
                                       value=int(config['interval_minutes']), step=1, key='automation_interval')
            daily = st.number_input('Budget API par jour', min_value=1, max_value=100000,
                                    value=int(config['daily_request_budget']), step=10, key='automation_daily_budget')
            cycle = st.number_input('Budget API par cycle', min_value=1, max_value=1000,
                                    value=int(config['cycle_request_budget']), step=1, key='automation_cycle_budget')
        years = sorted(set(range(1900, datetime.now(timezone.utc).year+1)) | set(config['history_seasons']))
        seasons = st.multiselect('Saisons historiques à préparer', years,
                                 default=config['history_seasons'], key='automation_seasons')
        history_requests = st.number_input('Imports historiques maximum par cycle', min_value=0, max_value=2,
                                           value=int(config['history_requests_per_cycle']), step=1,
                                           key='automation_history_requests')
        st.caption('Les imports alternent entre les compétitions qui jouent dans les sept prochains jours et la file générale. '
                   'La préparation locale des modèles traite au maximum une compétition par passage du service. '
                   'Ses paramètres restent ensuite figés pour le suivi avant match.')
        saved = st.form_submit_button('Enregistrer les réglages', icon=':material/save:', key='automation_save')
    if saved:
        candidate = dict(config, enabled=enabled, models_enabled=models_enabled, sizes=sizes, interval_minutes=int(interval),
                         daily_request_budget=int(daily), cycle_request_budget=int(cycle),
                         history_seasons=seasons, history_requests_per_cycle=int(history_requests))
        try:
            config = worker.save_config(candidate)
            _coverage.clear()
            st.success('Réglages enregistrés. Ils seront utilisés au prochain cycle.')
        except (worker.AutomationError, OSError, ValueError) as error:
            st.error(str(error))
    st.caption('Ces réglages ne déclenchent pas de cycle et n’installent pas le planificateur macOS.')
    return config


def _show_report(report):
    st.subheader('Dernier cycle', icon=':material/history:')
    if not report:
        st.info('Aucun cycle enregistré pour le moment.')
        return
    st.caption(f"État : {STATES.get(report.get('status'), report.get('status', 'Inconnu'))} · "
               f"Début : {report.get('started_at', '—')} · Fin : {report.get('finished_at') or '—'}")
    with st.container(horizontal=True):
        st.metric('Requêtes API', report.get('requests', 0))
        st.metric('Nouveaux groupes verrouillés', report.get('created', 0))
        st.metric('Groupes déjà enregistrés', report.get('already_recorded', 0))
        st.metric('Imports historiques', report.get('history_imports', 0))
    for issue in report.get('issues', []):
        st.warning(str(issue))
    all_jobs = report.get('jobs') or []
    today = datetime.now(ZoneInfo('Africa/Kinshasa')).date().isoformat()
    jobs = [job for job in all_jobs if job.get('day') == today]
    excluded = len(all_jobs)-len(jobs)
    st.caption(f'Groupes affichés : journée du {today} à Kinshasa. '
               f'{excluded} groupe(s) d’autres jours ou sans date restent dans le rapport complet et sont exclus de ce tableau.')
    if not jobs:
        st.info('Aucun groupe du jour dans ce dernier bilan. Le rapport complet est conservé ci-dessous.')
    if jobs:
        waiting = sum(job.get('state') == 'insufficient' for job in jobs)
        if waiting:
            st.info(f'{waiting} groupe(s) à venir attendent un historique ou un modèle utilisable. '
                    'Leur absence du journal est indiquée dans le bilan ; les imports continuent aux prochains cycles.')
        st.dataframe([{'Compétition': job.get('competition', job.get('league', '')),
                       'ID compétition': job.get('league', ''), 'Matchs par groupe': job.get('size'),
                       'Jour': job.get('day', ''), 'État': STATES.get(job.get('state'), job.get('state', '')),
                       'Motif': job.get('reason', ''),
                       'Méthodes figées': ', '.join(METHODS.get(name, name) for name in (job.get('methods') or [])),
                       'Méthodes indisponibles': ' ; '.join(f'{METHODS.get(name, name)} : {reason}'
                                                           for name, reason in (job.get('unavailable') or {}).items())}
                      for job in jobs], hide_index=True, width='stretch', key='automation_jobs')
    with st.expander('Rapport détaillé du cycle'):
        st.json(report, expanded=False)


def _show_coverage(rows):
    st.subheader('Les compétitions du jour', icon=':material/public:')
    st.caption('Cette vue couvre les compétitions ayant des matchs aujourd’hui à Kinshasa. '
               'La préparation dépend de la taille du groupe et des résultats disponibles avant son premier jour UTC.')
    if not rows:
        st.info('Aucune compétition enregistrée pour aujourd’hui à Kinshasa. Actualisez le calendrier du jour dans Analyser des matchs.')
        return
    with st.container(horizontal=True):
        st.metric('Prêtes pour Clubs + répétitions', sum(r.get('readiness') == 'ready' for r in rows))
        st.metric('Prêtes pour certaines tailles', sum(r.get('readiness') == 'partial' for r in rows))
        st.metric('Historique statistique à compléter', sum(r.get('readiness') == 'insufficient' for r in rows))
        st.metric('Sans groupe à venir', sum(r.get('readiness') == 'no_upcoming' for r in rows))
    st.caption(f"{len(rows)} compétitions du jour · "
               f"{sum(int(r.get('history_matches') or 0) for r in rows)} résultats historiques utilisables · "
               f"{sum(int(r.get('ready_groups') or 0) for r in rows)} groupes prêts · "
               f"{sum(int(r.get('pending_groups') or 0) for r in rows)} groupes en attente d’historique.")
    readiness = st.segmented_control('Préparation des compétitions', ['all']+list(READINESS),
        format_func=lambda value: {'all': 'Toutes', 'ready': 'Prêtes', 'partial': 'Certaines tailles',
                                  'insufficient': 'Statistique à compléter', 'no_upcoming': 'Sans groupe à venir'}[value],
        default='all', required=True, key='automation_readiness')
    countries = sorted({str(row.get('country') or 'Pays non précisé') for row in rows})
    with st.container(horizontal=True):
        country = st.selectbox('Pays', ['Tous']+countries, key='automation_country')
        query = st.text_input('Rechercher une compétition', placeholder='Nom, pays ou identifiant', key='automation_query')
    needle = query.strip().casefold()
    visible = [row for row in rows if (readiness == 'all' or row.get('readiness') == readiness)
               and (country == 'Tous' or str(row.get('country') or 'Pays non précisé') == country)
               and (not needle or needle in ' '.join(str(row.get(k, '')) for k in ('league','competition','country')).casefold())]
    order = {'ready':0, 'partial':1, 'insufficient':2, 'no_upcoming':3}
    visible.sort(key=lambda row: (order.get(row.get('readiness'),4), -int(row.get('ready_groups') or 0),
                                  -int(row.get('eligible_groups') or 0), str(row.get('country','')),
                                  str(row.get('competition','')), str(row.get('league',''))))
    st.dataframe([{'ID compétition': str(r.get('league', '')), 'Pays': r.get('country', ''),
                   'Compétition': r.get('competition', ''),
                   'Préparation': r.get('readiness_label') or READINESS.get(r.get('readiness'), 'À recalculer'),
                   'Résultats utilisables': r.get('history_matches', 0),
                   'Groupes prêts': r.get('ready_groups', 0), 'Groupes en attente': r.get('pending_groups', 0),
                   'Tailles prêtes': ', '.join(str(size) for size in r.get('ready_sizes', [])),
                   'Matchs manquants au minimum': r.get('missing_matches'), 'Motif': r.get('reason', '')}
                  for r in visible], hide_index=True, width='stretch', key='automation_coverage')
    st.caption(f'{len(visible)} compétition(s) affichée(s) sur {len(rows)}. Ces filtres modifient uniquement cette vue.')
    st.caption('Ces seuils concernent le moteur Clubs + répétitions. CatBoost et contexte sont vérifiés séparément '
               'au calcul. La préparation des données ne mesure pas la précision des prévisions.')
    st.page_link('ui_pages/journal.py', label='Voir les performances réellement observées dans le journal',
                 icon=':material/history_edu:')
    if not visible:
        st.info('Aucune compétition ne correspond à ces filtres.')
        return
    with st.expander('Données manquantes par taille de groupe', icon=':material/checklist:'):
        indexed = {str(row['league']): row for row in visible}
        code = st.selectbox('Compétition à détailler', list(indexed),
            format_func=lambda value: f"{indexed[value].get('country', '')} · {indexed[value].get('competition', value)}",
            key='automation_readiness_league')
        selected = indexed[code]
        st.caption(f"{selected.get('fixtures', 0)} matchs du jour · {selected.get('issues', 0)} scores à vérifier. "
                   'Le minimum du tableau principal concerne une taille configurée ; les besoins de chaque taille figurent ci-dessous.')
        details = selected.get('per_size') or []
        if not details:
            st.info('Le détail par taille n’est pas disponible dans cet état local.')
        else:
            st.dataframe([{'Matchs par groupe': item['size'], 'Résultats avant le (UTC)': item.get('before', ''),
                           'Résultats antérieurs': item.get('history_matches', 0),
                           'Groupes historiques': item.get('history_groups', 0), 'Groupes requis': item.get('min_groups', 20),
                           'Groupes manquants': item.get('missing_groups', 0),
                           'Matchs supplémentaires requis': item.get('missing_matches', 0),
                           'Clubs + répétitions calculable': bool(item.get('eligible')),
                           'Groupes à venir': item.get('eligible_groups', 0), 'Groupes prêts': item.get('ready_groups', 0),
                           'Groupes en attente': item.get('pending_groups', 0), 'Motif': item.get('reason', '')}
                          for item in details], hide_index=True, width='stretch', key='automation_readiness_detail')
            st.caption('Seuils : au moins 20 groupes historiques de la taille choisie et 30 matchs de la compétition '
                       'sur trois jours antérieurs. Les résultats du jour des affiches ne sont pas utilisés pour ces calculs.')


def render_automation():
    render_page_header('Automatisation',
                       'Préparer les historiques, verrouiller les prévisions avant match et rapprocher les résultats.',
                       eyebrow='TOUTES LES COMPÉTITIONS LOCALES · API-FOOTBALL', icon=':material/schedule:')
    st.caption('Le calendrier et les nouvelles prévisions concernent uniquement les matchs du jour à Kinshasa. '
               'Les historiques et le suivi des résultats déjà enregistrés sont conservés. '
               'Chaque première prévision reste verrouillée dans le journal.')
    _scheduler_status()
    try:
        config = worker.load_config()
        with st.expander('Réglages des cycles', icon=':material/tune:'):
            config = _configuration(config)
    except (worker.AutomationError, OSError, ValueError) as error:
        st.error(str(error))
        return
    with st.container(horizontal=True):
        run = st.button('Lancer un cycle maintenant', type='primary', icon=':material/play_arrow:',
                         key='automation_run', disabled=not config['enabled'])
        refresh = st.button('Relire l’état local', icon=':material/refresh:', key='automation_refresh')
    st.caption('Lancer un cycle peut consommer des requêtes dans les budgets ci-dessus. '
               'L’ouverture de cette page et « Relire l’état local » utilisent seulement les fichiers locaux.')
    report = None
    if refresh:
        _coverage.clear()
    if run:
        try:
            with st.spinner('Préparation des données et enregistrement des groupes avant match…'):
                report = worker.run_cycle(config=config)
            _coverage.clear()
            if report.get('status') == 'error':
                st.error('Le cycle a rencontré une erreur. Consultez son bilan ci-dessous.')
            elif report.get('status') in ('paused', 'busy', 'disabled') or report.get('issues'):
                st.warning('Le cycle est suspendu ou comporte des points à vérifier. Consultez son bilan ci-dessous.')
            else:
                st.success('Cycle terminé. Consultez son bilan ci-dessous.')
        except (worker.AutomationError, api.ApiFootballError, OSError, ValueError) as error:
            st.error(str(error))
    try:
        if report is None:
            report = worker.load_status()
        today = datetime.now(ZoneInfo('Africa/Kinshasa')).date().isoformat()
        coverage = _coverage(api.data_signature(api.STORE_PATH), json.dumps(config, sort_keys=True), today)
    except (worker.AutomationError, api.ApiFootballError, OSError, ValueError) as error:
        st.error(str(error))
        return
    _show_coverage(coverage)
    _show_report(report)
