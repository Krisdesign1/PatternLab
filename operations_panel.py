"""Small read-only operating views; never start a service or restore data."""
import table_views
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st

import operations_status


ZONE = ZoneInfo('Africa/Kinshasa')


def local_time(value):
    if not value:
        return 'Non disponible'
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            return 'Non disponible'
        return stamp.astimezone(ZONE).strftime('%d/%m/%Y à %H h %M')
    except (ValueError, TypeError, OverflowError):
        return 'Non disponible'


def _gigabytes(value):
    return f'{value / 1024**3:.1f} Go' if type(value) is int and value >= 0 else 'Non disponible'


@st.cache_data(ttl=10, max_entries=2, show_spinner=False)
def linux_scheduler():
    return operations_status.scheduler_status()


def render_scheduler(state):
    with st.container(border=True):
        st.subheader('Automatisation du serveur', icon=':material/schedule:')
        if not state.get('available'):
            st.warning('État du service indisponible. Cela ne confirme ni un arrêt ni un fonctionnement normal.')
            st.caption('Le journal des passages ci-dessous reste consultable. Aucune commande de démarrage n’est exécutée ici.')
            return
        if state.get('running') and state.get('enabled'):
            st.success('Planification active · redémarrage automatique configuré.')
        elif state.get('running'):
            st.warning('Planification active, mais sa reprise au redémarrage n’est pas confirmée.')
        else:
            st.warning('La planification automatique du serveur est arrêtée ou désactivée.')
        timer, worker = state.get('timer') or {}, state.get('worker') or {}
        with st.container(horizontal=True):
            st.metric('Cycle de données', 'En cours' if state.get('process_running') else 'Au repos', border=True)
            st.metric('Dernier démarrage', local_time(worker.get('started_at')), border=True)
        if worker.get('result') == 'oom-kill':
            st.warning('Le dernier cycle a été arrêté par manque de mémoire. '
                       'La planification reste distincte de la réussite de ce cycle.')
        elif worker.get('result') not in (None, '', 'success'):
            st.warning('Le dernier passage du service comporte une erreur ou une interruption. Consultez son bilan ci-dessous.')
        interval = state.get('interval_seconds')
        if type(interval) in (int, float) and interval > 0:
            st.caption(f'Le prochain cycle attend {interval / 60:g} minutes après la fin du précédent. '
                       'Un cycle long reste unique ; aucun second passage du timer ne le chevauche.')
        if timer.get('next_elapse_at'):
            st.caption('Prochain déclenchement prévu : ' + local_time(timer['next_elapse_at']) + ' · Kinshasa.')
        st.caption('Le serveur fonctionne indépendamment du Mac. Entraînement désactivé pour conserver les modèles validés. '
                   'Les imports, résultats et nouvelles prévisions continuent selon les règles existantes.')
        st.caption('État du service lu le ' + local_time(state.get('observed_at')) + ' · Kinshasa.')


REASONS = {
    'insufficient_space': 'Espace libre insuffisant : les sauvegardes précédentes sont conservées.',
    'worker_busy': 'Cycle de données en cours : la sauvegarde est différée.',
    'busy': 'Une autre sauvegarde est en cours.',
    'backup_busy': 'Une autre sauvegarde est en cours.',
    'verification_failed': 'Le contrôle de la nouvelle sauvegarde a échoué ; le dernier point vérifié est conservé.',
    'source_changed': 'Un fichier a changé pendant la copie : une nouvelle tentative est nécessaire.',
    'snapshot_changed': 'Un fichier a changé pendant la copie : une nouvelle tentative est nécessaire.',
}


@st.fragment(run_every=30)
def render_backup_status():
    view = operations_status.backup_status()
    with st.container(border=True):
        st.subheader('Sauvegarde complète des données', icon=':material/backup:')
        if not view.get('available'):
            if view.get('state') == 'invalid':
                st.warning('Le relevé de sauvegarde ne peut pas être validé. Aucun succès n’est supposé.')
            else:
                st.info('Aucun état de sauvegarde complète vérifié n’est disponible pour cette installation.')
            st.caption('Le téléchargement des matchs API-Football ci-dessous est un export de matchs ; '
                       'il ne contient pas le journal complet, les modèles et les autres bases de l’application.')
            return
        point = view.get('last_backup') or {}
        if point:
            st.success('Dernière sauvegarde contrôlée avec restauration des bases dans un espace isolé.')
            with st.container(horizontal=True):
                st.metric('Dernier contrôle', local_time(point.get('verified_at')), border=True)
                st.metric('Bases restaurées pour contrôle', str(point.get('databases_restored', '—')), border=True)
                st.metric('Volume des données sauvegardées', _gigabytes(point.get('payload_bytes')), border=True)
            st.caption(f"{point.get('files_verified', '—')} fichiers vérifiés. "
                       'Ce contrôle ne remplace pas les données actives de l’application.')
        else:
            st.warning('Aucune sauvegarde complète réussie n’est encore confirmée par ce suivi.')
        scheduler = view.get('scheduler') or {}
        if scheduler.get('available') and scheduler.get('enabled') is True and scheduler.get('running') is True:
            st.caption('Sauvegarde quotidienne programmée. Les tentatives et leurs résultats sont suivis séparément.')
            if (scheduler.get('timer') or {}).get('next_elapse_at'):
                st.caption('Prochaine tentative : ' + local_time(scheduler['timer']['next_elapse_at']) + ' · Kinshasa.')
        else:
            st.warning('La programmation quotidienne des sauvegardes n’est pas confirmée active.')
        attempt = view.get('last_attempt') or {}
        state = attempt.get('state') or view.get('state')
        if state in ('error', 'failed', 'deferred', 'skipped'):
            st.warning(REASONS.get(attempt.get('reason_code'),
                       'La dernière tentative n’a pas produit de nouveau point vérifié. Le point précédent reste conservé.'))
        if state == 'running':
            st.info('Une nouvelle sauvegarde est en cours de préparation ou de contrôle. Son succès reste à confirmer.')
        if view.get('observation_stale'):
            st.warning('Ce relevé est ancien : il ne confirme pas le fonctionnement actuel des sauvegardes.')
        if view.get('backup_overdue'):
            st.warning('La dernière sauvegarde vérifiée dépasse le délai attendu. Les tentatives récentes doivent être examinées.')
        table = [
            {'Contrôle': 'Données incluses', 'État': 'Bases locales, journal, modèles et fichiers publics prévus par le collecteur'},
            {'Contrôle': 'Mots de passe et clés', 'État': 'Configuration privée exclue ; à conserver séparément'},
            {'Contrôle': 'Copie hors serveur', 'État': 'Non configurée — les sauvegardes restent sur ce serveur'},
            {'Contrôle': 'Espace libre lors du relevé', 'État': _gigabytes((view.get('capacity') or {}).get('free_bytes'))},
            {'Contrôle': 'Dernier relevé', 'État': local_time(view.get('observed_at')) + ' · Kinshasa'},
        ]
        table_views.dataframe(table, hide_index=True, width='stretch', key='operations_backup_summary', table_id='operations_panel:render_backup_status:dataframe:1')
        st.caption('Cette page ne lance ni sauvegarde ni restauration. Son relevé se recharge toutes les 30 secondes tant qu’elle reste ouverte.')
