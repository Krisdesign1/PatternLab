"""Native tables for fixed-rule selections and immutable individual results."""
import table_views
from datetime import datetime, timezone, date
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

import streamlit as st

import admin_access
import api_football as api
from analysis_date import get_selected_day
from cloud_ui import guarded
import cloud_storage
import argumented_selection_workspace as workspace
import argumented_selection_store as store
from ux_theme import render_page_header


ZONE=ZoneInfo('Africa/Kinshasa')
VALUES=('1st','Égal','2nd')
STATES={'pending':'En attente','settled':'Évalué','unresolved':'À vérifier','void':'Annulé du suivi'}
ERRORS=(api.ApiFootballError,ValueError,OSError,sqlite3.Error)


def _now():
    return datetime.now(timezone.utc)


def _local(value):
    if not value:return '—'
    stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
    if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(ZONE).strftime('%d/%m/%Y %H:%M')


@st.cache_data(ttl=60,max_entries=4,show_spinner=False)
def _history(root,signature,minute):
    return store.summary(root=root)


def _outcome(value):
    return [value] if value in VALUES else []


def table_rows(rows):
    """Everything explaining a choice stays in the same row, including history."""
    values=[]
    for rank,row in enumerate(rows,1):
        f=row['fixture'];choice=row['choice'];p=row['probabilities'];b=row['baseline']
        h=row['recent_home'];a=row['recent_away']
        saved=row.get('created_at');status=row.get('status')
        verdict=('Réalisé' if row.get('correct') else 'Non réalisé') if status=='settled' else STATES.get(status,'À conserver')
        values.append({'Rang':rank,'Match':f"{f['home']} — {f['away']}",
            'Compétition':' · '.join(str(v) for v in (f.get('country'),f.get('competition',f['league'])) if v),
            'Coup d’envoi (Kinshasa)':_local(f['timestamp']),'Choix':_outcome(choice),
            'Probabilité (%)':100*p[choice],'Tendance (%)':100*b[choice],
            'Écart (points)':100*(p[choice]-b[choice]),
            '10 précédents domicile':f"{h['matching']}/{h['matches']}",
            '10 précédents extérieur':f"{a['matching']}/{a['matches']}",
            'Précédents domicile':row['home_profile']['matches'],
            'Précédents extérieur':row['away_profile']['matches'],
            'Pourquoi retenu':' · '.join(row['reasons']),
            'Conservation': 'Conservé avant match' if saved else 'Calcul local à confirmer',
            'Enregistré (Kinshasa)':_local(saved),'Verdict':[verdict],
            'Score à la pause':row.get('ht') or 'Non reçu',
            'Score à 90 minutes':row.get('ft') or 'Non reçu',
            'Résultat réel':_outcome(row.get('actual')),
            **{label+' (%)':100*p[label] for label in VALUES},
            'Période des 10 domicile':_local(h['first_at'])+' → '+_local(h['last_at']),
            'Période des 10 extérieur':_local(a['first_at'])+' → '+_local(a['last_at']),
            'Résultats du championnat':row['training'],'Journées historiques':row['history_days'],
            'Statut':STATES.get(status,'À confirmer avant match'),
            'Motif du statut':' · '.join(str(v) for v in row.get('issues',[]))})
    return values


def _table(rows,key):
    if not rows:
        st.info('Aucune rencontre pour ces filtres.')
        return
    serial=st.session_state.get(key+'_columns',0)
    if st.button('Réafficher toutes les colonnes',key=key+'_restore',icon=':material/view_column:'):
        serial+=1;st.session_state[key+'_columns']=serial
    table_views.dataframe(table_rows(rows),hide_index=True,width='stretch',row_height=64,
        column_config={'Match':st.column_config.TextColumn(width=290,pinned=True),
            'Compétition':st.column_config.TextColumn(width=220),
            'Choix':st.column_config.MultiselectColumn(options=list(VALUES),color=['#1D4ED8','#6D48B5','#B45309'],width=80),
            'Résultat réel':st.column_config.MultiselectColumn(options=list(VALUES),color=['#1D4ED8','#6D48B5','#B45309']),
            'Verdict':st.column_config.MultiselectColumn(options=['Réalisé','Non réalisé','En attente','À vérifier','Annulé du suivi','À conserver'],
                color=['#15803D','#B91C1C','#1D4ED8','#B45309','#64748B','#64748B']),
            **{field:st.column_config.NumberColumn(format='%.2f',width=115)
               for field in ('Probabilité (%)','Tendance (%)','1st (%)','Égal (%)','2nd (%)')},
            'Écart (points)':st.column_config.NumberColumn(format='%+.2f',width=110),
            'Pourquoi retenu':st.column_config.TextColumn(width=600),
            'Motif du statut':st.column_config.TextColumn(width=350)},key=key+'_'+str(serial), table_id='argumented_selection_panel:_table:dataframe:1', reset_token=serial, view_key=key)


def _filters(rows,key, *, history=False):
    with st.container(horizontal=True):
        choice=st.selectbox('Résultat retenu',['Tous',*VALUES],key=key+'_outcome')
        needle=st.text_input('Rechercher un club ou une compétition',key=key+'_search').strip().casefold()
        status=st.selectbox('État de la sélection',['Tous',*STATES.values()],key=key+'_state') if history else 'Tous'
    return [row for row in rows if (choice=='Tous' or row['choice']==choice)
        and (status=='Tous' or STATES.get(row.get('status'))==status)
        and (not needle or needle in ' '.join(str(row['fixture'].get(k,'')) for k in ('home','away','country','competition')).casefold())]


def _paged(rows,key):
    with st.container(horizontal=True):
        count=st.selectbox('Matchs par page',[25,50,100,'Tous'],index=1,key=key+'_size')
        size=(len(rows) or 1) if count=='Tous' else count
        pages=max(1,(len(rows)+size-1)//size)
        if type(st.session_state.get(key+'_page')) is not int or not 0<=st.session_state[key+'_page']<pages:
            st.session_state[key+'_page']=0
        page=st.selectbox('Page',list(range(pages)),format_func=lambda n:f'{n+1} / {pages}',key=key+'_page')
    st.caption(f'{len(rows)} rencontre(s) après les filtres. La pagination ne limite ni la sélection ni sa conservation.')
    _table(rows[page*size:(page+1)*size],key)


def _upcoming(day,now,saved):
    if day<now.astimezone(ZONE).date():
        st.info('Pour une journée passée, consultez « Historique et résultats ». Les choix ne sont pas reconstruits après les matchs.')
        return
    try:report=workspace.load_report(now=now,day=day)
    except ERRORS:
        st.error('La sélection est indisponible. Aucun pourcentage de remplacement n’est calculé.')
        return
    recorded={row['fixture']['match_id']:row for row in saved['first_matches'] if row['day']==day.isoformat()}
    rows={row['fixture']['match_id']:row for row in report['rows']}
    # A captured choice remains its first distribution, even after an import
    # changes the current ranking or the fixture no longer qualifies today.
    for mid,row in recorded.items():
        if datetime.fromisoformat(row['fixture']['timestamp'].replace('Z','+00:00')).replace(tzinfo=timezone.utc)>now:
            rows[mid]=row
    rows=sorted(rows.values(),key=lambda r:(-r['probabilities'][r['choice']],r['fixture']['timestamp'],int(r['fixture']['match_id'])))
    with st.container(horizontal=True):
        st.metric('Sélections à venir',str(len(rows)))
        st.metric('Déjà conservées',str(sum('created_at' in row for row in rows)))
        st.metric('À conserver',str(sum('created_at' not in row for row in rows)))
    st.caption('Calendrier API : '+_local(report.get('calendar_updated_at'))+' · Heure de Kinshasa. '
        'Une sélection conservée garde ses probabilités et ses motifs d’origine.')
    can_write=admin_access.can_admin() and not cloud_storage.configured()
    if st.button('Confirmer et conserver les sélections',type='primary',icon=':material/bookmark_add:',
        key='arg_record',disabled=not can_write):
        admin_access.require_admin()
        try:
            with st.spinner('Actualisation API, vérification des affiches et conservation avant match…'):
                result=workspace.capture_now(day=day)
            st.session_state['arg_capture_result']=result
            st.rerun()
        except ERRORS as error:st.error(str(error))
    result=st.session_state.get('arg_capture_result')
    if result:
        st.caption(f"Dernière confirmation : {result.get('created',0)} nouvelle(s) sélection(s), "
                   f"{result.get('existing',0)} déjà conservée(s).")
        if result.get('issues'):st.warning(' · '.join(result['issues']))
    visible=_filters(rows,'arg_current')
    _paged(visible,'arg_current_table')
    if not rows:st.info('Aucune affiche ne satisfait actuellement tous les critères avec les données disponibles.')
    if report['excluded']:
        st.markdown('**Rencontres écartées et raisons**')
        reasons={}
        for row in report['excluded']:
            text=row.get('detail',row['reason']);reasons[text]=reasons.get(text,0)+1
        table_views.dataframe([{'Motif':text,'Rencontres':count} for text,count in reasons.items()],hide_index=True,width='stretch',key='arg_exclusions', table_id='argumented_selection_panel:_upcoming:dataframe:1')
    st.download_button('Exporter la sélection et ses motifs',json.dumps(dict(report=report,displayed=visible),ensure_ascii=False,indent=2,allow_nan=False),
        file_name=f'patternlab_selection_argumentee_{day.isoformat()}.json',mime='application/json',on_click='ignore',key='arg_current_export')


def _past(saved,day):
    options=['Toutes les journées',*sorted({row['day'] for row in saved['rows']},reverse=True)]
    if st.session_state.get('arg_history_day') not in options:
        st.session_state['arg_history_day']=day.isoformat() if day.isoformat() in options else options[0]
    selected=st.selectbox('Journée des sélections',options,key='arg_history_day')
    # Resolve first observations globally BEFORE any user filter, so filtering
    # a category cannot replace a first miss with a later version's success.
    rows=[row for row in saved['first_matches'] if selected=='Toutes les journées' or row['day']==selected]
    rows=_filters(rows,'arg_history',history=True)
    first={}
    for row in sorted(rows,key=lambda r:(r['created_at'],r['id'])):first.setdefault(row['fixture']['match_id'],row)
    evaluated=[row for row in first.values() if row['status']=='settled']
    correct=sum(row['correct'] for row in evaluated)
    with st.container(horizontal=True):
        st.metric('Matchs distincts',str(len(first)))
        st.metric('Évalués',str(len(evaluated)))
        st.metric('Choix réalisés',str(correct))
        st.metric('Réussite observée',f'{100*correct/len(evaluated):.1f} %' if evaluated else '—')
    st.caption('Bilan des filtres ci-dessus. Un match compte une fois, à sa première conservation. '
               'Les choix en attente, annulés ou à vérifier restent hors du taux de réussite.')
    _paged(sorted(rows,key=lambda r:(r['day'],r['created_at']),reverse=True),'arg_history_table')
    ids={row['fixture']['match_id'] for row in rows}
    exported=dict(first_matches=rows,all_captures=[row for row in saved['rows'] if row['fixture']['match_id'] in ids])
    st.download_button('Exporter cet historique et ses versions',json.dumps(exported,ensure_ascii=False,indent=2,allow_nan=False),
        file_name='patternlab_historique_selection_argumentee.json',mime='application/json',on_click='ignore',key='arg_history_export')


@st.fragment(run_every=60)
@guarded
def render():
    now=_now();day=get_selected_day(now);root=Path(api.DATA_DIR)
    render_page_header('Sélection argumentée','Des choix expliqués, conservés avant match et suivis jusqu’aux résultats.',
        eyebrow='MÊME COMPÉTITION · BONS RÔLES · DIX PRÉCÉDENTS DISPONIBLES',icon=':material/checklist:')
    st.caption('Le choix le plus probable du modèle doit aussi être favori dans le championnat. '
        'Chaque club doit avoir au moins 10 précédents dans son rôle ; le choix doit apparaître au moins '
        '5 fois parmi les 10 derniers disponibles et y être parmi les résultats les plus fréquents.')
    st.caption('Règle exploratoire : les pourcentages et les seuils ne garantissent pas une meilleure réussite. '
        'Les dix précédents disponibles peuvent couvrir une longue période, indiquée dans le tableau.')
    view=st.segmented_control('Consulter la sélection',['upcoming','history','study'],
        format_func={'upcoming':'Sélections à venir','history':'Historique et résultats','study':'Test sur l’historique'}.get,
        default='upcoming',required=True,key='arg_view')
    if view=='study':
        from argumented_selection_study_panel import render as render_study
        render_study()
        return
    try:saved=_history(str(root),store.signature(root),now.strftime('%Y-%m-%dT%H:%M'))
    except ERRORS:
        st.error('L’historique est indisponible ; aucun résultat n’est inventé.')
        return
    if view=='history':_past(saved,day)
    else:_upcoming(day,now,saved)
    enabled=workspace.is_enabled(root)
    st.caption('La consultation reste en lecture seule. Archivage automatique local : '+('activé.' if enabled else 'désactivé.')+
        ' Le service conserve les sélections du jour après confirmation API lorsqu’il est actif ; '
        'le bouton permet aussi de confirmer la date sélectionnée.')
