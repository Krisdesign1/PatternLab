"""Small native Streamlit elements shared by the three main pages."""
from pathlib import Path

import streamlit as st


LOGO_PATH = Path(__file__).resolve().parent / 'assets' / 'patternlab-mark.svg'


def render_brand():
    """Place the local PatternLab wordmark in Streamlit's header and sidebar."""
    st.logo(str(LOGO_PATH), size='large')


def render_page_header(title, description, *, eyebrow=None, icon=None):
    """Render a compact page introduction with the active native theme."""
    with st.container(gap='xsmall'):
        if eyebrow:
            st.caption(eyebrow)
        st.title(title, icon=icon)
        st.markdown(description)
