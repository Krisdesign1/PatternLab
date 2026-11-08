import streamlit as st
from api_football_panel import render_api_football
from ux_theme import render_page_header
from cloud_ui import render_storage_status
from fixture_import_panel import render_import_review
from operations_panel import render_backup_status

render_page_header('Données et sauvegardes',
                   'Suivre la sauvegarde complète, exporter les matchs et gérer les données API-Football.',
                   eyebrow='VOTRE ESPACE DE DONNÉES', icon=':material/tune:')
st.caption('API-Football est la source unique du calendrier, des scénarios et des modèles.')
render_storage_status()
render_backup_status()
render_import_review()
with st.container(border=True):
    st.markdown('#### API-Football')
    st.caption('Calendrier, clubs, compétitions et historique HT/FT. '
               'Les saisons accessibles dépendent du forfait du compte.')
    with st.expander('Connexion API-Football, historique et export des matchs', expanded=True):
        render_api_football()
