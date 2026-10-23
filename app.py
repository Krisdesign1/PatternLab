"""PatternLab: a focused scenario workspace with native multipage navigation."""
import streamlit as st
from ux_theme import render_brand
from scenario_odds import DEFAULT_ODDS, format_odds
from cloud_ui import guarded

st.set_page_config(page_title="PatternLab", page_icon=":material/schema:", layout="wide")
st.set_option("client.toolbarMode", "viewer")
if st.session_state.get('api_only_data_version') != '2026-10-18-v1':
    st.session_state.clear()
    st.cache_data.clear()
    st.cache_resource.clear()
    st.session_state['api_only_data_version'] = '2026-10-18-v1'
render_brand()
page = st.navigation([
    st.Page("ui_pages/analyze.py", title="Analyser des matchs", icon=":material/calendar_month:", default=True),
    st.Page("ui_pages/high_probabilities.py", title="Fortes probabilités", icon=":material/trending_up:"),
    st.Page("ui_pages/daily.py", title="Analyses du jour", icon=":material/stars:"),
    st.Page("ui_pages/daily_evaluation.py", title="Bilan de la sélection", icon=":material/fact_check:"),
    st.Page("ui_pages/selection_study.py", title="Comparatif de sélection", icon=":material/science:"),
    st.Page("ui_pages/explore.py", title="Explorer les scénarios", icon=":material/schema:"),
    st.Page("ui_pages/journal.py", title="Mes analyses", icon=":material/history_edu:"),
    st.Page("ui_pages/automation.py", title="Automatisation", icon=":material/schedule:"),
    st.Page("ui_pages/models.py", title="Modèles et validation", icon=":material/model_training:"),
    st.Page("ui_pages/settings.py", title="Données et réglages", icon=":material/tune:"),
])
with st.sidebar:
    st.caption("LES RÉSULTATS PAR MI-TEMPS")
    st.markdown(f":blue-badge[1st] · **{format_odds(DEFAULT_ODDS['1st'])}** · Plus de buts avant la pause  \n"
                f":violet-badge[Égal] · **{format_odds(DEFAULT_ODDS['Égal'])}** · Autant dans chaque période  \n"
                f":orange-badge[2nd] · **{format_odds(DEFAULT_ODDS['2nd'])}** · Plus de buts après la pause")
    st.caption("Cotes fixes de référence renseignées manuellement.")
    st.caption("Horaires : Africa/Kinshasa · UTC+1")
guarded(page.run)()
