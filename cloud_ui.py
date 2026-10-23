"""Streamlit boundaries for the shared durable workspace, including fragments."""
from functools import wraps

import streamlit as st

import cloud_storage


def guarded(function):
    """Restore one committed workspace and prevent offline writes in cloud mode."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            with cloud_storage.transaction():
                return function(*args, **kwargs)
        except cloud_storage.CloudStorageBusy:
            st.info('Une mise à jour des données partagées est en cours. '
                    'Réessayez dans un instant ; aucune opération de cette page n’a été lancée.')
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
