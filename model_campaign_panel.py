"""Local-only view of the first, immutable validation campaign per league."""
import json

import streamlit as st

import api_football as api
import model_campaign as campaign


STATES = {'ready': 'Prête à valider', 'insufficient': 'Historique insuffisant',
          'frozen': 'Paramètres figés', 'building': 'Campagne commencée', 'error': 'Campagne interrompue',
          'completed': 'Terminé', 'paused': 'En pause', 'disabled': 'Désactivé', 'busy': 'Déjà en cours',
          'idle': 'Aucune compétition à préparer'}
COMPONENTS = {'catboost': 'CatBoost', 'context_neighbors': 'Répétitions par contexte'}
ORIGINS = {'adopted_existing_exploratory': 'Artefact exploratoire existant conservé',
           'trained_new_on_frozen_input': 'Appris sur le premier historique figé',
           'validated_new_on_frozen_input': 'Réglages choisis sur le premier historique figé',
           'recovered_frozen_artifact': 'Artefact figé retrouvé après interruption'}


@st.cache_data(ttl=30, max_entries=4, show_spinner=False)
def _plan(rows, raw, sizes, signature):
    return campaign.plan(rows, raw=raw, sizes=sizes)


def _signature():
    values = []
    for path in (api.DATA_DIR/'model_campaigns').glob('**/*.json'):
        try:
            stat = path.stat()
            values.append((str(path), stat.st_mtime_ns, stat.st_size))
        except OSError:
            continue
    return (str(api.DATA_DIR), api.data_signature(api.STORE_PATH), tuple(sorted(values)))


def _different(job):
    return bool(job.get('frozen_fingerprint') and job.get('fingerprint') != job['frozen_fingerprint'])


def _history_label(job):
    labels = {'unchanged': 'Historique du gel inchangé', 'not_frozen': 'Pas encore figé',
              'new_results': 'Nouveaux résultats après le gel',
              'changed_history': 'Historique initial corrigé ou complété'}
    return labels.get(job.get('history_state'), 'Historique actualisé depuis le gel' if _different(job) else 'Inchangé')


def _scheduled_status():
    path = api.DATA_DIR/'model_campaign_status.json'
    if not path.exists():
        st.caption('Aucun cycle de modèles enregistré par le planificateur pour le moment.')
        return
    try:
        report = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(report, dict):
            raise ValueError()
    except (ValueError, OSError):
        st.warning('L’état local du dernier cycle de modèles est illisible.')
        return
    with st.expander('Dernier passage du planificateur de modèles', icon=':material/schedule:'):
        st.caption(f"État : {STATES.get(report.get('status'), report.get('status', 'Inconnu'))} · "
                   f"Début : {report.get('started_at', '—')} · Fin : {report.get('finished_at') or '—'}")
        for issue in report.get('issues', []):
            st.warning(str(issue))
        st.json(report, expanded=False)


def _manifest(report):
    st.subheader('Première campagne conservée', icon=':material/lock:')
    st.caption(f"État : {STATES.get(report.get('status'), report.get('status', 'Inconnu'))} · "
               f"Historique figé le {report.get('frozen_at', '—')} · "
               f"Résultats strictement antérieurs au {report.get('cutoff', '—')} (UTC).")
    components = report.get('components') or {}
    if components:
        st.dataframe([{'Méthode': COMPONENTS.get(name, name),
                       'Disponibilité': 'Disponible' if component.get('status') == 'available' else 'Indisponible',
                       'Origine': ORIGINS.get(component.get('origin'), component.get('origin', '')),
                       'Génération': component.get('generation', ''), 'Motif': component.get('reason', '')}
                      for name, component in components.items()], hide_index=True, width='stretch', key='campaign_components')
    if report.get('error'):
        st.warning(report['error'])
    comparison = report.get('comparisons') or {}
    if comparison.get('status') == 'partial':
        st.info('Comparaison partielle : certaines méthodes ou tailles manquent d’historique. '
                'Le détail reste conservé dans le rapport de cette première campagne.')
    with st.expander('Réglages figés, protocole et rapports'):
        st.json(report, expanded=False)
    st.download_button('Télécharger le manifeste de campagne', json.dumps(report, ensure_ascii=False, indent=2),
                       file_name=f"patternlab_campagne_{report['league']}.json", mime='application/json',
                       icon=':material/download:', key='campaign_download')


def render(rows, source='API-Football'):
    st.subheader('Une campagne figée par compétition', icon=':material/lock_clock:')
    st.markdown('Les compétitions disposant d’un historique suffisant peuvent être entraînées et validées une fois. '
                'Le premier historique, les réglages choisis et les rapports sont conservés pour les prochains matchs.')
    st.caption('L’ouverture de cette vue lit uniquement les données locales. Aucun entraînement, appel API '
               'ou ajout au journal n’est déclenché. Le lancement manuel traite une seule compétition.')
    st.caption('Le planificateur prépare au plus une compétition admissible par cycle de modèles. '
               'Les compétitions déjà figées sont conservées ; les historiques insuffisants attendent les prochains imports.')
    _scheduled_status()
    if source != 'API-Football':
        st.error('Seule la source API-Football est autorisée.')
        return
    sizes = st.multiselect('Tailles de groupes de la campagne', list(range(2,21)),
                           default=list(campaign.DEFAULT_SIZES), key='campaign_sizes')
    if not sizes:
        st.info('Choisissez au moins une taille de groupe.')
        return
    try:
        jobs = _plan(rows, api.load_fixtures(api.STORE_PATH), tuple(sizes), _signature())
    except (campaign.CampaignError, api.ApiFootballError, OSError, ValueError) as error:
        st.error(str(error))
        return
    if not jobs:
        st.info('Aucune compétition locale disponible. Les prochaines importations alimenteront cette liste.')
        st.page_link('ui_pages/automation.py', label='Voir la préparation des historiques', icon=':material/schedule:')
        return
    with st.container(horizontal=True):
        st.metric('Compétitions locales', len(jobs))
        st.metric('Prêtes à valider', sum(job['status'] == 'ready' for job in jobs))
        st.metric('Paramètres figés', sum(job['status'] == 'frozen' for job in jobs))
        st.metric('Historique insuffisant', sum(job['status'] == 'insufficient' for job in jobs))
    with st.container(horizontal=True):
        state = st.selectbox('État de campagne', ['all','ready','frozen','changed','insufficient'],
                            format_func=lambda value: 'Toutes' if value == 'all' else 'Historique actualisé depuis le gel' if value == 'changed'
                            else STATES[value], key='campaign_state')
        query = st.text_input('Rechercher une compétition', placeholder='Nom ou identifiant', key='campaign_query')
    needle = query.strip().casefold()
    visible = [job for job in jobs if (state == 'all' or state == 'changed' and _different(job) or job['status'] == state)
               and (not needle or needle in f"{job['league']} {job['competition']}".casefold())]
    st.dataframe([{'ID compétition': job['league'], 'Compétition': job['competition'],
                   'Campagne': STATES.get(job['status'], job['status']), 'Résultats utilisables': job['history_matches'],
                   'Prochain match UTC': job.get('upcoming_at') or '—',
                   'Validation CatBoost possible': bool(job.get('chronological_ready')),
                   'Comparaison par contexte possible': bool(job.get('contextual_ready')),
                   'Historique depuis le gel': _history_label(job),
                   'Contexte figé indisponible': bool(job.get('context_frozen_unavailable')), 'Motif': job.get('reason', '')}
                  for job in visible], hide_index=True, width='stretch', key='campaign_plan')
    st.caption('Les prochaines rencontres déterminent la priorité de la liste. Un historique actuel différent '
               'peut contenir de nouveaux matchs ou des corrections ; il ne déclenche aucune réoptimisation des paramètres figés.')
    with st.expander('Comment les paramètres sont choisis'):
        st.markdown('**CatBoost** : séparation chronologique d’environ 60 % pour apprendre, 20 % pour choisir '
                    'les réglages et 20 % pour mesurer le test réservé. Les jours UTC restent entiers.')
        st.markdown('**Répétitions par contexte** : les réglages de chaque taille sont choisis sur les saisons '
                    'de validation, puis mesurés sur les saisons suivantes selon le protocole existant.')
        st.caption('Les profils de chaque groupe utilisent uniquement les résultats antérieurs à son premier jour UTC. '
                   'Les résultats du test ne choisissent pas les réglages. Un artefact existant compatible peut être conservé sans nouvel entraînement.')
        st.info('Cet historique a déjà été disponible ou examiné : recalculer un rapport ne crée pas un nouveau test '
                'historique indépendant. La confirmation réelle repose sur les prévisions enregistrées avant les matchs.')
    st.page_link('ui_pages/journal.py', label='Mesurer les performances des prévisions avant match', icon=':material/history_edu:')
    if not visible:
        st.info('Aucune compétition ne correspond à ces filtres.')
        return
    indexed = {job['league']:job for job in visible}
    league = st.selectbox('Compétition à préparer', list(indexed),
                          format_func=lambda value:indexed[value]['competition'], key='campaign_league')
    job = indexed[league]
    if job.get('history_state') == 'new_results':
        st.info('De nouveaux résultats sont arrivés après le gel. Les paramètres sont conservés ; '
                'les prévisions suivantes seront mesurées dans le journal sans nouvelle optimisation.')
    elif _different(job):
        st.warning('Historique actualisé depuis le gel ; paramètres conservés. La campagne et les prévisions déjà '
                   'enregistrées restent inchangées ; la compatibilité de chaque moteur sera contrôlée au calcul.')
    if job.get('context_frozen_unavailable'):
        st.info('Le premier historique ne permettait pas de valider les contextes. Cette indisponibilité reste figée '
                'dans la campagne ; l’arrivée de nouveaux résultats ne déclenche pas une nouvelle recherche de réglages.')
    try:
        report = campaign.load_manifest(league)
    except (campaign.CampaignError, OSError, ValueError) as error:
        st.error(str(error))
        return
    if job['status'] == 'insufficient':
        st.info(job.get('reason') or 'Historique insuffisant pour la séparation chronologique.')
        st.page_link('ui_pages/automation.py', label='Voir les historiques à compléter', icon=':material/schedule:')
    resumable = report and report.get('status') in ('building','error')
    action = st.button('Reprendre cette campagne figée' if resumable else 'Entraîner et figer cette compétition',
                       type='primary', icon=':material/model_training:', key='campaign_run',
                       disabled=job['status'] != 'ready' and not resumable)
    st.caption('Une compétition par lancement. Les paramètres figés ne sont pas réajustés aux résultats suivants. '
               'Le journal conserve toutes les prévisions originales.')
    if action:
        try:
            with st.spinner('Entraînement et validation sur le premier historique figé…'):
                report = campaign.run_one(rows, league, sizes=tuple(sizes))
            _plan.clear()
            if report['status'] == 'frozen':
                st.success('La campagne de cette compétition est figée. Aucun autre entraînement n’a été lancé.')
            else:
                st.info(report.get('reason') or 'La campagne ne peut pas être terminée avec cet historique.')
        except (campaign.CampaignError, OSError, ValueError) as error:
            st.error(str(error))
    if report:
        _manifest(report)
