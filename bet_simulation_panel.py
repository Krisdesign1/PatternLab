"""Manually selected local paper bets; no proposal becomes a bet by itself."""
import table_views
from datetime import date, datetime, timezone
from io import StringIO
from pathlib import Path
import csv

import streamlit as st

from analysis_date import get_selected_day
from group_size_filter import render as render_group_size_filter
from cloud_ui import guarded
import api_football as api
import prediction_journal as journal
import probability_scenario_store as intents
import bet_simulation as simulation
import manual_bet_store as portfolio
import paper_bet_access as bet_access
import scenario_presentation as presentation
from scenario_odds import DEFAULT_ODDS, format_odds
from ux_theme import render_page_header


STATES = {'won': 'Gagné', 'lost': 'Perdu', 'pending': 'En attente',
          'unresolved': 'Non évaluable', 'refunded': 'Remboursé'}
DAY_FIELDS = {'bets': 'Paris simulés', 'won': 'Gagnés', 'lost': 'Perdus', 'pending': 'En attente',
              'unresolved': 'Non évaluables', 'refunded': 'Remboursés',
              'stake_total': 'Mises totales (FC)', 'stake_settled': 'Mises gagnées ou perdues (FC)',
              'stake_open': 'Mises sans verdict (FC)', 'returned': 'Retours réglés (FC)',
              'pnl': 'Bénéfice / perte (FC)', 'roi': 'Rendement réalisé (%)'}
MONEY = tuple(label for key, label in DAY_FIELDS.items() if key.startswith('stake_') or key in ('returned','pnl'))
EXCLUSIONS = {'invalid_source': 'Source non admissible', 'invalid_forecast': 'Prévision incomplète ou incohérente',
    'future_capture': 'Enregistrement daté dans le futur', 'after_kickoff': 'Enregistrement après le début du groupe',
    'mixed_days': 'Groupe réparti sur plusieurs journées', 'invalid_rankings': 'Classement non vérifiable',
    'no_valid_proposals': 'Aucun scénario valide', 'invalid_requested_scenario': 'Scénario personnalisé non vérifiable',
    'invalid_intent': 'Choix personnalisé non admissible',
    'selection_abstained': 'Aucun scénario retenu par la politique de sélection'}


def _now():
    return datetime.now(timezone.utc)


def _signature(path):
    parts = []
    for name in (str(path), str(path)+'-wal'):
        try:
            value = Path(name).stat()
            parts.append((value.st_size, value.st_mtime_ns, value.st_ino))
        except FileNotFoundError:
            parts.append(None)
    return tuple(parts)


@st.cache_data(ttl=60, max_entries=4, show_spinner=False)
def _load(journal_path, intent_path, journal_signature, intent_signature):
    entries = journal.list_entries(journal_path)
    try:
        choices = intents.list_intents(intent_path)
        warning = False
    except (ValueError, OSError):
        choices, warning = [], True
    return entries, choices, warning


@st.cache_data(ttl=60, max_entries=4, show_spinner=False)
def _catalog(journal_path, intent_path, journal_signature, intent_signature, clock_minute):
    entries, choices, warning = _load(journal_path, intent_path, journal_signature, intent_signature)
    return simulation.build(entries, choices, stake=1, now=_now()), warning


def _amount(value):
    return f'{format_odds(value)} FC'


def _stamp(value):
    return journal._stamp(value).astimezone(simulation.ZONE).strftime('%d/%m/%Y %H:%M')


def daily_rows(rows):
    return [{'Journée': date.fromisoformat(row['day']),
             'État du bilan': 'Provisoire' if row['pending'] or row['unresolved'] else 'Réglé',
             **{label: row[key] for key, label in DAY_FIELDS.items()}} for row in rows]


def ticket_rows(rows):
    result = []
    for row in rows:
        result.append({'Matchs dans l’ordre': row['matches'], 'Scénario': list(row['sequence']),
            'Méthodes et choix': ' · '.join(row['methods']),
            'Probabilités conservées (%)': row['probability_by_method'],
            'Cote cumulée indicative': row['odds'], 'Mise (FC)': row['stake'],
            'Verdict': [STATES[row['status']]], 'Retour réglé (FC)': row['return_amount'],
            'Bénéfice / perte (FC)': row['pnl'], 'Résultat réel': row.get('actual') or [],
            'Motif': row.get('reason') or '', 'Matchs': row['size'],
            'Proposition conservée avant match': _stamp(row['captured_at']),
            'Référence de l’analyse': row['entry_id']})
    return result


def _csv(rows):
    if not rows:
        return b''
    buffer = StringIO()
    names = list(rows[0])
    writer = csv.DictWriter(buffer, fieldnames=names)
    writer.writeheader()
    for row in rows:
        writer.writerow({name: ' → '.join(map(str, value)) if isinstance(value, list) else value
                         for name, value in row.items()})
    return buffer.getvalue().encode('utf-8-sig')


def _table_config():
    return {
        'Matchs dans l’ordre': st.column_config.TextColumn(pinned=True, width=420),
        'Scénario': presentation.result_column('Scénario', width=350,
            help='Chaque valeur suit l’ordre numéroté des matchs de cette ligne.'),
        'Méthodes et choix': st.column_config.TextColumn(width=300),
        'Probabilités conservées (%)': st.column_config.TextColumn(width=300,
            help='Probabilités du scénario complet enregistrées par méthode, sans les additionner.'),
        'Verdict': st.column_config.MultiselectColumn(options=list(STATES.values()),
            color=['#15803d','#b91c1c','#1d4ed8','#b45309','#64748b']),
        'Résultat réel': presentation.result_column('Résultat réel', width=350),
        'Motif': st.column_config.TextColumn(width=400),
        'Cote cumulée indicative': st.column_config.NumberColumn(format='%.2f'),
        **{label: st.column_config.NumberColumn(format='%.2f FC') for label in
           ('Mise (FC)','Retour réglé (FC)','Bénéfice / perte (FC)')}}


def _selected_rows(rows):
    display = ticket_rows(rows)
    for value, row in zip(display,rows):
        value.update({'Choix enregistré le': _stamp(row['selected_at']),
            'Mise modifiée le': _stamp(row['updated_at']),
            'Type de simulation': 'Choisi avant match' if row['timing']=='before_kickoff'
                                   else 'Rétrospectif : choix ou mise après début'})
    return display


def _choice_label(row, number):
    return f'{number} · '+ ' → '.join(row['sequence']) + ' · ' + row['matches']


def _page(rows, prefix, context):
    per_page=st.selectbox('Scénarios par page' if prefix=='bet_available' else 'Paris par page',
        [20,50,100],index=1,key=prefix+'_size')
    context=(*context,per_page)
    if st.session_state.get(prefix+'_context')!=context:
        st.session_state[prefix+'_context']=context
        st.session_state.pop(prefix+'_page',None)
    pages=max(1,(len(rows)+per_page-1)//per_page)
    key=prefix+'_page'
    if type(st.session_state.get(key,0)) is not int or not 0<=st.session_state.get(key,0)<pages:
        st.session_state.pop(key,None)
    page=st.selectbox('Page des scénarios' if prefix=='bet_available' else 'Page des paris',
        range(pages),format_func=lambda value:f'Page {value+1} sur {pages}',key=key) if pages>1 else 0
    return rows[page*per_page:(page+1)*per_page]


def _add_scenarios(catalog, saved, day, path, now):
    st.subheader('Choisir les scénarios à ajouter',icon=':material/add_circle:')
    st.caption('Choisis un scénario ou tous ceux de la journée correspondant à la taille choisie, règle la mise en FC, '
               'puis valide leur ajout à tes paris.')
    group_size=render_group_size_filter('bet_available_group_size')
    selection_context=(day,group_size)
    if st.session_state.get('bet_add_selection_context')!=selection_context:
        st.session_state['bet_add_selection_context']=selection_context
        st.session_state.pop('bet_add_all',None)
        st.session_state.pop('bet_pick_scenario',None)
    owned={row['id'] for row in saved}
    day_available=[row for row in catalog if row['day']==day and row['id'] not in owned]
    available=[row for row in day_available if group_size is None or row['size']==group_size]
    if not available:
        st.info('Aucun scénario disponible ne correspond à cette taille de groupe.' if day_available
                else 'Tous les scénarios de cette journée sont déjà dans tes paris.' if any(row['day']==day for row in catalog)
                else 'Aucun scénario conservé pour cette journée. Aucun ancien pronostic n’est reconstruit.')
        return
    size_scope='toutes tailles' if group_size is None else f'taille : {group_size} matchs'
    all_selected=st.toggle(f'Sélectionner tous les scénarios de cette journée · {size_scope}',
        value=False,key='bet_add_all')
    if all_selected:
        st.caption(f'{len(available)} scénarios sélectionnés sur toute la journée · {size_scope}, toutes pages comprises. '
                   'La recherche filtre seulement le tableau. Les paris déjà enregistrés gardent leurs mises.')
    query=st.text_input('Rechercher un match ou une méthode',key='bet_available_query')
    needle=query.strip().casefold()
    filtered=[row for row in available if not needle or needle in
              (row['matches']+' '+' '.join(row['methods'])).casefold()]
    indexed={row['id']:row for row in filtered}
    numbers={row['id']:index for index,row in enumerate(filtered,1)}
    if filtered:
        visible=_page(filtered,'bet_available',(day,group_size,needle))
        candidates=[]
        for row in visible:
            value=ticket_rows([row])[0]
            for key in ('Mise (FC)','Retour réglé (FC)','Bénéfice / perte (FC)'):
                value.pop(key,None)
            candidates.append({'N°':numbers[row['id']],**value})
        table_views.dataframe(candidates,hide_index=True,width='stretch',key='bet_available_table',
            row_height=80,column_config=_table_config(), table_id='bet_simulation_panel:_add_scenarios:dataframe:1')
    else:
        st.info('Aucun scénario ne correspond à cette recherche.')
    if all_selected:
        selected=available
    else:
        if not filtered:
            return
        if st.session_state.get('bet_pick_scenario') not in indexed:
            st.session_state.pop('bet_pick_scenario',None)
        chosen=st.selectbox('Scénario à ajouter',list(indexed),index=None,
            placeholder='Choisis un scénario du tableau',key='bet_pick_scenario',
            format_func=lambda identity:_choice_label(indexed[identity],numbers[identity]))
        selected=[indexed[chosen]] if chosen in indexed else []
    historical=sum(not row.get('kickoff_at') or journal._stamp(row['kickoff_at'])<=now for row in selected)
    if historical:
        st.caption(f'{historical} scénario(s) concernent des matchs déjà commencés : '
                   'ces ajouts seront marqués « Rétrospectif ».')
    stake=st.number_input('Mise par scénario sélectionné (FC)' if all_selected else 'Mise de ce scénario (FC)',
        min_value=.01,value=300.0,step=100.0,format='%.2f',key='bet_manual_add_stake')
    try:
        total=simulation._amount(stake)*len(selected)
        st.markdown(f'**{len(selected)} scénario(s) × {_amount(stake)} = {_amount(total)} à ajouter.**')
    except ValueError:
        st.error('Saisis une mise positive en FC.');return
    label=f'Ajouter les {len(selected)} scénarios à mes paris' if all_selected else 'Ajouter à mes paris'
    if st.button(label,icon=':material/add:',key='bet_add_submit',
                 disabled=not selected or not bet_access.can_edit()):
        bet_access.require_edit()
        try:
            if all_selected:
                portfolio.add_many(selected,stake,path=path,now=_now())
            else:
                portfolio.add(selected[0],stake,path=path,now=_now())
        except (ValueError,OSError) as error:
            st.error(str(error));return
        st.session_state['bet_manual_notice']=(f'{len(selected)} scénarios ajoutés à tes paris.' if all_selected
                                               else 'Scénario ajouté à tes paris avec sa propre mise.')
        st.rerun()


def _manage(rows, saved, path):
    if not rows:
        return
    st.subheader('Modifier ou retirer un pari',icon=':material/edit:')
    indexed={row['id']:row for row in rows}
    numbers={row['id']:index for index,row in enumerate(rows,1)}
    if st.session_state.get('bet_manage_id') not in indexed:
        st.session_state.pop('bet_manage_id',None)
    chosen=st.selectbox('Pari à gérer',list(indexed),index=None,key='bet_manage_id',
        placeholder='Choisis un pari enregistré',
        format_func=lambda identity:_choice_label(indexed[identity],numbers[identity]))
    if not chosen:
        return
    row=indexed[chosen]
    # The widget key belongs to this bet: selecting another never replaces its stake.
    with st.form('bet_edit_form_'+chosen,border=True):
        stake=st.number_input('Nouvelle mise de ce pari (FC)',min_value=.01,value=row['stake'],step=100.0,
            format='%.2f',key='bet_edit_stake_'+chosen)
        save=st.form_submit_button('Enregistrer la mise',icon=':material/save:',
            disabled=not bet_access.can_edit())
        remove=st.form_submit_button('Retirer ce pari de ma simulation',icon=':material/delete:',
            disabled=not bet_access.can_edit())
    if save or remove:
        bet_access.require_edit()
        if chosen not in {record['id'] for record in saved}:
            st.error('Ce pari n’est plus enregistré. Actualise la page.');return
        try:
            if remove:
                portfolio.remove(chosen,path=path)
                message='Pari retiré de ta simulation.'
            else:
                portfolio.update_stake(chosen,stake,path=path,now=_now())
                message='Mise de ce pari enregistrée.'
        except (ValueError,OSError) as error:
            st.error(str(error));return
        st.session_state['bet_manual_notice']=message
        st.rerun()


def _my_bets(report,saved,day,path):
    summary=next((row for row in report['days'] if row['day']==day),
                 dict(bets=0,stake_total=0,pnl=0,stake_open=0))
    st.subheader(f'Mes paris du {date.fromisoformat(day).strftime("%d/%m/%Y")}',icon=':material/calendar_today:')
    group_size=render_group_size_filter('bet_personal_group_size')
    selection_context=(day,group_size)
    if st.session_state.get('bet_manage_selection_context')!=selection_context:
        st.session_state['bet_manage_selection_context']=selection_context
        st.session_state.pop('bet_manage_id',None)
    with st.container(horizontal=True):
        st.metric('Paris choisis · journée',summary['bets'])
        st.metric('Mises choisies (FC)',_amount(summary['stake_total']))
        st.metric('Bénéfice / perte réglé (FC)',_amount(summary['pnl']))
        st.metric('Mises sans verdict (FC)',_amount(summary['stake_open']))
    st.caption('Les indicateurs et l’export de cette journée couvrent tous tes paris de la journée, '
               'toutes tailles comprises. Le filtre de taille porte sur les groupes complets de la liste et du choix à gérer.')
    if not report['tickets']:
        st.info('Tu n’as encore ajouté aucun pari. Ouvre « Ajouter des scénarios » pour choisir lesquels simuler et leur mise.')
        return
    selected=[row for row in report['tickets'] if row['day']==day]
    if selected:
        with st.container(horizontal=True):
            state=st.selectbox('Verdict de mes paris',['all',*STATES],key='bet_state',
                format_func=lambda value:'Tous les verdicts' if value=='all' else STATES[value])
            query=st.text_input('Rechercher dans mes paris',key='bet_query')
        needle=query.strip().casefold()
        sized=[row for row in selected if group_size is None or row['size']==group_size]
        filtered=[row for row in sized if (state=='all' or row['status']==state) and
                  (not needle or needle in (row['matches']+' '+' '.join(row['methods'])).casefold())]
        if filtered:
            visible=_page(filtered,'bet_personal',(day,group_size,state,needle))
            table_views.dataframe(_selected_rows(visible),hide_index=True,width='stretch',key='bet_tickets',
                row_height=80,column_config=_table_config(), table_id='bet_simulation_panel:_my_bets:dataframe:1')
            st.caption(f'{len(filtered)} paris après filtres · {len(selected)} choisis pour cette journée.')
        else:
            st.info('Aucun de tes paris ne correspond à ces filtres.')
        _manage(sized,saved,path)
        st.download_button('Exporter mes paris de cette journée',_csv(_selected_rows(selected)),mime='text/csv',
            file_name=f'patternlab_mes_paris_{day}_FC.csv',key='bet_export_tickets',icon=':material/download:')
    else:
        st.info('Tu n’as choisi aucun pari pour cette journée.')
    st.subheader('Historique · mes paris par journée',icon=':material/history:')
    daily=daily_rows(report['days'])
    table_views.dataframe(daily,hide_index=True,width='stretch',key='bet_days',
        column_config={'Journée':st.column_config.DateColumn(format='DD/MM/YYYY',pinned=True),
            **{label:st.column_config.NumberColumn(format='%.2f FC') for label in MONEY},
            'Rendement réalisé (%)':st.column_config.NumberColumn(format='%+.2f%%')}, table_id='bet_simulation_panel:_my_bets:dataframe:2')
    st.caption('Ces totaux couvrent uniquement tes choix enregistrés. Une journée reste provisoire '
               'tant que des résultats manquent. Les ajouts ou mises après le début sont signalés dans le tableau.')
    st.download_button('Exporter le bilan de mes paris',_csv(daily),mime='text/csv',
        file_name='patternlab_mes_paris_par_jour_FC.csv',key='bet_export_days',icon=':material/download:')


@st.fragment(run_every=60)
@guarded
def render_simulation():
    render_page_header('Mes paris simulés',
        'Tu choisis les scénarios et la mise de chaque pari en francs congolais.',
        eyebrow='CHOIX MANUELS · MISES EN FC · SUIVI DES RÉSULTATS',icon=':material/calculate:')
    day=get_selected_day(_now()).isoformat()
    st.caption('Aucun pari n’est ajouté automatiquement. Tes choix et leurs mises sont conservés localement. Aucun pari réel n’est placé.')
    st.caption('Cotes fixes manuelles : '+' · '.join(f'{label} : {format_odds(DEFAULT_ODDS[label])}'
        for label in ('1st','Égal','2nd'))+'. Aucune cote réelle de bookmaker n’est utilisée.')
    notice=st.session_state.pop('bet_manual_notice',None)
    if notice:
        st.success(notice)
    mode=st.segmented_control('Gérer ma simulation',['portfolio','add'],default='portfolio',required=True,
        format_func=lambda value:{'portfolio':'Mes paris','add':'Ajouter des scénarios'}[value],
        key='bet_manual_view')
    path,intent_path=Path(journal.DB_PATH),Path(api.DATA_DIR)/'probability_scenarios.sqlite3'
    try:
        catalog,missing_choices=_catalog(str(path),str(intent_path),_signature(path),_signature(intent_path),
                                         int(_now().timestamp())//60)
        saved=portfolio.list_bets()
        report=portfolio.build(catalog['tickets'],saved)
    except (ValueError,OSError) as error:
        st.error(f'Impossible de lire ta simulation : {error}');return
    if missing_choices:
        st.warning('Les scénarios personnalisés sont indisponibles. Les autres propositions restent consultables.')
    bet_access.render_notice()
    if mode=='add':
        if catalog['exclusions']:
            st.caption('Certaines propositions ne sont pas admissibles : '+
                ' · '.join(f'{EXCLUSIONS.get(key,"Autre incohérence")} : {value}'
                           for key,value in catalog['exclusions'].items()))
        _add_scenarios(catalog['tickets'],saved,day,portfolio.database_path(),_now())
    else:
        _my_bets(report,saved,day,portfolio.database_path())
        st.caption('Le retour d’un pari gagné inclut la mise ; le bénéfice en déduit la mise. '
                   'Les paris en attente ou non évaluables restent sans gain ni perte. '
                   'Convention : groupe annulé intégralement remboursé ; un report seul n’est pas une perte.')
