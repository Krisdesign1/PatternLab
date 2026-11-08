"""Owner-only access to the separate paper-bet portfolio.

Personal stakes and selections do not grant access to shared imports or models.
The existing server-side owner policy remains the sole source of identity.
"""
import streamlit as st

import admin_access


DENIED = ('La gestion des paris simulés est réservée au propriétaire. '
          'Connecte-toi avec le compte autorisé pour ajouter un pari, modifier sa mise ou le retirer. '
          'La consultation reste disponible.')


def can_edit():
    """Recheck owner identity without granting shared administration rights."""
    return admin_access.can_owner()


def require_edit():
    """Check again at every portfolio mutation, including forged button events."""
    if not can_edit():
        st.warning(DENIED)
        st.stop()


def render_notice():
    """Explain portfolio access and offer the configured native sign-in flow."""
    if can_edit():
        st.caption('Compte propriétaire connecté : tu peux ajouter tes paris simulés, '
                   'modifier leurs mises et les retirer.')
        if not admin_access.shared_writes_allowed(admin_access._secrets()):
            st.caption('Les imports, les réglages globaux et les modèles restent verrouillés '
                       'pendant la qualification du serveur.')
        return

    st.caption(DENIED)
    if admin_access._claims().get('is_logged_in') is True:
        st.caption('Ce compte n’est pas autorisé, ou sa session a expiré. '
                   'Clique sur « Se déconnecter » dans la barre latérale, puis reconnecte le compte propriétaire.')
        return

    ready, provider = admin_access.auth_provider(admin_access._secrets())
    if ready and st.button('Se connecter pour gérer mes paris', icon=':material/login:', key='paper_bet_login'):
        st.login(provider)
