"""Explicit chronological cohort inspection; no training from the web view."""
import table_views
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import streamlit as st

import causal_feature_artifacts as artifacts
import causal_partitions as partitions


PHASES = ('train', 'tuning', 'calibration', 'confirmation')
NAMES = {'train': 'Entraînement', 'tuning': 'Réglage',
         'calibration': 'Calibration', 'confirmation': 'Confirmation réservée'}


def windows_from_boundaries(boundaries):
    if len(boundaries) != 5:
        raise ValueError('Cinq dates UTC sont nécessaires pour définir les quatre périodes.')
    return {name: {'start': str(boundaries[index]) + 'T00:00:00Z',
                   'end': str(boundaries[index + 1]) + 'T00:00:00Z'}
            for index, name in enumerate(PHASES)}


def render():
    st.subheader('Cohortes et candidats', icon=':material/date_range:')
    tool = st.segmented_control('Contrôle des cohortes', ['periods', 'population', 'candidate', 'confirmation', 'scenarios'],
        format_func=lambda value: {'periods': 'Périodes du candidat', 'population': 'Préparation du vivier',
                                  'candidate': 'Candidat conservé', 'confirmation': 'Confirmation avant match',
                                  'scenarios': 'Contrôle des scénarios'}[value],
        default='periods', required=True, key='causal_cohort_tool', wrap=True)
    if tool == 'population':
        from causal_population_panel import render as render_population
        render_population()
        return
    if tool == 'candidate':
        from causal_candidate_panel import render as render_candidate
        render_candidate()
        return
    if tool == 'confirmation':
        from causal_confirmation_panel import render as render_confirmation
        render_confirmation()
        return
    if tool == 'scenarios':
        from scenario_group_audit_panel import render as render_scenario_audit
        render_scenario_audit()
        return
    st.caption('Vérifier quatre périodes successives à partir d’un paquet de variables conservé. '
               'La consultation ne lance ni entraînement, ni appel API, ni changement du modèle actif.')
    today = datetime.now(timezone.utc).date()
    defaults = [today - timedelta(days=90), today - timedelta(days=60),
                today - timedelta(days=30), today, today + timedelta(days=30)]
    labels = ('Début de l’entraînement', 'Début du réglage', 'Début de la calibration',
              'Début de la confirmation', 'Fin exclusive de la confirmation')
    with st.form('causal_partition_request'):
        directory = st.text_input('Dossier du paquet de variables',
                                  placeholder='data/research/causal_features/mon_paquet',
                                  key='causal_partition_directory')
        st.caption('Dates au format AAAA-MM-JJ ; les bornes sont à minuit UTC. '
                   'La date de fin d’une période est le début de la suivante.')
        boundaries = [st.text_input(label, value=default.isoformat(),
                                   key=f'causal_partition_boundary_{index}')
                      for index, (label, default) in enumerate(zip(labels, defaults))]
        submitted = st.form_submit_button('Vérifier les périodes et les données',
                                           icon=':material/fact_check:', key='causal_partition_prepare')
    if submitted:
        st.session_state.pop('causal_partition_result', None)
        try:
            if not directory.strip():
                raise ValueError('Indiquez le dossier d’un paquet de variables déjà préparé.')
            with st.spinner('Vérification du paquet et des séparations chronologiques…'):
                plan = partitions.make_plan(windows_from_boundaries(boundaries))
                bundle = artifacts.load_bundle(Path(directory.strip()))
                partition = partitions.prepare(bundle, plan)
            st.session_state['causal_partition_result'] = dict(plan=plan, partition=partition)
        except (ValueError, OSError, KeyError, TypeError) as error:
            st.error(f'Préparation impossible : {error}')
    result = st.session_state.get('causal_partition_result')
    if result is None:
        st.info('Sélectionnez un paquet de variables et les périodes, puis lancez le contrôle. '
                'Aucun fichier n’est chargé à l’ouverture de cette vue.')
        return
    plan, partition = result['plan'], result['partition']
    rows = []
    for name in PHASES:
        phase = partition['phases'][name]
        window = plan['windows'][name]
        classes = phase.get('classes')
        rows.append({'Période': NAMES[name], 'Début inclus UTC': window['start'],
                     'Fin exclue UTC': window['end'], 'Cibles demandées': phase['requested'],
                     'Cibles retenues': phase['included'],
                     'Labels utilisables': str(phase['usable_labels']) if name != 'confirmation' else 'Réservés',
                     'Classes observées': ' · '.join(f'{key} : {count}' for key, count in classes.items())
                         if classes is not None else 'Non utilisées'})
    table_views.dataframe(rows, hide_index=True, width='stretch', key='causal_partition_periods', table_id='causal_partition_panel:render:dataframe:1')
    if partition['ready']:
        st.success('Les données de développement satisfont les contrôles de préparation. '
                   'Cela ne démontre pas une amélioration prédictive.')
    else:
        st.warning('Le candidat ne peut pas encore être entraîné avec ces données et ces périodes.')
    if partition['blockers']:
        table_views.dataframe([{'Condition non satisfaite': reason} for reason in partition['blockers']],
                     hide_index=True, width='stretch', key='causal_partition_blockers', table_id='causal_partition_panel:render:dataframe:2')
    st.caption('Le paquet historique peut déjà contenir les labels de confirmation, que sa validation '
               'peut vérifier. Aucune ligne de confirmation n’est transmise au candidat : ces résultats '
               'ne servent ni au choix des réglages ni à la calibration. Les preuves locales restent '
               'rétrospectives et conditionnelles ; choisir une période future ne les transforme pas '
               'en validation prospective.')
    st.caption(f"Plan : {plan['id']} · Cohorte : {partition['cohort_id']}")
    st.download_button('Télécharger le plan et son contrôle',
                       json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False),
                       file_name=f"patternlab_cohortes_{plan['id'][:12]}.json", mime='application/json',
                       key='causal_partition_download', icon=':material/download:')
