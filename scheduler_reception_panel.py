"""Explicit, read-only reception of scheduler deadlines and operation freshness."""
import table_views
from datetime import datetime, timezone
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

import cloud_storage
import scheduler_deadlines as deadlines
import scheduler_operations as operations


ZONE = ZoneInfo('Africa/Kinshasa')
STATES = {'upcoming':'À venir', 'warmup':'Préparation du calendrier', 'open':'Fenêtre ouverte',
          'closed':'Fermeture attestée', 'closed_unverified':'Fermeture ou captures à vérifier',
          'missed':'Recensement manqué'}
PHASES = {'calendar':'Calendrier', 'results':'Résultats', 'forecasts':'Prévisions', 'reconcile':'Rapprochements'}
FRESHNESS = {'missing':'Non observé', 'recent':'Récent', 'stale':'Ancien', 'invalid_clock':'Dates à vérifier'}
CYCLE_STATES = {'completed':'Terminé', 'paused':'En pause', 'busy':'Déjà occupé',
                'error':'Erreur', 'interrupted':'Interrompu', 'running':'En cours, fin non confirmée'}
ISSUES = {
    'unknown_census':'Nombre de cibles inconnu',
    'closure_timing_unverified':'Accusé de fermeture absent',
    'known_targets_not_captured':'Cibles connues sans capture',
    'warmup_not_observed_in_window':'Préparation du calendrier non observée avant la décision',
    'registration_timing_unverified':'Inscription avant décision non attestée',
    'capture_timing_unverified':'Certaines captures ne sont pas attestées à temps',
    'census_timing_unverified':'Recensement terminé hors fenêtre',
    'future_cycle_start':'Début du cycle dans le futur',
    'future_cycle_end':'Fin du cycle dans le futur',
    'end_before_start':'Fin antérieure au début',
    'running_with_finish':'Cycle en cours portant une heure de fin',
    'finished_without_end':'Fin du cycle sans heure conservée',
    'future_signal':'Signal dans le futur', 'future_progress':'Progression dans le futur',
    'signal_before_start':'Signal antérieur au cycle', 'progress_before_start':'Progression antérieure au cycle',
    'signal_after_finish':'Signal postérieur à la fin', 'progress_after_finish':'Progression postérieure à la fin',
    'progress_after_signal':'Progression postérieure au dernier signal',
    'invalid_measured_duration':'Durée mesurée invalide',
    'running_with_measured_duration':'Durée finale présente sur un cycle en cours',
    'wall_measured_divergence':'Grand écart entre temps civil et durée mesurée',
    'measured_exceeds_wall':'Durée mesurée supérieure au temps civil',
    'future_event':'Opération dans le futur', 'event_before_cycle':'Opération antérieure au cycle',
    'event_day_mismatch':'Journée déclarée différente de la fin observée',
    'cycle_identity_unavailable':'Cycle associé non identifié de façon unique',
    'event_after_cycle_end':'Opération postérieure à la fin du cycle',
}


def _local(value):
    return datetime.fromisoformat(value).astimezone(ZONE).strftime('%d/%m/%Y %H:%M:%S') if value else '—'


def _minutes(value):
    return round(value/60, 1) if value is not None else None


def _compatibility(value):
    return 'Compatible' if value is True else 'Code incompatible' if value is False else 'Compatibilité non vérifiée'


def collect(root, *, interval_minutes=15):
    now = datetime.now(timezone.utc)
    return dict(version='scheduler-reception-v1', as_of=now.isoformat(),
                deadlines=deadlines.load(root=root, now=now),
                operations=operations.load(root, now=now, days=7, interval_minutes=interval_minutes),
                atomic_across_databases=False, predictive_improvement_demonstrated=False,
                availability_continuous_proven=False)


def _windows(report):
    if not report['protocols']:
        st.info('Aucune période de capture inscrite dans ce journal. Cette consultation n’en crée pas.')
        return
    protocols = {p['id']:p for p in report['protocols']}
    options = sorted(protocols, key=lambda pid:protocols[pid]['start'], reverse=True)
    pid = st.selectbox('Période de capture', options,
        format_func=lambda pid:f"{_local(protocols[pid]['start'])[:10]} → {_local(protocols[pid]['end'])[:10]} · {pid[:12]} · "
                               +_compatibility(protocols[pid].get('implementation_compatible')),
        key='scheduler_reception_protocol')
    compatible = protocols[pid].get('implementation_compatible')
    if compatible is not True:
        st.warning(f'{_compatibility(compatible)} — consultation uniquement. '
                   'Les fenêtres inscrites et les anciennes preuves restent visibles ; '
                   'aucune nouvelle capture n’est annoncée pour ce protocole.')
    else:
        st.caption('Code et versions compatibles au moment du contrôle. '
                   'Ce constat ne certifie pas le code déjà chargé dans un processus ni sa disponibilité.')
    rows = [r for r in report['days'] if r['protocol_id']==pid]
    due = [r for r in rows if r['state'] in ('closed','closed_unverified','missed')]
    with st.container(horizontal=True):
        st.metric('Journées inscrites', len(rows))
        st.metric('Fenêtres échues', len(due))
        st.metric('Recensements manqués', sum(r['state']=='missed' for r in due))
        st.metric('Cibles connues sans capture', sum(r['missing_known_targets'] or 0 for r in due))
    if not due:
        st.info('Aucune fenêtre échue dans cette période : sa tenue dans les délais reste à observer.'
                if compatible is True else
                'Les fenêtres inscrites restent à venir. Cette période est conservée pour consultation.')
    unknown = sum(r['target_count'] is None for r in due)
    if unknown:
        st.warning(f'{unknown} journée(s) échue(s) sans recensement : le nombre de matchs manqués est inconnu.')
    next_day = next((r for r in rows if r['state'] in ('upcoming','warmup','open')),None)
    if next_day and compatible is True:
        st.caption(f"Prochaine décision inscrite : {_local(next_day['decision_at'])} · "
                   f"préparation dès {_local(next_day['warmup_at'])} · "
                   f"fin de capture {_local(next_day['deadline_at'])}, borne exclue.")
    table_views.dataframe([{
        'Journée UTC':r['day'], 'État':STATES[r['state']],
        'Code installé':_compatibility(compatible),
        'Préparation dès · Kinshasa':_local(r['warmup_at']),
        'Décision · Kinshasa':_local(r['decision_at']),
        'Fin de capture · Kinshasa':_local(r['deadline_at']),
        'Cibles recensées':r['target_count'], 'Captures conservées':r['capture_count'],
        'Captures attestées à temps':r['timing_admissible_count'],
        'Cibles connues sans capture':r['missing_known_targets'],
        'Première capture attestée':_local(r['first_capture_ack_at']),
        'Dernière capture attestée':_local(r['last_capture_ack_at']),
        'Fermeture enregistrée':_local(r['closure_at']),
        'Accusé après fermeture':_local(r['closure_ack_at']),
        'Retard de fermeture (s)':r['closure_delay_seconds'],
        'Inscription attestée avant décision':r['registration_ready'],
        'Calendrier préparé avant décision':_local(r['warmup_observed_at']),
        'Points à vérifier':' ; '.join(ISSUES.get(v,v) for v in r['issues']),
    } for r in rows], hide_index=True, width='stretch', key='scheduler_reception_windows', table_id='scheduler_reception_panel:_windows:dataframe:1')
    st.caption('Les nombres absents restent inconnus. Une fermeture attestée ne signifie pas que toutes les cibles ont été capturées. '
               'Les captures passées ne sont jamais recréées par cette lecture.')


def _operations(report):
    st.markdown('**Fraîcheur des opérations terminées**')
    if report['state']!='available':
        st.warning('Le journal des opérations est absent ou invalide. Aucun délai de fonctionnement ne peut être confirmé ici.')
        return
    if report['truncated']:
        st.warning('La lecture des opérations est tronquée ; les intervalles ne couvrent pas toute la période.')
    if report['issues']:
        st.warning('Le journal contient des points de chronologie à vérifier. Ils restent séparés des délais et des résultats sportifs.')
        table_views.dataframe([{'Point à vérifier':ISSUES.get(code,code)} for code in report['issues']],
                     hide_index=True,width='stretch',key='scheduler_reception_issues', table_id='scheduler_reception_panel:_operations:dataframe:1')
    st.caption(f"Repère de surveillance : {_minutes(report['stale_threshold_seconds']):g} minutes, "
               'soit deux intervalles configurés. Ce repère n’est pas une garantie de délai.')
    table_views.dataframe([{'Opération':PHASES[phase], 'État':FRESHNESS[row['state']],
                   'Dernière fin observée · Kinshasa':_local(row['observed_at']),
                   'Journée concernée':row['day'], 'Ancienneté (min)':_minutes(row['age_seconds'])}
                  for phase,row in report['latest_operations'].items()],
                 hide_index=True, width='stretch', key='scheduler_reception_freshness', table_id='scheduler_reception_panel:_operations:dataframe:2')
    if any(row['state']=='stale' for row in report['latest_operations'].values()):
        st.warning('Des données sont anciennes selon ce repère, même si le processus du service est présent.')
    st.markdown('**Intervalles sans opération et reprise observée**')
    if report['resumptions']:
        table_views.dataframe([{'Cycle':r['cycle_id'], 'Opération précédente':_local(r['previous_at']),
            'Reprise observée':_local(r['resumed_at']), 'Intervalle (min)':_minutes(r['gap_seconds']),
            'Opération à la reprise':PHASES[r['resumed_phase']],
            'Dernier calendrier avant reprise':_local(r['calendar_at']),
            'Ancienneté du calendrier à la reprise (min)':_minutes(r['calendar_age_seconds']),
            'État du calendrier à la reprise':FRESHNESS[r['calendar_state']]}
            for r in report['resumptions']], hide_index=True, width='stretch', key='scheduler_reception_resumptions', table_id='scheduler_reception_panel:_operations:dataframe:3')
    else:
        st.info('Aucune reprise après un long intervalle n’est observée dans les événements lus.')
    st.markdown('**Temps civil et durée mesurée des passages**')
    table_views.dataframe([{'Cycle':r['id'], 'Début · Kinshasa':_local(r['started_at']),
        'Fin · Kinshasa':_local(r['finished_at']), 'État enregistré':CYCLE_STATES[r['status']],
        'Durée civile (min)':_minutes(r['wall_duration_seconds']),
        'Durée mesurée (min)':_minutes(r['measured_duration_seconds']),
        'Écart civil − mesuré (min)':_minutes(r['wall_minus_measured_seconds']),
        'Signal':FRESHNESS.get(r['signal_state'],r['signal_state']),
        'Points à vérifier':' ; '.join(ISSUES.get(code,code) for code in r['issues'])}
        for r in report['cycles']], hide_index=True, width='stretch', key='scheduler_reception_cycles', table_id='scheduler_reception_panel:_operations:dataframe:4')
    st.caption('Un intervalle suivi d’une opération atteste une reprise d’activité enregistrée dans le même cycle. '
               'Il ne permet pas d’attribuer la cause à une veille, une coupure réseau ou un arrêt. '
               'Un grand écart de durée reste également à expliquer.')


def render(root, *, interval_minutes=15):
    if cloud_storage.configured():
        return
    st.subheader('Délais, fraîcheur et reprises', icon=':material/pending_actions:')
    st.caption('Contrôle local sur demande, sans collecte, entraînement ni modification des prévisions. '
               'Les échéances inscrites et les opérations terminées sont contrôlées séparément.')
    key = 'scheduler_reception_report'
    signature = (str(Path(root).resolve()),interval_minutes)
    if st.session_state.get('scheduler_reception_signature') != signature:
        st.session_state.pop(key,None)
        st.session_state['scheduler_reception_signature'] = signature
    if st.button('Contrôler les délais et les reprises',icon=':material/fact_check:',key='scheduler_reception_read'):
        st.session_state.pop(key,None)
        try:
            with st.spinner('Lecture des journaux locaux…'):
                st.session_state[key] = collect(root, interval_minutes=interval_minutes)
        except (deadlines.DeadlineError,operations.OperationError,OSError,ValueError):
            st.error('Les journaux ne peuvent pas être contrôlés. Le rapport précédent a été retiré ; aucune donnée n’a été modifiée.')
    report = st.session_state.get(key)
    if report is None:
        return
    st.caption(f"Observation du {_local(report['as_of'])} · heures de Kinshasa. "
               'Les deux bases possèdent chacune leur transaction de lecture ; elles ne forment pas un instantané global.')
    _windows(report['deadlines'])
    _operations(report['operations'])
    st.download_button('Télécharger le contrôle des délais (JSON)',
        json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),
        file_name=f"patternlab_delais_{datetime.fromisoformat(report['as_of']).strftime('%Y%m%dT%H%M%SZ')}.json",
        mime='application/json',on_click='ignore',key='scheduler_reception_export')
