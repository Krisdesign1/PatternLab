"""Consistent display-only filter for complete groups, separate from pagination."""
import streamlit as st


SIZES = tuple(range(2, 21))


def matches(size, selected):
    return selected is None or size == selected


def render(key, *, label='Nombre de matchs par groupe'):
    saved = st.session_state.get(key)
    if saved is not None and (type(saved) is not int or saved not in SIZES):
        st.session_state[key] = None
    return st.selectbox(label, [None, *SIZES],
                        format_func=lambda value: 'Toutes les tailles' if value is None else f'{value} matchs',
                        key=key, persist_state='page',
                        help='Afficher uniquement les groupes contenant exactement ce nombre de matchs. '
                             'Ce filtre ne change pas le nombre de scénarios ni la pagination.')
