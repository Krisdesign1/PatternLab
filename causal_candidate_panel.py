"""Explicit read-only inspection of an isolated, saved research candidate."""
import table_views
import json
from pathlib import Path

import streamlit as st

import causal_candidate as candidates


def render():
    st.subheader('Candidat conservé', icon=':material/model_training:')
    st.caption('Consulter une version de recherche déjà entraînée, ses périodes et ses références appariées. '
               'La consultation ne lance ni apprentissage ni activation.')
    with st.form('causal_candidate_report_form'):
        directory = st.text_input('Dossier du candidat', value='data/research/causal_candidates/lot05-20261031',
                                  key='causal_candidate_directory')
        submitted = st.form_submit_button('Lire le candidat conservé', key='causal_candidate_read', icon=':material/fact_check:')
    if submitted:
        st.session_state.pop('causal_candidate_report', None)
        try:
            saved = candidates.load_candidate(Path(directory.strip()))
            st.session_state['causal_candidate_report'] = saved['report']
        except (ValueError, OSError, KeyError, TypeError) as error:
            st.error(f'Lecture impossible : {error}')
    report = st.session_state.get('causal_candidate_report')
    if report is None:
        st.info('Choisissez un candidat et lancez la lecture. Aucun modèle n’est chargé à l’ouverture de cette vue.')
        return
    names = {'train': 'Entraînement', 'tuning': 'Réglage', 'calibration': 'Calibration'}
    st.info('Candidat de recherche · modèle actif conservé · aucune confirmation évaluée.')
    table_views.dataframe([{'Période': label, 'Début inclus UTC': report['plan']['windows'][name]['start'],
                   'Fin exclue UTC': report['plan']['windows'][name]['end'],
                   'Exemples': report['inputs']['phases'][name]['count']}
                  for name, label in names.items()], hide_index=True, width='stretch', key='causal_candidate_periods', table_id='causal_candidate_panel:render:dataframe:1')
    tuning = next(row['metrics'] for row in report['trials'] if row['params'] == report['selection']['params'])
    rows = []
    for phase, label in [('tuning', 'Réglage'), ('calibration', 'Calibration')]:
        metrics = [('Candidat retenu', tuning)] if phase == 'tuning' else [
            ('Candidat brut', report['calibration']['raw_metrics']),
            ('Candidat calibré', report['calibration']['calibrated_metrics'])]
        metrics += [('Clubs', report['references'][phase]['clubs']),
                    ('Fréquences du championnat', report['references'][phase]['frequency'])]
        for method, value in metrics:
            rows.append({'Période': label, 'Méthode': method, 'Matchs': value['count'],
                         'Erreur logarithmique ↓': value['logloss'], 'Brier ↓': value['brier'],
                         'Choix corrects (%)': value['accuracy']*100})
    table_views.dataframe(rows, hide_index=True, width='stretch', key='causal_candidate_comparison',
                 column_config={'Choix corrects (%)': st.column_config.NumberColumn(format='%.2f'),
                                'Erreur logarithmique ↓': st.column_config.NumberColumn(format='%.4f'),
                                'Brier ↓': st.column_config.NumberColumn(format='%.4f')}, table_id='causal_candidate_panel:render:dataframe:2')
    st.caption('Ces périodes servent au développement : le réglage choisit la variante et la calibration '
               'choisit la température. Elles ne prouvent pas un avantage sur de nouveaux matchs. '
               'Les références utilisent les mêmes cibles et les sources conservées avant leur décision.')
    st.caption(f"Température : {report['calibration']['temperature']} · Modèle final ajusté sur "
               f"{report['final_training']['count']} exemples d’entraînement et de réglage · "
               f"Terminé : {report['completed_at']}")
    st.download_button('Télécharger le rapport du candidat', json.dumps(report, ensure_ascii=False, indent=2),
                       file_name='patternlab_candidat_'+report['id'][:12]+'.json', mime='application/json',
                       key='causal_candidate_download')
