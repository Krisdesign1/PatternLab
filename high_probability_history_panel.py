"""Local, lazy browsing of the saved high-probability selections."""
import table_views
import json
from pathlib import Path

import streamlit as st
from analysis_date import get_selected_day
from group_size_filter import render as render_group_size_filter

import admin_access
import api_football as api
import high_probability_history as history
import journal_tracking as tracking
import prediction_journal as journal
import probability_scenario_store as intents
import scenario_tables
from probability_reliability_panel import _storage_signature
from scenario_presentation import result_column
from result_explanations import annotate_entries
from result_explanations_panel import render_explanation


STATES = {'all': 'Tous les états', 'pending': 'En attente', 'settled': 'Évaluées',
          'unresolved': 'À vérifier', 'void': 'Annulées'}
VERDICTS = {'all': 'Tous les verdicts', 'hit': 'Réalisé', 'miss': 'Non réalisé',
            'pending': 'En attente', 'unresolved': 'Non évalué', 'void': 'Annulé',
            'unavailable': 'Choix non conservé'}


@st.cache_data(ttl=60, max_entries=4, show_spinner=False)
def _load(path, intent_path, revision, intent_revision, day, version):
    # The cache stores source snapshots; the pure projection checks the actual
    # clock on every render. A different day or result revision invalidates it.
    entries, choices = journal.list_entries(path), intents.list_intents(intent_path)
    linked = {choice['entry_id'] for choice in choices}
    return [entry for entry in entries if entry['id'] in linked or
            (entry.get('metadata') or {}).get('daily_policy') in history.POLICIES], choices


def _local(value):
    return journal._stamp(value).astimezone(history.ZONE).strftime('%d/%m/%Y %H:%M')


def _table(rows):
    output = []
    for row in rows:
        for index, match in enumerate(row['match_rows'], 1):
            probabilities = match['probabilities'] or {}
            explanation = match.get('result_explanation') or {}
            output.append({'Analyse': row['id'][:8] if not row['id'].startswith('intent:') else row['id'][7:15],
                'Enregistré avant match': _local(match['created_at']),
                'Ordre': match.get('order', index), 'Coup d’envoi (Kinshasa)': _local(match['kickoff']),
                'Match': f"{match['home']} — {match['away']}", 'Compétition': match['competition'],
                'Choix enregistré': [match['searched_outcome']] if match['searched_outcome'] else [],
                'Probabilité du choix (%)': (100 * match['searched_probability']
                                             if match['searched_probability'] is not None else None),
                **{label: probabilities.get(label) * 100 if probabilities.get(label) is not None else None
                   for label in history.VALUES},
                'Score à la pause': match['ht'] or '—', 'Score à 90 minutes': match['ft'] or '—',
                'Résultat réel': [match['actual']] if match['actual'] else [],
                'État du match': explanation.get('label', VERDICTS[match['verdict']]),
                'Motif': explanation.get('reason', ''),
                'Verdict du choix': [VERDICTS[match['verdict']]]})
    return output


def _show_matches(rows, key):
    table = _table(rows)
    leading = ['Match', 'État du match', 'Motif', 'Verdict du choix', 'Probabilité du choix (%)']
    columns = leading + [name for name in (table[0] if table else ()) if name not in leading]
    table_views.dataframe(table, hide_index=True, width='stretch', key=key, column_order=columns,
        column_config={
            'Match': st.column_config.TextColumn(pinned=True),
            'Motif': st.column_config.TextColumn(width='large'),
            **{label: st.column_config.NumberColumn(f'{label} (%)', format='%.2f') for label in history.VALUES},
            'Probabilité du choix (%)': st.column_config.NumberColumn(format='%.2f'),
            'Choix enregistré': result_column('Choix enregistré'),
            'Résultat réel': result_column('Résultat réel'),
            'Verdict du choix': st.column_config.MultiselectColumn(options=list(VERDICTS.values())[1:],
                color=['#15803D', '#B91C1C', '#1D4ED8', '#B45309', '#64748B', '#64748B'])}, table_id='high_probability_history_panel:_show_matches:dataframe:1')


def _choose():
    st.session_state['hp_history_selected'] = st.session_state.get('hp_history_analysis')


def _detail(row):
    entry = row['entry']
    st.subheader(f"Analyse enregistrée · {entry['size']} matchs", icon=':material/schema:')
    st.badge(STATES.get(row['status'], 'À vérifier'), color={'pending': 'blue', 'settled': 'gray',
             'unresolved': 'orange', 'void': 'gray'}.get(row['status'], 'orange'))
    st.caption(f"Prévision figée le {_local(entry['created_at'])} · Référence {entry['id'][:8]}")
    render_explanation(entry, key='hp_history_result_explanation')
    if row.get('scenario_intent'):
        st.caption(f"Choix du scénario conservé le {_local(row['selection_created_at'])}. "
                   'Plusieurs choix peuvent partager la même prévision.')
    _show_matches([row], 'hp_history_detail_matches')
    searched = row.get('searched_scenario')
    if searched:
        st.markdown('**Scénario recherché**')
        if searched.get('sequence'):
            scenario_tables.render_sequence(searched['sequence'])
            percent = searched['probability'] * 100
            st.metric('Probabilité du scénario avant match', '< 0,01 %' if 0 < percent < .01 else
                      f'{percent:.2f} %'.replace('.', ','))
        st.badge(searched['label'], color={'hit': 'green', 'miss': 'red', 'pending': 'blue',
                 'void': 'gray'}.get(searched['state'], 'orange'))
    from adaptive_scenario_proposals import effective_methods
    scenario_tables.render_archive_scope(entry)
    scenario_tables.render_matrix(effective_methods(entry), actual=row['scenario_outcome'].get('actual'),
        key=f'hp_history_matrix_{row["id"]}', title='Les scénarios conservés du moteur')
    scenario_tables.render_outcome(row['scenario_outcome'], show_sequence=True)
    for issue in entry.get('issues', []):
        st.warning(issue)
    st.caption('Les résultats proviennent du dernier rapprochement enregistré. '
               'L’ouverture de cet historique ne lance aucun appel API ni nouveau calcul de prévision.')
    if st.button('Actualiser les résultats de cette analyse', icon=':material/refresh:',
                 key='hp_history_refresh', disabled=not admin_access.can_admin()):
        admin_access.require_admin()
        try:
            tracking.refresh_entry(entry)
            _load.clear()
            st.rerun()
        except (journal.JournalError, api.ApiFootballError, ValueError, OSError) as error:
            st.error(str(error))
    box = st.expander('Historique des évaluations et corrections', key='hp_history_revisions', on_change='rerun')
    if box.open:
        with box:
            st.json(entry.get('result_history', []), expanded=False)


def render_history(now):
    st.subheader('Mes groupes enregistrés', icon=':material/history:')
    st.caption('Vos choix enregistrés avant match, avec leurs pourcentages figés et leurs résultats. '
               'Les analyses des journées à venir restent visibles en attente.')
    group_size = render_group_size_filter('hp_history_group_size')
    path, intent_path = Path(journal.DB_PATH), Path(api.DATA_DIR) / 'probability_scenarios.sqlite3'
    try:
        entries, choices = _load(str(path), str(intent_path), _storage_signature(path),
            _storage_signature(intent_path), now.astimezone(history.ZONE).date().isoformat(), history.VERSION)
        entries = annotate_entries(entries, root=path.parent, now=now)
        report = history.build(entries, choices, now)
    except (journal.JournalError, ValueError, OSError) as error:
        st.error(f'Historique local indisponible : {error}')
        return
    rows = report['rows']
    if not rows:
        st.info('Aucune analyse de fortes probabilités n’a encore été enregistrée avant match. '
                'Dans « Classement du jour », préparez votre groupe puis cliquez sur « Enregistrer avant match ».')
        st.caption('Cette vue contient les groupes enregistrés. Les classements simplement consultés se retrouvent '
                   'dans « Historique des probabilités » lorsqu’ils ont été archivés avant match. '
                   'Les anciens pourcentages non enregistrés ne peuvent pas être reconstitués après les résultats.')
        if report['excluded']:
            st.warning(f"{report['excluded']} choix hors de la période avant match écarté(s).")
        return
    with st.container(horizontal=True):
        st.metric('Prévisions distinctes', report['forecasts'])
        st.metric('Choix enregistrés', report['choices'])
        st.metric('Matchs distincts', report['fixtures'])
    st.caption('Plusieurs choix ou groupes peuvent contenir les mêmes matchs. Ces lignes ne sont pas autant '
               'd’observations indépendantes ; aucun taux global de réussite n’est calculé ici.')
    st.caption('Ces indicateurs couvrent tout l’historique, toutes tailles de groupes comprises. '
               'Le filtre de taille porte sur le groupe enregistré complet, avant les filtres par match.')
    dates = sorted({row['day'].isoformat() for row in rows}, reverse=True)
    if 'analysis_selected_day' in st.session_state:
        selected_day = get_selected_day(now).isoformat()
        dates = sorted(set(dates) | {selected_day}, reverse=True)
        if st.session_state.get('hp_history_selected_day_context') != selected_day:
            st.session_state['hp_history_selected_day_context'] = selected_day
            st.session_state['hp_history_day'] = selected_day
    options = ['all', *dates]
    if st.session_state.get('hp_history_day') not in options:
        st.session_state['hp_history_day'] = 'all'
    with st.container(horizontal=True):
        day = st.selectbox('Journée des matchs', options,
            format_func=lambda value: 'Toutes les journées' if value == 'all' else
                '/'.join(reversed(value.split('-'))), key='hp_history_day', persist_state='page')
        state = st.selectbox('État de l’analyse', list(STATES), format_func=STATES.get,
            key='hp_history_state', persist_state='page')
        query = st.text_input('Rechercher un club ou une compétition', key='hp_history_search', persist_state='page')
    selected = [row for row in rows if (day == 'all' or row['day'].isoformat() == day)
                and (group_size is None or row['entry']['size'] == group_size)
                and (state == 'all' or row['status'] == state)
                and (not query.strip() or query.strip().casefold() in ' '.join(
                    f"{match['home']} {match['away']} {match['competition']}" for match in row['match_rows']).casefold())]
    if not selected:
        st.info('Aucune analyse ne correspond à ces filtres. Les autres prévisions restent conservées.')
        return
    with st.container(horizontal=True):
        outcome = st.selectbox('Choix du match', ['all', *history.VALUES],
            format_func=lambda value: 'Tous les choix' if value == 'all' else value,
            key='hp_history_outcome', persist_state='page')
        verdict = st.selectbox('Verdict du choix', list(VERDICTS), format_func=VERDICTS.get,
            key='hp_history_verdict', persist_state='page')
    matching = [dict(row, match_rows=[match for match in row['match_rows']
                    if (outcome == 'all' or match['searched_outcome'] == outcome)
                    and (verdict == 'all' or match['verdict'] == verdict)]) for row in selected]
    matching = [row for row in matching if row['match_rows']]
    flat = [(row, match) for row in matching for match in row['match_rows']]
    if flat:
        with st.container(horizontal=True):
            per_page = st.selectbox('Lignes par page', [10, 25, 50, 100], index=2, key='hp_history_page_size')
            context = (day, group_size, state, query, outcome, verdict, per_page)
            if st.session_state.get('hp_history_page_context') != context:
                st.session_state['hp_history_page_context'] = context
                st.session_state['hp_history_page'] = 0
            pages = max(1, (len(flat) + per_page - 1) // per_page)
            current_page = st.session_state.get('hp_history_page')
            if type(current_page) is not int or not 0 <= current_page < pages:
                st.session_state['hp_history_page'] = 0
            page = st.selectbox('Page de l’historique', range(pages),
                format_func=lambda value: f'Page {value + 1}', key='hp_history_page')
        subset = [dict(row, match_rows=[match]) for row, match in flat[page*per_page:(page+1)*per_page]]
        _show_matches(subset, 'hp_history_matches')
        st.caption(f'{page*per_page+1}–{min((page+1)*per_page,len(flat))} sur {len(flat)} lignes de choix. '
                   'Réalisé / Non réalisé concerne le choix de ce match ; le scénario complet est évalué dans la fiche. '
                   '« Non évalué » indique un résultat non évaluable, pas une défaite ; le motif est affiché près du match.')
    else:
        st.info('Aucun choix de match ne correspond à ce résultat ou à ce verdict.')
    indexed = {row['id']: row for row in matching}
    if indexed:
        wanted = st.session_state.get('hp_history_selected')
        if wanted not in indexed:
            wanted = next(iter(indexed))
        if st.session_state.get('hp_history_analysis') not in indexed or st.session_state.get('hp_history_analysis') != wanted:
            st.session_state['hp_history_analysis'] = wanted
        chosen = st.selectbox('Ouvrir une analyse enregistrée', list(indexed), key='hp_history_analysis',
            format_func=lambda value: (
                f"{indexed[value]['day']:%d/%m/%Y} · {indexed[value]['entry']['size']} matchs · "
                f"{STATES[indexed[value]['status']]} · {value.removeprefix('intent:')[:8]}"), on_change=_choose)
        st.session_state['hp_history_selected'] = chosen
        # The fiche always keeps the whole original group, even when the list
        # is filtered to one outcome or one per-match verdict.
        _detail(next(row for row in rows if row['id'] == chosen))
    if matching:
        st.download_button('Exporter les analyses affichées', json.dumps([dict(row['entry'],
            scenario_intent=row.get('scenario_intent')) for row in matching], ensure_ascii=False, indent=2),
            file_name='fortes_probabilites_historique.json', mime='application/json', key='hp_history_export')
        st.caption('L’export conserve les groupes complets et leurs choix enregistrés, même si le tableau est filtré à certains matchs.')
    if report['excluded']:
        st.warning(f"{report['excluded']} choix hors de la période avant match écarté(s).")
