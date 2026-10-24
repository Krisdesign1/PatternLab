"""Read-only operational evidence for the local worker, separate from ML scores."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import streamlit as st

import api_football as api
import automation_health as health
import cloud_storage


STATES = {'completed': 'Terminé', 'paused': 'En pause', 'busy': 'Déjà occupé',
          'error': 'Erreur', 'interrupted': 'Interrompu', 'running': 'Fin non confirmée'}
ORIGINS = {'manual': 'Bouton manuel', 'service': 'Service / commande CLI', 'direct': 'Appel direct'}
ZONE = ZoneInfo('Africa/Kinshasa')


def _local(value):
    return health._stamp(value).astimezone(ZONE).strftime('%d/%m/%Y %H:%M:%S') if value else '—'


def render(config, *, scheduler=None):
    if cloud_storage.configured():
        return
    st.subheader('Suivi des passages locaux', icon=':material/monitor_heart:')
    st.caption('Ce journal conserve les exécutions du cycle de données et de prévisions depuis son activation. '
               'La préparation séparée des modèles n’est pas comprise dans ces durées. '
               'Un service chargé ou un test réussi ne prouve pas plusieurs jours de fonctionnement.')
    days = st.segmented_control('Période observée', [1, 7, 30], default=7, required=True,
                                 format_func=lambda value: '24 heures' if value == 1 else f'{value} jours',
                                 key='automation_health_days')
    now = datetime.now(timezone.utc)
    try:
        view = health.load(api.STORE_PATH.parent, now=now, days=days)
        expected = bool(config.get('enabled') and (scheduler or {}).get('running')
                        and not (scheduler or {}).get('process_running'))
        summary = health.summarize(view, now=now, schedule_expected=expected)
    except health.HealthError as error:
        st.warning(str(error))
        return
    if not view['rows']:
        st.info('Aucun passage observé sur cette période. Les anciens rapports ne sont pas réécrits dans ce journal.')
        if expected:
            st.warning('Le planificateur est chargé, mais aucun passage du service n’a encore été observé ici.')
        return
    counts = summary['counts']
    with st.container(horizontal=True):
        st.metric('Passages observés', len(view['rows']))
        st.metric('Terminés', counts['completed'])
        st.metric('En pause', counts['paused'] + counts['busy'])
        st.metric('Erreurs / interruptions', counts['error'] + counts['interrupted'])
    st.caption(f"Journal commencé le {_local(view['first_started_at'])} · Horaires de Kinshasa. "
               f"{summary['service_observations']} passage(s) du service / CLI dans cette fenêtre. "
               'Les lancements manuels restent identifiés séparément.')
    if not summary['service_observations']:
        st.info('Les passages affichés ne confirment pas encore le fonctionnement automatique du service.')
    if counts['running']:
        active = [row for row in view['rows'] if row['status']=='running']
        recent = sum(row.get('signal_state')=='recent' for row in active)
        stale = sum(row.get('signal_state')=='stale' for row in active)
        if recent:
            st.info(f'{recent} passage(s) en cours avec un signal récent. Leur fin reste à confirmer.')
        if len(active)>recent:
            st.warning(f'{len(active)-recent} passage(s) sans fin confirmée et sans signal récent. '
                       'Ils peuvent être en cours ou avoir été arrêtés ; ils ne sont pas comptés comme terminés.')
        st.caption(f'{recent} passage(s) avec signal récent · {stale} avec signal ancien. '
                   'Un signal récent confirme la présence du processus, pas la progression des données. '
                   'Les anciens cycles sans signal restent de fin inconnue.')
    if summary['gaps']:
        st.warning(f"{len(summary['gaps'])} écart(s) au rythme configuré entre les passages du service. "
                   'Un Mac en veille, un cycle long ou un arrêt peuvent expliquer une absence ; ce journal seul ne permet pas d’en attribuer la cause.')
    if view['truncated']:
        st.caption('La fenêtre contient plus de 300 passages : les compteurs et écarts portent sur les 300 plus récents.')
    with st.expander('Durées, états et écarts observés', icon=':material/history:'):
        rows = view['rows'][:50]
        st.dataframe([{'Début · Kinshasa': _local(row['started_at']),
                       'Fin · Kinshasa': _local(row['finished_at']),
                       'Durée (s)': row.get('elapsed_seconds', row['duration_seconds']),
                       'État': ('En cours · signal récent' if row['status']=='running' and row.get('signal_state')=='recent'
                                else STATES[row['status']]),
                       'Dernier signal': _local(row.get('last_seen_at')),
                       'Dernière progression': _local(row.get('last_progress_at')),
                       'Phase': health.PHASES.get(row.get('phase'),'—'),
                       'Origine': ORIGINS[row['origin']],
                       'Requêtes API': row['counters'].get('requests'),
                       'Prévisions ajoutées': row['counters'].get('created'),
                       'Motif': ' '.join(health.REASONS[code] for code in row['reasons'])}
                      for row in rows], hide_index=True, width='stretch', key='automation_health_cycles')
        st.caption(f"{len(rows)} passage(s) affiché(s), les plus récents d’abord. "
                   'Les pauses liées à API-Football restent distinctes des erreurs.')
        if summary['gaps']:
            st.dataframe([{'Passage attendu après · Kinshasa': _local(gap['start']),
                           'Passage suivant / constat · Kinshasa': _local(gap['end']),
                           'Écart (minutes)': round(gap['minutes'], 1),
                           'Toujours sans nouveau passage': gap['ongoing']}
                          for gap in summary['gaps']], hide_index=True, width='stretch',
                         key='automation_health_gaps')
        st.caption('Les écarts comparent le rythme enregistré lors de chaque cycle du service. '
                   'Aucune disponibilité n’est supposée avant le premier passage observé. '
                   'La lecture de ce suivi ne lance ni requête API ni cycle.')
