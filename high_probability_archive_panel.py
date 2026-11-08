"""History of the actual individual probabilities, independent of any group."""
import json
from pathlib import Path

import streamlit as st
from analysis_date import get_selected_day

import admin_access
import api_football as api
import high_probability_archive as archive
import high_probability_archive_view as view
import high_probability_history as groups
import high_probability_workspace as workspace
import prediction_journal as journal
from high_probability_history_panel import _load as _load_groups
from probability_reliability_panel import _storage_signature
from result_explanations import annotate_entries
from probability_history_presentation import render_match_table
from high_probability_export_panel import render_export


STATES = {'all': 'Tous les états', 'settled': 'Terminés et évalués', 'pending': 'Résultat non disponible',
          'unresolved': 'À vérifier', 'void': 'Annulés'}
VERDICTS = {'hit': 'Réalisé', 'miss': 'Non réalisé', 'pending': 'En attente',
            'unresolved': 'Non évalué', 'void': 'Annulé'}


@st.cache_data(ttl=60, max_entries=4, show_spinner=False)
def _load(root, signature, version):
    return archive.list_entries(root=root)


def _verdict(row, outcome, now):
    if row['status'] == 'settled' and row.get('actual') in groups.VALUES:
        return 'hit' if row['actual'] == outcome else 'miss'
    if row['status'] == 'pending' and journal._stamp(row['fixture']['timestamp']) <= now:
        return 'unresolved'
    return row['status'] if row['status'] in ('pending', 'unresolved', 'void') else 'unresolved'


def render_archive_history(now):
    st.subheader('Historique des fortes probabilités', icon=':material/history:')
    st.caption('Les trois probabilités, la tendance, les scores et le suivi sont réunis dans le tableau. '
               'Le match reste visible à gauche pendant le défilement des colonnes.')
    # Retire the old detail route even in an already-open browser session.
    for key in ('hp_archive_detail', 'hp_archive_detail_context', 'hp_archive_pagination'):
        st.session_state.pop(key, None)
    root = Path(api.DATA_DIR)
    try:
        stored = _load(str(root), _storage_signature(archive.database_path(root)), view.VERSION)
    except (ValueError, OSError) as error:
        st.error(f'Archive des probabilités indisponible : {error}')
        return
    try:
        entries, intents = _load_groups(str(journal.DB_PATH), str(root/'probability_scenarios.sqlite3'),
            _storage_signature(journal.DB_PATH), _storage_signature(root/'probability_scenarios.sqlite3'),
            now.astimezone(groups.ZONE).date().isoformat(), groups.VERSION)
    except (ValueError, OSError):
        entries, intents = [], []
        st.warning('Les anciennes prévisions de groupes sont indisponibles. Le classement archivé reste consultable.')
    try:
        report = view.build(stored, entries, intents, now)
    except (ValueError, OSError) as error:
        st.error(f'Historique des probabilités indisponible : {error}')
        return
    rows = annotate_entries(report['rows'], root=root, now=now)
    if report.get('legacy_unavailable'):
        st.warning('Certaines anciennes prévisions ne peuvent pas être vérifiées. Le classement archivé reste consultable.')
    if not rows:
        st.info('Aucune probabilité avant match n’a encore été archivée sur cette installation. '
                'Le prochain calcul du classement ou passage automatique conservera tous les matchs calculables.')
        st.caption('Les anciens pourcentages jamais enregistrés ne sont pas recalculés après les résultats.')
        return
    if report['legacy_matches']:
        st.caption('Certaines anciennes probabilités proviennent de groupes enregistrés ; la colonne Provenance les identifie.')
    days = sorted({row['day'] for row in rows}, reverse=True)
    today = now.astimezone(groups.ZONE).date().isoformat()
    if 'analysis_selected_day' in st.session_state:
        selected_day = get_selected_day(now).isoformat()
        days = sorted(set(days) | {selected_day}, reverse=True)
        if st.session_state.get('hp_archive_selected_day_context') != selected_day:
            st.session_state['hp_archive_selected_day_context'] = selected_day
            st.session_state['hp_archive_day'] = selected_day
    if st.session_state.get('hp_archive_day') not in days:
        st.session_state['hp_archive_day'] = next((day for day in days if day < today), days[0])
    with st.container(horizontal=True):
        day = st.selectbox('Journée des probabilités', days, key='hp_archive_day',
            format_func=lambda value: '/'.join(reversed(value.split('-'))), persist_state='page')
        state = st.selectbox('État des matchs', list(STATES), format_func=STATES.get,
            key='hp_archive_state', persist_state='page')
        query = st.text_input('Club ou compétition', key='hp_archive_search', persist_state='page')
    population = [row for row in rows if row['day'] == day]
    with st.container(horizontal=True):
        st.metric('Matchs archivés pour cette journée', len(population))
        st.metric('Terminés et évalués', sum(row['status'] == 'settled' for row in population))
        st.metric('Sans verdict', sum(row['status'] in ('pending', 'unresolved') for row in population))
    outcome = st.segmented_control('Résultat à consulter', list(groups.VALUES),
        default='1st', required=True, key='hp_archive_outcome', persist_state='page')
    with st.container(horizontal=True):
        minimum = st.number_input('Pourcentage minimum archivé (%)', min_value=0, max_value=100,
            value=0, step=1, key='hp_archive_minimum') / 100
        verdict = st.selectbox('Verdict pour cette catégorie', ['all', *VERDICTS],
            format_func=lambda value: 'Tous les verdicts' if value == 'all' else VERDICTS[value],
            key='hp_archive_verdict')
    needle = query.strip().casefold()
    filtered = [row for row in population if (state == 'all' or row['status'] == state)
        and row['probabilities'][outcome] >= minimum
        and (verdict == 'all' or _verdict(row, outcome, now) == verdict)
        and (not needle or needle in f"{row['fixture']['home']} {row['fixture']['away']} {row['competition']}".casefold())]
    filtered.sort(key=lambda row: (-row['probabilities'][outcome], row['fixture']['timestamp'], int(row['match_id'])))
    st.caption('Classement par probabilité enregistrée pour cette catégorie. '
               'Tendance = fréquence historique lissée dans la compétition ; écart = probabilité moins tendance, en points.')
    render_export(rows, default_day=day, now=now)
    if not filtered:
        st.info('Aucun match ne correspond à ces filtres. Les probabilités restent conservées.')
        return
    with st.container(horizontal=True):
        per_page = st.selectbox('Matchs par page dans l’historique', [10,25,50,100], index=1,
                               key='hp_archive_page_size', persist_state='page')
        context = (day,state,needle,outcome,minimum,verdict,per_page)
        if st.session_state.get('hp_archive_context') != context:
            st.session_state['hp_archive_context'] = context
            st.session_state['hp_archive_page'] = 0
        pages = max(1, (len(filtered)+per_page-1)//per_page)
        page = st.session_state.get('hp_archive_page')
        if type(page) is not int or not 0 <= page < pages:
            st.session_state['hp_archive_page'] = 0
        page = st.selectbox('Page des probabilités archivées', range(pages),
            format_func=lambda value:f'Page {value+1}', key='hp_archive_page', persist_state='page')
    shown = filtered[page*per_page:(page+1)*per_page]
    render_match_table(shown, outcome, now)
    st.caption(f'{page*per_page+1}–{min((page+1)*per_page,len(filtered))} sur {len(filtered)} matchs. '
               '« Non évalué » signifie qu’aucun verdict fiable ne peut encore être attribué ; ce n’est pas une défaite. '
               'Aucun résultat manquant ou annulé ne compte comme un échec.')
    _actions(population, filtered, day, report)


def _actions(population, filtered, day, report):
    if st.button('Actualiser les résultats des matchs de cette journée', icon=':material/refresh:',
        key='hp_archive_refresh', disabled=not admin_access.can_admin() or not any(
            row['source_kind'] == 'ranking_archive' for row in population)):
        admin_access.require_admin()
        try:
            workspace.refresh_archive_results([row['match_id'] for row in population
                if row['source_kind'] == 'ranking_archive'])
            _load.clear()
            st.rerun()
        except (ValueError, OSError, api.ApiFootballError) as error:
            st.error(str(error))
    st.download_button('Exporter les données brutes filtrées (JSON)', json.dumps(filtered, ensure_ascii=False, indent=2),
        file_name=f'fortes_probabilites_{day}.json', mime='application/json', key='hp_archive_export')
    if report['excluded']:
        st.warning(f"{report['excluded']} prévision(s) incompatibles écartées de cet affichage.")
