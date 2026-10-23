import streamlit as st
from api_football_panel import render_api_football
from ux_theme import render_page_header
from cloud_ui import render_storage_status

render_page_header('Données et réglages',
                   'Configurer API-Football, compléter son historique et sauvegarder ses données.',
                   eyebrow='VOTRE ESPACE DE DONNÉES', icon=':material/tune:')
st.caption('API-Football est la source unique du calendrier, des scénarios et des modèles.')
render_storage_status()
with st.container(border=True):
    st.markdown('#### API-Football')
    st.caption('Calendrier, clubs, compétitions et historique HT/FT. '
               'Les saisons accessibles dépendent du forfait du compte.')
    with st.expander('Connexion API-Football, historique et sauvegarde', expanded=True):
        render_api_football()
