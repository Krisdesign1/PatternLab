"""Lazy, read-only multi-day observations for the local automation screen."""
import table_views
from datetime import datetime
import json
from pathlib import Path
import sqlite3

import streamlit as st

import local_operation_report as observations


@st.cache_data(ttl=60,max_entries=4,show_spinner=False)
def _report(root, days):
    return observations.collect(Path(root),days=days,include_evidence=True)


def _minutes(value):
    return round(value/60,1) if value is not None else None


def _tables(report):
    operations, forecasts = [], []
    for day in reversed(report['daily']):
        done = day['completed_refreshes']
        operations.append({'Journée':day['day'],'Calendrier':done['calendar']['count'],
            'Résultats':done['results']['count'],'Prévisions':done['forecasts']['count'],
            'Rapprochements':done['reconcile']['count'],
            'Journée calendaire close':day['closed_calendar_day']})
        results = day['result_states']
        forecasts.append({'Journée':day['day'],'Matchs uniques':day['forecast_unique_matches'],
            'Enregistrés avant match':day['forecasts_before_kickoff'],
            'Hors délai':day['forecasts_not_before_kickoff'],
            'Avance minimale (min)':_minutes(day['forecast_lead_seconds_min']),
            'Résultats exploitables':results.get('evaluated',0),'En attente':results.get('pending',0),
            'Exclus du bilan':results.get('excluded',0)})
    return operations, forecasts


def render(root, *, days=7):
    expander = st.expander('Observations par journée et preuves prédictives',
                          icon=':material/fact_check:',key='automation_daily_observations',on_change='rerun')
    if not expander.open:
        return
    with expander:
        st.caption('Lecture locale uniquement : cette vue ne lance ni collecte, ni prévision, ni entraînement. '
                   'Une journée calendaire close ne prouve pas une journée complète de stabilité ou une disponibilité continue.')
        try:
            if type(days) is not int or days<1:
                raise ValueError()
            with st.spinner('Lecture des observations locales…'):
                report = _report(str(Path(root).resolve()),min(days,31))
        except (ValueError,OSError,sqlite3.Error,KeyError,TypeError):
            st.warning('Les observations locales ne peuvent pas être lues pour le moment. '
                       'Aucune action ni modification des données n’a été exécutée.')
            return
        stamp = datetime.fromisoformat(report['as_of'])
        st.caption(f"État lu le {stamp.astimezone(observations.ZoneInfo(report['timezone'])).strftime('%d/%m/%Y à %H:%M:%S')} "
                   f"· {report['timezone']} · Réutilisation de cette lecture pendant 60 secondes maximum.")
        invalid = [name for name,state in report['component_states'].items() if state=='unavailable_or_invalid']
        if invalid:
            st.warning('Certaines sources locales sont indisponibles ou invalides ; leurs compteurs ne permettent pas de conclure.')
        if report['refresh_observations']['state']!='available':
            st.info('Aucune fin d’opération du nouveau suivi n’est encore disponible. '
                    'Les anciens passages ne sont pas reconstruits à partir des compteurs actuels.')
        if report['refresh_observations'].get('truncated') or report['automation'].get('truncated'):
            st.warning('La lecture est tronquée : les comptes d’opérations et les intervalles ne couvrent pas toute la période.')
        operations, forecasts = _tables(report)
        st.markdown('**Opérations terminées observées**')
        table_views.dataframe(operations,hide_index=True,width='stretch',key='automation_daily_operations', table_id='automation_observation_panel:render:dataframe:1')
        st.caption('Chaque nombre décrit une opération terminée enregistrée, même lorsqu’elle n’a rien ajouté. '
                   'Un zéro peut correspondre à une absence d’observation. Les nouvelles dates de suivi ne reconstituent pas le passé.')
        st.markdown('**Prévisions et résultats par journée de match**')
        table_views.dataframe(forecasts,hide_index=True,width='stretch',key='automation_daily_forecasts', table_id='automation_observation_panel:render:dataframe:2')
        st.caption('Chaque match est compté une seule fois, selon sa première prévision conservée et son coup d’envoi enregistré. '
                   'Les groupes qui se recouvrent ne multiplient pas les observations. Une attente ou une exclusion n’est pas un échec.')
        evidence = report['predictive_evidence']
        st.markdown('**Effectif du comparatif prospectif**')
        if evidence['state']=='available':
            coverage = evidence['coverage']
            with st.container(horizontal=True):
                st.metric('Matchs comparables uniques',coverage['unique_matches'])
                st.metric('Résultats évalués uniques',coverage['evaluated_unique_matches'])
                st.metric('Matchs comparables en attente',coverage['pending_unique_matches'])
                st.metric('Cohortes avec assez de blocs clos',f"{coverage['mature_cohorts']} / {coverage['cohorts']}")
            st.caption(f"Maximum observé : {coverage['max_closed_blocks']} bloc(s) clos ; "
                       f"minimum prévu : {coverage['required_blocks']}. "
                       'Les méthodes sont comparées sur les mêmes matchs, par compétition, taille et versions. '
                       'Aucun score global n’est obtenu en mélangeant les cohortes ; atteindre le minimum ne démontre pas un avantage.')
        else:
            st.info('Le comparatif prospectif est indisponible ou le protocole n’est pas encore enregistré. '
                    'Cette lecture n’en crée pas un nouveau.')
        st.download_button('Télécharger cette observation (JSON)',
            data=json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),
            file_name=f"observation_locale_{stamp.strftime('%Y%m%dT%H%M%SZ')}.json",mime='application/json',
            on_click='ignore',icon=':material/download:',key='automation_observation_download')
