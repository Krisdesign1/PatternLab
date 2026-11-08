"""Read-only prospective evidence and explicit enrollment of a candidate."""
import table_views
import json

import streamlit as st

import admin_access
import candidate_tracking as tracking
import candidate_followup_panel
from result_explanations import annotate_entries
from result_explanations_panel import render_explanation

NAMES = dict(candidate='Candidat', incumbent='CatBoost en service figé', clubs='Clubs', frequency='Répétitions seules')
STATUS = dict(pending='En attente', settled='Évalué', unresolved='À vérifier', void='Annulé')


def _signature():
    path=tracking.database_path()
    result=[]
    for item in (path,path.with_name(path.name+'-wal')):
        try:
            stat=item.stat(); result.append((str(item),stat.st_mtime_ns,stat.st_size))
        except OSError: result.append((str(item),None,None))
    return tuple(result)


@st.cache_data(ttl=60,max_entries=8,show_spinner=False)
def _summary(study_id, signature):
    return tracking.summary(study_id)


def render(item):
    st.subheader('Comparaison sur les nouveaux matchs', icon=':material/history_edu:')
    try:
        registered = [s for s in tracking.studies() if s['run_id'] == item['run_id']]
        aligned = item.get('report', {}).get('policy', {}).get('evaluation_procedure') == 'native'
        compatible=any(s.get('implementation')==tracking._implementation() for s in registered)
        if not compatible:
            st.caption('L’activation conserve les versions exactes du candidat et de CatBoost en service. '
                       'Le planificateur local comparera les quatre méthodes avant match sur les mêmes groupes du jour. '
                       'Aucun modèle ne sera remplacé.')
            if not aligned:
                st.info('Cette ancienne réévaluation conserve ses règles d’origine. Préparez un candidat aligné pour activer le nouveau suivi.')
            elif registered:
                st.warning('Le code des moteurs a changé. Les anciennes prévisions restent consultables ; '
                           'activez une nouvelle version du suivi pour les prochains matchs.')
            if st.button('Activer le suivi avant match de ce candidat', key='candidate_enroll',
                         icon=':material/schedule:', disabled=not aligned or not admin_access.can_admin()):
                admin_access.require_admin()
                activated=tracking.register(item['league'], item['run_id'])
                registered=[s for s in registered if s['id']!=activated['id']]+[activated]
                _summary.clear()
                st.success('Suivi enregistré. Les prochaines prévisions seront conservées par le planificateur local.')
        if not registered:
            return
        if len(registered)>1:
            by_id={s['id']:s for s in reversed(registered)}
            selected=st.selectbox('Version du suivi à consulter',list(by_id),
                format_func=lambda key:f"{by_id[key]['registered_at']} · {key[:12]}",
                key=f"candidate_study_{item['run_id']}")
            registered=[by_id[selected]]
        summary = _summary(registered[0]['id'],_signature())
    except (ValueError, OSError) as error:
        st.error(str(error))
        return
    st.caption(f"Suivi depuis le {registered[0]['registered_at']} · {summary['total']} groupes · "
               f"{summary['unique_matches']} rencontres distinctes, qui peuvent être partagées entre tailles.")
    size = st.segmented_control('Taille suivie', registered[0]['sizes'], required=True,
                                default=4 if 4 in registered[0]['sizes'] else registered[0]['sizes'][0],
                                key=f"candidate_size_{registered[0]['id']}")
    current = next(s for s in summary['sizes'] if s['size'] == size)
    with st.container(horizontal=True):
        for status in ('pending','settled','unresolved','void'):
            st.metric(STATUS[status], current['counts'].get(status, 0))
    table_views.dataframe([{'Méthode':NAMES[r['method']], 'Groupes évalués':r['groups'],
                   'Erreur probabiliste ↓':r['loss'], 'Premier scénario réalisé':r['top1'],
                   'Un des trois réalisé':r['top3']} for r in current['metrics']],
                 hide_index=True, width='stretch', key='candidate_future_metrics', table_id='candidate_tracking_panel:render:dataframe:1')
    aliases = dict(catboost='Candidat', combined='CatBoost en service figé', frequency='Répétitions seules')
    table_views.dataframe([{'Comparaison face à Clubs':aliases[r['method']], 'Blocs hebdomadaires clos':r['blocks'],
                   'Groupes admissibles':r['groups'], 'Gain de logloss':r['delta_loss']['estimate'],
                   'État':r['status'], 'Explication':r['reason']}
                  for r in current['evidence']['comparisons']], hide_index=True, width='stretch', table_id='candidate_tracking_panel:render:dataframe:2')
    pair=current['candidate_vs_incumbent']
    st.caption(f"Candidat face au CatBoost courant figé : {pair['blocks']} blocs clos · "
               f"gain de logloss : {pair['delta_loss']['estimate'] if pair['delta_loss']['estimate'] is not None else '—'}. "
               f"{pair['reason']}")
    st.caption('Les écarts sont descriptifs. Les semaines ouvertes et les blocs contenant des résultats '
               'en attente ou non résolus sont exclus des comparaisons closes. Aucun remplacement automatique.')
    with st.expander('Pourcentages et fréquences observées sur les nouveaux matchs'):
        table_views.dataframe([{'Méthode':NAMES[name], 'Matchs distincts évalués':value['matches'],
                       'Logloss par match ↓':value['logloss'], 'Brier multiclasse ↓':value['brier']}
                      for name,value in current['calibration'].items()], hide_index=True, width='stretch', table_id='candidate_tracking_panel:render:dataframe:3')
        label=st.segmented_control('Résultat du diagnostic', ['1st','Égal','2nd'], required=True,
                                    default='1st', key=f"candidate_calibration_{registered[0]['id']}")
        table_views.dataframe([{'Méthode':NAMES[name], 'Intervalle (%)':f"{b['lower']*100:.0f}–{b['upper']*100:.0f}",
                       'Matchs':b['count'], 'Probabilité moyenne (%)':100*b['mean_probability'],
                       'Fréquence observée (%)':100*b['observed_rate']}
                      for name,value in current['calibration'].items() for b in value['reliability'][label] if b['count']],
                     hide_index=True, width='stretch', table_id='candidate_tracking_panel:render:dataframe:4')
        st.caption('Marginales exactes des distributions enregistrées. Chaque match conserve sa première prévision '
                   'commune, pour cette taille, avant de filtrer les résultats ; seuls les groupes évalués '
                   'alimentent ce diagnostic. Un meilleur Brier ne démontre pas seul une meilleure calibration.')
    entries = [e for e in summary['entries'] if e['size'] == size]
    if entries:
        indexed = {e['id']:e for e in entries}
        selected = st.selectbox('Groupe suivi à consulter', list(indexed),
            format_func=lambda key:f"{indexed[key]['created_at']} · {STATUS[indexed[key]['status']]} · {key[:8]}",
            key=f"candidate_entry_{registered[0]['id']}_{size}")
        entry = annotate_entries([indexed[selected]], root=tracking.database_path().parent)[0]
        render_explanation(entry, key=f'candidate_result_reason_{entry["id"]}')
        table_views.dataframe([{'Match':f"{f['home']} — {f['away']}", 'Coup d’envoi UTC':f['timestamp']}
                      for f in entry['fixtures']], hide_index=True, width='stretch', table_id='candidate_tracking_panel:render:dataframe:5')
        table_views.dataframe([{'Méthode':NAMES[name], 'Scénario':' → '.join(row['sequence']),
                       'Probabilité avant match (%)':100*row['probability'],
                       'Réalisé':list(row['sequence']) == entry['actual'] if entry['actual'] is not None else None}
                      for name,payload in entry['methods'].items() for row in payload['ranking']],
                     hide_index=True, width='stretch', table_id='candidate_tracking_panel:render:dataframe:6')
        for issue in entry.get('issues',[]): st.warning(issue)
    candidate_followup_panel.render(summary['study']['id'])
    st.download_button('Télécharger le suivi prospectif', json.dumps(summary, ensure_ascii=False, indent=2),
                       file_name=f"patternlab_suivi_{registered[0]['id'][:12]}.json", mime='application/json',
                       key='candidate_future_download')
