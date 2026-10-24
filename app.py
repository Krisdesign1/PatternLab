"""PatternLab: a focused scenario workspace with native multipage navigation."""
import streamlit as st
from ux_theme import render_brand
from scenario_odds import DEFAULT_ODDS, format_odds
from cloud_ui import guarded, install_retry_watchdog
from admin_access import render_identity
from navigation_local import pages, render_workflow_navigation

st.set_page_config(page_title="PatternLab", page_icon=":material/schema:", layout="wide")
st.set_option("client.toolbarMode", "viewer")
if st.session_state.get('api_only_data_version') != '2026-10-18-v1':
    st.session_state.clear()
    st.session_state['api_only_data_version'] = '2026-10-18-v1'
render_brand()
page = st.navigation(pages())
with st.sidebar:
    render_identity()
    with st.expander('Comprendre 1st, Égal et 2nd'):
        st.markdown(f":blue-badge[1st] · **{format_odds(DEFAULT_ODDS['1st'])}** · Plus de buts avant la pause  \n"
                    f":violet-badge[Égal] · **{format_odds(DEFAULT_ODDS['Égal'])}** · Autant dans chaque période  \n"
                    f":orange-badge[2nd] · **{format_odds(DEFAULT_ODDS['2nd'])}** · Plus de buts après la pause")
        st.caption("Cotes fixes de référence renseignées manuellement.")
    st.caption("Horaires : Africa/Kinshasa · UTC+1")
render_workflow_navigation(page)
install_retry_watchdog()
guarded(page.run)()
