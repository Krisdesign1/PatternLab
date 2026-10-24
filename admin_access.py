"""Server-side authorization for shared administration, separate from CLI workers.

No role is read from session state, query parameters, headers or browser input.
An absent or malformed configuration always leaves administration read-only.
"""
from collections.abc import Mapping
from math import isfinite
from time import time

import streamlit as st


DENIED = ('Cette commande modifie les données partagées et nécessite un compte administrateur. '
          'La consultation et vos analyses restent disponibles.')


def _secrets():
    try:
        return st.secrets.to_dict()
    except (FileNotFoundError, OSError, ValueError):
        return {}


def _access(config):
    value = config.get('access', {})
    return value if isinstance(value, Mapping) else {}


def auth_provider(config):
    """Return (configured, provider), without exposing any secret to the UI."""
    auth = config.get('auth', {})
    if not isinstance(auth, Mapping):
        return False, None
    provider = _access(config).get('provider')
    if provider is not None and (not isinstance(provider, str) or not provider or '_' in provider):
        return False, None
    credentials = auth.get(provider, {}) if provider else auth
    ready = (isinstance(credentials, Mapping)
             and all(isinstance(auth.get(key), str) and bool(auth[key].strip())
                     for key in ('redirect_uri', 'cookie_secret'))
             and all(isinstance(credentials.get(key), str) and bool(credentials[key].strip())
                     for key in ('client_id', 'client_secret', 'server_metadata_url')))
    return bool(ready), provider


def authorized(config, claims, *, server_address='', now=None):
    """Pure, fail-closed policy; callers supply only server-owned values.

    Local ownership is explicit and requires a literal loopback-only bind. The
    browser's Host/header/IP is never a substitute for that server setting.
    OIDC identities must have a current token and exact issuer/subject match, or
    a verified email explicitly listed by the owner.
    """
    if not isinstance(config, Mapping):
        return False
    access = _access(config)
    if access.get('local_owner') is True and server_address in ('127.0.0.1', '::1'):
        return True
    ready, _ = auth_provider(config)
    if not ready or not isinstance(claims, Mapping) or claims.get('is_logged_in') is not True:
        return False
    issuer, subject = claims.get('iss'), claims.get('sub')
    if not isinstance(issuer, str) or not issuer or not isinstance(subject, str) or not subject:
        return False
    expiry = claims.get('exp')
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
        return False
    current = time() if now is None else now
    if not isfinite(expiry) or not expiry > current:
        return False
    identities = access.get('admin_identities', [])
    if isinstance(identities, list) and any(
        isinstance(item, Mapping) and item.get('issuer') == issuer and item.get('subject') == subject
        for item in identities
    ):
        return True
    emails = access.get('admin_emails', [])
    email = claims.get('email')
    return (claims.get('email_verified') is True and isinstance(email, str)
            and isinstance(emails, list)
            and email.strip().casefold() in {item.strip().casefold() for item in emails
                                             if isinstance(item, str) and item.strip()})


def _claims():
    try:
        return dict(st.user)
    except (AttributeError, KeyError, RuntimeError):
        return {}


def can_admin():
    """Evaluate the current identity anew for every server action; never cached."""
    try:
        address = st.get_option('server.address')
    except (KeyError, RuntimeError):
        address = ''
    return authorized(_secrets(), _claims(), server_address=address)


def require_admin():
    """Re-check at the mutation boundary, even if a button was enabled earlier."""
    if not can_admin():
        st.warning(DENIED)
        st.stop()


def read_only_notice():
    if not can_admin():
        st.caption('Administration en lecture seule. Connectez-vous avec un compte autorisé pour modifier les données partagées.')


def render_identity():
    """Optional sign-in; public consultation does not require an account."""
    config, claims = _secrets(), _claims()
    ready, provider = auth_provider(config)
    if can_admin():
        st.caption('Administration autorisée.')
    else:
        st.caption('Consultation et analyses · administration en lecture seule.')
    if claims.get('is_logged_in') is True:
        if st.button('Se déconnecter', icon=':material/logout:', key='admin_logout'):
            st.logout()
        if not can_admin():
            st.caption('Ce compte ne dispose pas de droits administrateur, ou sa session doit être renouvelée.')
    elif ready:
        if st.button('Connexion administrateur', icon=':material/login:', key='admin_login'):
            st.login(provider)
