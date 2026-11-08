"""On-demand local feature inspection; opening the view never builds a cohort."""
import table_views
from datetime import datetime,timezone
import json

import streamlit as st

import api_football as api
import causal_feature_workspace as preparation


def render():
    st.subheader('Variables avant match',icon=':material/manage_search:')
    st.caption('Préparer les entrées d’un match à partir des versions locales enregistrées avant '
               'un instant T. Cette opération inspecte les variables ; elle ne lance pas d’entraînement.')
    with st.form('causal_features_request'):
        match_id=st.text_input('Identifiant API-Football du match',key='causal_feature_match_id')
        decision=st.text_input('Instant de décision UTC',value=datetime.now(timezone.utc).isoformat(timespec='seconds'),
                               help='Exemple : 2026-10-30T18:00:00Z. Cet instant doit précéder le coup d’envoi.',
                               key='causal_feature_decision')
        submitted=st.form_submit_button('Préparer les variables avant T',icon=':material/analytics:',key='causal_feature_prepare')
    if submitted:
        st.session_state.pop('causal_feature_result',None)
        try:
            with st.spinner('Reconstruction des versions locales et calcul des variables…'):
                manifest,snapshots=preparation.prepare_cohort(api.STORE_PATH,[dict(match_id=match_id,decision_at=decision)])
            st.session_state['causal_feature_result']=dict(manifest=manifest,snapshots=snapshots)
        except (ValueError,OSError,api.ApiFootballError) as error:
            st.error(str(error))
    result=st.session_state.get('causal_feature_result')
    if result is None:
        st.info('Choisissez un match et une décision pour vérifier ses variables. '
                'Le suivi local doit contenir une affiche confirmée à venir avant cette décision.')
        return
    manifest=result['manifest']
    if manifest['exclusions']:
        st.warning('Les données conservées ne permettent pas de préparer cette décision.')
        table_views.dataframe([{'Match':row['match_id'],'Décision UTC':row['decision_at'],
                       'Motif':row['reason'],'Explication':row['detail']} for row in manifest['exclusions']],
                     hide_index=True,width='stretch',key='causal_feature_exclusions', table_id='causal_feature_panel:render:dataframe:1')
        return
    snapshot=next(iter(result['snapshots'].values()))
    fixture=snapshot['fixture']
    st.markdown(f"**{fixture['home']} — {fixture['away']}**")
    st.caption('Reconstruction conditionnelle : les heures d’import locales ne certifient pas '
               'la date de publication du fournisseur. Ces variables ne constituent pas une preuve de précision.')
    table_views.dataframe([{'Information':'Décision UTC','Valeur':snapshot['decision_at']},
                  {'Information':'Coupure historique exclusive UTC','Valeur':snapshot['cutoff_exclusive']},
                  {'Information':'Historique utilisé','Valeur':str(len(snapshot['history_rows']))},
                  {'Information':'Version des variables','Valeur':snapshot['feature_version']},
                  {'Information':'Identifiant de l’instantané','Valeur':snapshot['id']}],
                 hide_index=True,width='stretch',key='causal_feature_provenance', table_id='causal_feature_panel:render:dataframe:2')
    categorical=set(snapshot['feature_schema']['categorical_columns'])
    table_views.dataframe([{'Variable':name,'Valeur':str(snapshot['features'][name]),
                   'Type':'Catégorie' if name in categorical else 'Numérique'}
                  for name in snapshot['feature_schema']['columns']],
                 hide_index=True,width='stretch',key='causal_feature_values', table_id='causal_feature_panel:render:dataframe:3')
    table_views.dataframe([{'Match historique':row['match_id'],'Import enregistré UTC':row['observed_at_utc'],
                   'Observation':row['observation_id']} for row in snapshot['source_refs']],
                 hide_index=True,width='stretch',key='causal_feature_sources', table_id='causal_feature_panel:render:dataframe:4')
    st.download_button('Télécharger l’instantané complet',
                       json.dumps(snapshot,ensure_ascii=False,indent=2,allow_nan=False),
                       file_name=f"patternlab_variables_{fixture['match_id']}_{snapshot['id'][:12]}.json",
                       mime='application/json',key='causal_feature_download',icon=':material/download:')
