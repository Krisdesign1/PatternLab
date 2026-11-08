"""Explicit read-only audit of saved group forecasts, with exact size filters."""
import table_views
import json
import sqlite3
import streamlit as st

import group_size_filter


STATUSES = {'valid':'Contrôles cohérents','unverified':'Preuves incomplètes','invalid':'Incohérences détectées'}
METHODS = {'frequency':'Répétitions','clubs':'Clubs','combined':'Combinaison',
           'catboost':'CatBoost','context_neighbors':'Contextes'}


def _load_report():
    from scenario_group_audit import load_report
    return load_report()


def _percent(value):
    return f'{100*value:.8g} %'


def render():
    st.subheader('Contrôle des scénarios',icon=':material/fact_check:')
    st.caption('Vérifier les dates, les identités et les probabilités des groupes conservés. '
               'Ce contrôle ne réécrit aucun historique et ne lance ni API ni entraînement.')
    with st.form('scenario_group_audit_form'):
        read = st.form_submit_button('Contrôler les groupes enregistrés',key='scenario_group_audit_read',
                                     icon=':material/fact_check:')
    if read:
        st.session_state.pop('scenario_group_audit_report',None)
        try:
            with st.spinner('Lecture des prévisions et vérification de leurs distributions…'):
                st.session_state['scenario_group_audit_report'] = _load_report()
        except (ValueError,OSError,TypeError,KeyError,sqlite3.Error) as error:
            st.error(f'Contrôle impossible : {error}')
    report = st.session_state.get('scenario_group_audit_report')
    if report is None:
        st.info('Lancez le contrôle pour lire le journal. Aucun historique n’est chargé à l’ouverture.')
        return
    counts = report['counts']
    st.caption('Relevé : '+report['generated_at']+' · Les effectifs ci-dessous portent sur le journal complet.')
    table_views.dataframe([{'Contrôle':label,'Groupes':counts[key]} for key,label in (
        ('total','Groupes conservés'),('valid',STATUSES['valid']),('unverified',STATUSES['unverified']),
        ('invalid',STATUSES['invalid']))],hide_index=True,width='stretch',key='scenario_group_audit_counts', table_id='scenario_group_audit_panel:render:dataframe:1')
    size = group_size_filter.render('scenario_group_audit_size')
    status = st.selectbox('État du contrôle',[None,*STATUSES],
                         format_func=lambda key: 'Tous les états' if key is None else STATUSES[key],
                         key='scenario_group_audit_status')
    query = st.text_input('Rechercher un club, une compétition ou un groupe',key='scenario_group_audit_query').strip().casefold()
    selected = [row for row in report['rows'] if group_size_filter.matches(row['size'],size)
                and (status is None or row['status']==status)
                and (not query or query in ' '.join([row['id'],*row['match_labels'],*row['leagues']]).casefold())]
    st.caption(f'{len(selected)} groupe(s) affiché(s) sur {counts["total"]}. '
               'Le filtre de taille porte sur le nombre de matchs contenus dans chaque groupe.')
    table=[]
    for row in selected:
        item={'Groupe':row['id'],'Matchs dans le groupe':row['size'],'Rencontres':' · '.join(row['match_labels']),
              'Compétitions API':' · '.join(row['leagues']),'État du contrôle':STATUSES[row['status']],
              'Coupure historique commune (UTC, exclue)':row['common_cutoff'] or 'Non vérifiée',
              'Décision conservée (UTC)':row['decision_at'] or 'Non conservée',
              'Enregistrement déclaré (UTC)':row['created_at'],
              'Motifs':' · '.join((issue.get('method','Groupe')+' : '+issue['message']) for issue in row['issues']) or 'Contrôles cohérents'}
        for method in row['methods']:
            name=METHODS.get(method['name'],method['name'])
            item[name+' · probabilités absolues']=(
                'Premier : '+_percent(method['top1_probability'])+' · Top 3 : '+_percent(method['top3_probability'])
                +' · Autres : '+_percent(method['remaining_probability']))
        table.append(item)
    if table:
        table_views.dataframe(table,hide_index=True,width='stretch',key='scenario_group_audit_groups', table_id='scenario_group_audit_panel:render:dataframe:2')
    else:
        st.info('Aucun groupe ne correspond à ces filtres.')
    st.caption('« Preuves incomplètes » signifie que certaines informations n’étaient pas conservées. '
               'Une incohérence de date ne prouve pas à elle seule l’utilisation de données futures. '
               'Ces contrôles ne changent pas les verdicts sportifs et ne certifient pas la précision du modèle. '
               'Les probabilités du Top 3 restent absolues ; elles ne sont pas ramenées à 100 %.')
    exported=dict(version=report['version'],generated_at=report['generated_at'],
                  source_sha256=report['source_sha256'],implementation=report['implementation'],
                  filters=dict(size=size,status=status,query=query),complete_ledger_counts=counts,rows=selected)
    st.download_button('Télécharger les contrôles affichés',json.dumps(exported,ensure_ascii=False,indent=2,allow_nan=False),
                       file_name='patternlab_controle_scenarios.json',mime='application/json',key='scenario_group_audit_download')
