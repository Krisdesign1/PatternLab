"""Lightweight, explicit reads of a local preparation's persisted progress."""
import table_views
import json
from pathlib import Path

import streamlit as st

import causal_population as population


def render():
    st.subheader('Préparation du vivier', icon=':material/dataset:')
    st.caption('Suivre les cibles réellement connues avant chaque décision quotidienne. '
               'Cette vue lit les fichiers de progression ; elle ne lance ni préparation ni entraînement.')
    with st.form('causal_population_status_form'):
        directory = st.text_input('Dossier de la préparation',
            value='data/research/causal_populations/lot05-20261030', key='causal_population_directory')
        submitted = st.form_submit_button('Lire la progression', key='causal_population_read', icon=':material/refresh:')
    if submitted:
        st.session_state.pop('causal_population_status', None)
        try:
            st.session_state['causal_population_status'] = population.status(Path(directory.strip()))
        except (ValueError, OSError, KeyError, TypeError) as error:
            st.error(f'Lecture impossible : {error}')
    result = st.session_state.get('causal_population_status')
    if result is None:
        st.info('Choisissez la préparation et cliquez sur Lire la progression. Aucun fichier source n’est lu à l’ouverture.')
        return
    progress = result['progress']; policy = result['policy']
    labels = {'awaiting_census': 'Source figée · recensement à préparer',
              'preparing_features': 'Variables en préparation', 'complete': 'Préparation terminée',
              'prepared': 'Préparation terminée · vérification complète non attestée'}
    if result.get('verification', {}).get('state') == 'verified':
        labels['prepared'] = 'Vivier vérifié depuis la source figée'
    st.markdown('**'+labels.get(progress['state'], progress['state'])+'**')
    st.caption(f"Période UTC : {policy['start']} au {policy['end_exclusive']} exclu · "
               f"Décision quotidienne : {policy['hour_utc']:02d} h UTC · Relevé : {progress['updated_at']}")
    if 'verification' in result:
        st.caption('Dernière vérification complète : '+result['verification']['checked_at'])
    st.progress(progress['processed']/progress['total'] if progress['total'] else 0,
                text=f"{progress['processed']} / {progress['total']} cibles traitées")
    summary = result.get('summary')
    if summary:
        table_views.dataframe([{'Journée UTC': row['day'], 'Cibles': row['targets'], 'Variables conservées': row['snapshots'],
                       'Labels disponibles': row['labelled'], 'Résultats non évaluables': row['result_exclusions'],
                       'Profils personnalisés': row['personalized']} for row in summary['days']],
                     hide_index=True, width='stretch', key='causal_population_days', table_id='causal_population_panel:render:dataframe:1')
        st.caption('Les précédents utilisés dans un profil ne sont pas des cibles supplémentaires. '
                   'Les labels absents ne deviennent pas des pertes. Ce bilan ne mesure pas la précision du modèle.')
        st.download_button('Télécharger le bilan du vivier', json.dumps(result, ensure_ascii=False, indent=2),
                           file_name='patternlab_vivier.json', mime='application/json', key='causal_population_download')
    st.caption('Source figée locale · reconstruction rétrospective conditionnelle · modèle actif conservé.')
