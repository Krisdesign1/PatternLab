"""Native, read-only browsing of the day's recorded football analyses."""
from datetime import date
from hashlib import sha256
from math import ceil
import re

import streamlit as st

import journal_browser_data as data
import scenario_tables
from scenario_odds import scenario_odds_text


ORIGINS = {'all': 'Toutes les origines', 'manual': 'Sélection manuelle', 'automatic': 'Automatique'}
STATES = {'all': 'Tous les états', 'pending': 'En attente', 'awaiting_results': 'Résultats attendus',
          'in_progress': 'Résultats partiels', 'settled': 'Évalué', 'void': 'Annulé du suivi', 'unresolved': 'À vérifier'}
STATE_COLORS = {'pending': 'gray', 'awaiting_results': 'orange', 'in_progress': 'blue', 'settled': 'gray',
                'void': 'gray', 'unresolved': 'orange'}


def _markdown(value):
    """Keep provider names as text instead of interpreting Markdown links/images."""
    return re.sub(r'([\\`*_{}\[\]()#+.!|>~\-])', r'\\\1', str(value))


def _reset_choice(key, choices, *, default=None):
    if st.session_state.get(key) not in choices:
        st.session_state[key] = default


def _filters(rows, key):
    leagues = sorted({str(row['league']) for row in rows},
                     key=lambda league: next((str(row['country']).casefold(), str(row['competition']).casefold(), league)
                                             for row in rows if str(row['league']) == league))
    league_names = {str(row['league']): f"{row['country']} · {row['competition']}" for row in rows}
    sizes = sorted({row['size'] for row in rows})
    _reset_choice(f'{key}_league', [None] + leagues)
    _reset_choice(f'{key}_size', [None] + sizes)
    _reset_choice(f'{key}_origin', list(ORIGINS), default='all')
    _reset_choice(f'{key}_state', list(STATES), default='all')
    _reset_choice(f'{key}_verdict', list(scenario_tables.OUTCOME_FILTERS), default='all')
    with st.container(horizontal=True):
        league = st.selectbox('Compétition des analyses', [None] + leagues,
                              format_func=lambda value: 'Toutes les compétitions' if value is None else league_names[value],
                              key=f'{key}_league', persist_state='page')
        size = st.selectbox('Taille des groupes', [None] + sizes,
                            format_func=lambda value: 'Toutes les tailles' if value is None else f'{value} matchs',
                            key=f'{key}_size', persist_state='page')
        origin = st.selectbox('Origine des analyses', list(ORIGINS), format_func=ORIGINS.get,
                              key=f'{key}_origin', persist_state='page')
        state = st.selectbox('État des analyses', list(STATES), format_func=STATES.get,
                             key=f'{key}_state', persist_state='page')
    with st.container(horizontal=True, vertical_alignment='bottom'):
        verdict = st.selectbox('Réalisation des scénarios', list(scenario_tables.OUTCOME_FILTERS),
                                format_func=scenario_tables.OUTCOME_FILTERS.get,
                                key=f'{key}_verdict', persist_state='page')
        query = st.text_input('Rechercher dans les analyses', placeholder='Club, pays ou compétition',
                              key=f'{key}_query', persist_state='page')
        per_page = st.selectbox('Analyses par page', [10, 20, 50], key=f'{key}_page_size', persist_state='page')
    return league, size, origin, state, verdict, query, per_page


def _restore_preferences(key):
    # Keep a non-widget copy as well as native page persistence: a caller may
    # replace this whole view with a detail fiche for several reruns.
    saved = st.session_state.get(f'{key}_preferences', {})
    for field in ('league', 'size', 'origin', 'state', 'verdict', 'query', 'page_size', 'page'):
        widget_key = f'{key}_{field}'
        if widget_key not in st.session_state and field in saved:
            st.session_state[widget_key] = saved[field]


def _render_row(row, key):
    entry_id = row['id']
    with st.container(gap='xsmall', key=f'{key}_row_{entry_id}'):
        group, scenario, following, action = st.columns([3.2, 3.1, 2.1, 1.3], vertical_alignment='center')
        with group:
            st.markdown(f"**{_markdown(row['competition'])}**")
            st.caption(f"{_markdown(row['country'])} · Groupe de {row['size']} matchs")
            names = row.get('match_names') or []
            st.caption(_markdown(' · '.join(names[:2])) + (f" · +{len(names)-2} affiches" if len(names) > 2 else ''))
            st.caption(ORIGINS.get(row['origin'], str(row['origin'])))
        with scenario:
            sequence = row.get('primary_sequence')
            if sequence:
                scenario_tables.render_sequence(sequence[:6])
                if len(sequence) > 6:
                    st.caption(f'Début du scénario · {len(sequence)} résultats au total')
                probability = row.get('primary_probability')
                percent = ('—' if probability is None else '< 0,01 %' if 0 < probability * 100 < .01 else
                           f'{probability * 100:.2f} %'.replace('.', ','))
                st.markdown(f"**{percent}** · scénario complet")
                st.caption(f"Cote cumulée indicative : {scenario_odds_text(sequence)} · Référence fixe manuelle")
                st.caption(f"Premier choix · {row.get('primary_method_name') or row.get('primary_method') or 'Méthode non précisée'}")
            else:
                st.caption('Premier scénario indisponible dans les données enregistrées.')
        with following:
            st.badge(row['status_label'], color=STATE_COLORS.get(row['status'], 'gray'))
            scenario_tables.render_outcome(row.get('scenario_outcome'))
            st.caption(f"{row['observed_matches']}/{row['expected_matches']} résultats connus")
            st.caption(f"Enregistré le {row['created_local']} · Kinshasa")
            if row.get('kickoff_range'):
                st.caption(f"Coups d’envoi : {row['kickoff_range']}")
        with action:
            clicked = st.button('Voir l’analyse', key=f'{key}_open_{entry_id}', icon=':material/arrow_forward:',
                                width='stretch', help='Ouvrir les matchs, les scénarios figés et le suivi de ce groupe.')
    return entry_id if clicked else None


def render(rows, day, *, key='journal_browser', historical=False):
    """Return only the complete ID explicitly opened by a native row button.

    ``rows`` is the local projection returned by journal_browser_data.build_rows.
    Filters and pagination have page persistence so a detail view can hide them
    temporarily without changing the reader's place. Nothing is selected by
    default, and this component never reads or writes the prediction journal.
    """
    if not isinstance(day, date):
        raise ValueError('La journée des analyses doit être une date locale.')
    _restore_preferences(key)
    st.subheader('Analyses et évaluations passées' if historical else 'Toutes les analyses du jour', icon=':material/view_list:')
    st.caption(f"Matchs du {day.strftime('%d/%m/%Y')} · Toutes les compétitions et tailles sont visibles par défaut.")
    league, size, origin, state, verdict, query, per_page = _filters(rows, key)
    preferences = dict(league=league, size=size, origin=origin, state=state, verdict=verdict, query=query, page_size=per_page)
    filtered = data.filter_rows(rows, league=league, size=size, origin=origin, state=state, query=query, verdict=verdict)
    context = (day.isoformat(), league, size, origin, state, verdict, query.strip().casefold(), per_page)
    page_key = f'{key}_page'
    if st.session_state.get(f'{key}_context') != context:
        st.session_state[f'{key}_context'] = context
        st.session_state[page_key] = 1
    pages = max(1, ceil(len(filtered) / per_page))
    current_page = st.session_state.get(page_key, 1)
    if type(current_page) is not int or current_page < 1:
        st.session_state[page_key] = 1
    elif current_page > pages:
        st.session_state[page_key] = pages
    st.caption(f'{len(filtered)} analyse(s) trouvée(s) sur {len(rows)} · Ordre des enregistrements, du plus récent au plus ancien.')
    if not filtered:
        st.session_state[f'{key}_preferences'] = dict(preferences, page=1)
        st.info('Aucune analyse ne correspond à ces filtres.' if rows else
                'Aucune analyse enregistrée pour cette date.' if historical else
                'Aucune analyse enregistrée pour les matchs d’aujourd’hui.')
        return None
    # Reserve the list above the native page controls while calculating its
    # slice afterwards. The reader sees pagination at the foot of the table.
    placeholder = st.container()
    with st.container(horizontal=True, horizontal_alignment='right'):
        pagination_key = f'{key}_pagination_{sha256(repr(context).encode()).hexdigest()[:16]}'
        page = st.pagination(pages, default=st.session_state.get(page_key, 1),
                             key=pagination_key, persist_state='page', disabled=pages == 1)
    st.session_state[page_key] = page
    st.session_state[f'{key}_preferences'] = dict(preferences, page=page)
    visible = filtered[(page-1) * per_page:page * per_page]
    selected = None
    with placeholder:
        with st.container(border=True, gap='small'):
            headings = st.columns([3.2, 3.1, 2.1, 1.3])
            for column, heading in zip(headings, ('Groupe et compétition', 'Scénario figé avant match', 'Suivi du groupe', 'Analyse')):
                column.caption(heading)
            for index, row in enumerate(visible):
                if index:
                    st.divider()
                clicked = _render_row(row, key)
                if clicked is not None:
                    selected = clicked
        st.caption(f'{(page-1)*per_page+1}–{min(page*per_page,len(filtered))} sur {len(filtered)} analyses · '
                   'Les scénarios et leurs probabilités restent ceux enregistrés avant match.')
    return selected
