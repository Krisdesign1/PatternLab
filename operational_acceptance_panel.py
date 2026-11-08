"""Lazy native tables for the operating reception, without automatic approval."""
import table_views
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

import cloud_storage
import operational_acceptance as acceptance


ZONE=ZoneInfo('Africa/Kinshasa')
STATES={'verified':'Contrôle vérifié', 'observed':'Observation récente', 'attention':'À contrôler',
        'unknown':'Non confirmé', 'unavailable':'Indisponible', 'not_due':'Échéance à venir',
        'observing':'Observation en cours', 'not_demonstrated':'Reste à démontrer'}
DIMENSIONS={'archived':'Preuves archivées', 'current':'État actuel',
            'prospective':'Suivi prospectif', 'remaining':'Travaux restants'}
NAMES={'lot11':'Coordinateur corrigé · lot 11', 'lot12':'Restauration isolée · lot 12',
       'candidate':'Candidat inscrit et fichiers', 'registration':'Inscription avant décision',
       'followup':'Couverture des fenêtres de capture', 'freshness':'Calendrier et résultats du jour',
       'process':'Signal du service', 'continuous':'Stabilité sur plusieurs journées',
       'off_machine':'Sauvegarde hors machine', 'full_recovery':'Reprise complète du service',
       'prediction':'Avantage prédictif', 'read_failures':'Lecture des sources locales'}
PHASES={'calendar':'Calendrier', 'results':'Résultats', 'forecasts':'Prévisions', 'reconcile':'Rapprochements'}
FRESH={'recent':'Récent', 'stale':'Ancien', 'missing':'Non observé', 'invalid_clock':'Dates à vérifier'}


def _local(value):
    return datetime.fromisoformat(value).astimezone(ZONE).strftime('%d/%m/%Y %H:%M:%S') if value else '—'


def render(project_root=None):
    st.subheader('État de réception',icon=':material/fact_check:')
    st.caption('Réunir les contrôles archivés, l’état actuel du service et les conditions encore à vérifier. '
               'Cette lecture ne lance ni collecte, ni entraînement, ni restauration, ni activation de modèle.')
    if cloud_storage.configured():
        st.info('Cette réception concerne l’application et les journaux locaux du Mac.')
        return
    project=Path(project_root or Path(__file__).resolve().parent).resolve()
    key='operational_acceptance_report'
    if st.session_state.get('operational_acceptance_root')!=str(project):
        st.session_state.pop(key,None)
        st.session_state['operational_acceptance_root']=str(project)
    if st.button('Actualiser l’état de réception',icon=':material/refresh:',key='operational_acceptance_read'):
        st.session_state.pop(key,None)
        try:
            with st.spinner('Vérification des preuves et lecture des journaux locaux…'):
                st.session_state[key]=acceptance.collect(project)
        except (ValueError,OSError,KeyError,TypeError,RuntimeError):
            st.error('Le relevé ne peut pas être établi. Le précédent a été retiré ; aucune donnée active n’a été modifiée.')
    report=st.session_state.get(key)
    if report is None:
        st.info('Cliquez sur Actualiser l’état de réception. Aucun journal ni modèle n’est chargé à l’ouverture.')
        return
    st.caption(f"Relevé du {_local(report['as_of'])}, heure de Kinshasa. Les sources possèdent des lectures séparées.")
    st.info('La réception reste ouverte. Ce tableau ne prononce aucune validation globale ni promotion automatique.')
    if report['source_failures']:
        st.warning('Certaines sources sont indisponibles : les contrôles concernés ne peuvent pas être confirmés.')
    scope=st.selectbox('Afficher les contrôles',options=['all',*DIMENSIONS],
                       format_func=lambda v:'Tous les contrôles' if v=='all' else DIMENSIONS[v],
                       key='operational_acceptance_scope')
    rows=[r for r in report['criteria'] if scope=='all' or r['dimension']==scope]
    table_views.dataframe([{'Contrôle':NAMES[r['id']], 'Portée':DIMENSIONS[r['dimension']],
                   'État':STATES[r['state']], 'Constat':r['observation'],
                   'Prochaine action':r['next_action']} for r in rows],
                 hide_index=True,width='stretch',key='operational_acceptance_criteria', table_id='operational_acceptance_panel:render:dataframe:1')
    st.markdown('**Contrôles archivés conservés séparément**')
    table_views.dataframe([{'Lot':r['id'], 'État':{'verified':'Preuves vérifiées','missing':'Reçu absent','invalid':'Preuves incohérentes'}[r['state']],
                   'Réception archivée · Kinshasa':_local(r['recorded_at']),
                   'Tests réussis lors de cette réception':r['tests_passed']}
                  for r in report['evidence']['rows']],hide_index=True,width='stretch',key='operational_acceptance_archives', table_id='operational_acceptance_panel:render:dataframe:2')
    st.caption('Les suites de tests peuvent se recouvrir : leurs effectifs ne s’additionnent pas. '
               'Les empreintes sont vérifiées contre les sources archivées ; les tests et la sauvegarde complète ne sont pas relancés.')
    summary=report['protocol_summary']
    if summary:
        st.markdown('**Période inscrite retenue**')
        table_views.dataframe([{'Protocole':summary['id'], 'Candidat':summary['candidate_id'],
                       'Début inclus · Kinshasa':_local(summary['start']),
                       'Fin exclue · Kinshasa':_local(summary['end']),
                       'Code et versions compatibles':summary['implementation_compatible'],
                       'Fichiers du candidat':{'valid':'Intègres et liés au protocole',
                                              'missing':'Absents','invalid':'Incohérents'}.get(report['candidate']['status'],'Non vérifiés'),
                       'Modèle chargé pour ce contrôle':False}],
                     hide_index=True,width='stretch',key='operational_acceptance_protocol', table_id='operational_acceptance_panel:render:dataframe:3')
    op=report['operations']
    if op['state']=='available':
        st.markdown('**Dernières opérations terminées observées**')
        table_views.dataframe([{'Opération':PHASES[phase], 'État':FRESH[r['state']],
                       'Dernière fin · Kinshasa':_local(r['observed_at']),
                       'Journée déclarée':r['day'],
                       'Ancienneté (min)':round(r['age_seconds']/60,1) if r['age_seconds'] is not None else None}
                      for phase,r in op['latest_operations'].items()],
                     hide_index=True,width='stretch',key='operational_acceptance_freshness', table_id='operational_acceptance_panel:render:dataframe:4')
    st.caption('Le candidat désigne ici celui du suivi de confirmation, pas une nouvelle version automatiquement activée dans les sélections. '
               'Un signal de processus ne prouve ni la fraîcheur des scores ni un gain prédictif.')
    st.download_button('Télécharger cet état de réception (JSON)',
        json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),
        file_name=f"patternlab_reception_{datetime.fromisoformat(report['as_of']).strftime('%Y%m%dT%H%M%SZ')}.json",
        mime='application/json',on_click='ignore',key='operational_acceptance_export')
