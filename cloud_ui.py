"""Streamlit boundaries for the shared durable workspace, including fragments."""
from functools import wraps
from time import monotonic

import streamlit as st

import cloud_storage


RETRY_SECONDS = 10


@st.fragment(run_every=RETRY_SECONDS)
def _busy_notice(retry_at, retry_key):
    """Retry the page after releasing all storage locks, without replaying an action."""
    st.info('Synchronisation des données en cours. La page se rechargera automatiquement '
            'dès que le stockage partagé sera disponible.')
    st.caption(f'Nouvelle tentative toutes les {RETRY_SECONDS} secondes. '
               'Aucune action de cette page n’a été exécutée pendant cette attente.')
    retry = st.button('Réessayer maintenant', icon=':material/refresh:', key=retry_key)
    if retry or monotonic() >= retry_at:
        # A full run is required: page.run() and navigation must stay outside
        # this retry fragment. A new Busy response sets a fresh future deadline.
        st.rerun(scope='app')


def guarded(function):
    """Restore one committed workspace and prevent offline writes in cloud mode."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            with cloud_storage.transaction():
                return function(*args, **kwargs)
        except cloud_storage.CloudStorageBusy:
            _busy_notice(monotonic()+RETRY_SECONDS,
                         f'cloud_retry_{function.__module__}.{function.__qualname__}')
        except cloud_storage.CloudStorageError as error:
            st.error(str(error))
            st.caption('Le stockage partagé est indisponible. Les opérations sont suspendues '
                       'pour conserver les prévisions déjà enregistrées. '
                       'La connexion se configure dans les secrets de l’hébergement.')
        return None
    return wrapped


def render_storage_status():
    """Show only connection metadata; never display or edit credentials."""
    with st.container(border=True):
        st.markdown('#### Conservation des données')
        if not cloud_storage.configured():
            st.warning('Stockage local uniquement. L’historique et les prévisions ne sont '
                       'pas encore conservés dans une base distante.')
            st.caption('Le déploiement gratuit peut perdre ses fichiers locaux. '
                       'Le guide docs/CLOUD_GRATUIT.md décrit la connexion au stockage partagé '
                       'et l’import initial de l’historique existant.')
            return
        try:
            state = cloud_storage.status()
        except cloud_storage.CloudStorageError as error:
            st.error(str(error))
            return
        if not state.get('initialized', bool(state.get('revision'))):
            st.warning('La connexion est configurée ; l’import initial de l’historique reste à effectuer.')
            return
        st.success('Stockage partagé connecté. Les prévisions sont confirmées après leur sauvegarde distante.')
        with st.container(horizontal=True):
            st.metric('Version conservée', state.get('revision', '—'))
            if isinstance(state.get('file_count'), int):
                st.metric('Fichiers conservés', state['file_count'])
            if isinstance(state.get('compressed_bytes'), int):
                st.metric('Données compressées', f"{state['compressed_bytes'] / (1024 * 1024):.1f} Mo")
        if state.get('updated_at'):
            st.caption(f"Dernière sauvegarde : {state['updated_at']}")
        st.caption('Le site et le traitement automatique utilisent le même historique. '
                   'Leur disponibilité reste soumise aux limites des services gratuits.')
