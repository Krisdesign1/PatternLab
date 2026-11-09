"""Browse a complete frozen scenario distribution without enumerating its space.

The catalogue is mathematical coverage, distinct from the explicit historical
proposals and their first-choice / first-three evaluation indicators.
"""
from collections.abc import Mapping
from copy import deepcopy
import csv
from hashlib import sha256
from io import StringIO
import json
import re
from time import monotonic

import streamlit as st

from analytics import normalize_value
import frozen_scenario_catalog as catalog
from scenario_odds import DEFAULT_ODDS, combined_odds, scenario_odds_text
import scenario_tables as tables
import table_views


PAGE_SIZE = 50
ROW_OPTIONS = (50, 100, 250, 500, 'all')
PAGE_SECONDS = 5.0
# This is an explicit per-render memory budget, not a scenario-selection cap.
# Larger catalogues remain paginated/searchable and fully exportable compactly.
RENDER_BYTES = 64 * 1024 * 1024
VALUES = ('1st', 'Égal', '2nd')


def _parse_sequence(text, size):
    tokens = [part for part in re.split(r'[\s,;|>→/\-]+', str(text).strip()) if part]
    if len(tokens) != size:
        raise ValueError(f'Saisissez exactement {size} résultats dans l’ordre des matchs.')
    return [normalize_value(token) for token in tokens]


def _percent(chance):
    value = chance * 100
    return (f'{value:.6g}' if 0 < value < .01 else f'{value:.4f}').replace('.', ',') + ' %'


def _colored_sequence(sequence):
    colors = {'1st': 'blue', 'Égal': 'violet', '2nd': 'orange'}
    return ' → '.join(f'{index} :{colors[value]}-badge[{value}]'
                      for index, value in enumerate(sequence, 1))


@st.cache_data(show_spinner=False, max_entries=4)
def _page(method, offset, page_size):
    started = monotonic()

    def check_time():
        if monotonic() - started > PAGE_SECONDS:
            raise TimeoutError('Le calcul de cette page demande davantage de temps. '
                               'Consultez une page plus proche ou recherchez directement un scénario précis.')

    if page_size <= catalog.MAX_PAGE_SIZE:
        return catalog.page(method, offset=offset, page_size=page_size, check_time=check_time)
    # The public paged reader intentionally bounds each individual page. All
    # mode walks the same frozen iterator once, avoiding repeated prefix scans.
    size = catalog._size(method)
    total = 3**size
    if offset != 0 or page_size != total:
        raise ValueError('Le chargement complet doit porter sur tout le catalogue.')
    if total * (256 + 24 * size) > RENDER_BYTES:
        raise ValueError('« Tout afficher » dépasse le budget mémoire de cet affichage. '
                         'Utilisez 50, 100, 250 ou 500 lignes par page, la recherche directe '
                         'ou le catalogue complet en JSON compact. Aucun scénario n’est supprimé.')
    budget = catalog._Budget(check_time, PAGE_SECONDS)
    model, _reference = catalog._model(method, budget)
    rows = []
    for row in catalog._all(model, budget):
        budget.check()
        rows.append(row)
    budget.check(force=True)
    if len(rows) != total:
        raise ValueError('Le classement complet n’a pas pu être chargé entièrement.')
    return dict(ranking=rows, total=total, has_next=False, offset=0, page_size=total)


def _remember_page(key, saved_key):
    value = st.session_state.get(key, 1)
    st.session_state[saved_key] = value if type(value) is int else 1


def _navigate(key, saved_key, amount, pages):
    value = st.session_state.get(key, 1)
    value = value if type(value) is int else 1
    st.session_state[key] = max(1, min(pages, value + amount))
    _remember_page(key, saved_key)


def _change_rows(scope):
    # A new presentation size starts on its first page; method/distribution
    # identity and frozen probabilities never change with this preference.
    st.session_state[scope + '_saved_page'] = 1
    st.session_state[scope + '_page'] = 1


def _render_rows(rows, offset, observed, scope, size):
    if len(rows) <= 500:
        display = []
        for rank, row in enumerate(rows, offset + 1):
            item = {'Rang': rank, 'Scénario complet': _colored_sequence(row['sequence']),
                    'Cote cumulée': scenario_odds_text(row['sequence']),
                    'Probabilité du groupe (%)': _percent(row['probability'])}
            if observed is not None:
                item['Résultat final'] = ':green-badge[Scénario observé]' if list(row['sequence']) == observed else ''
            display.append(item)
        table_views.table(display, hide_index=True, width='stretch',
                          table_id='scenario_catalog_panel:render:table:1', key=scope + '_table')
        return
    # Native virtualization avoids putting tens of thousands of HTML rows in
    # the browser. Indexed badges preserve both the colours and match order.
    options = [f'{index} {label}' for index in range(1, size + 1) for label in VALUES]
    config = {'Scénario complet': st.column_config.MultiselectColumn(
        options=options, color=['#8fc1ff', '#d7b9ff', '#ffc58a'] * size),
        'Cote cumulée': st.column_config.NumberColumn(format='%.2f'),
        'Probabilité du groupe (%)': st.column_config.NumberColumn(format='%.6g')}
    display = []
    for rank, row in enumerate(rows, offset + 1):
        item = {'Rang': rank, 'Scénario complet': [f'{index} {label}'
                    for index, label in enumerate(row['sequence'], 1)],
                'Cote cumulée': combined_odds(row['sequence']),
                'Probabilité du groupe (%)': row['probability'] * 100}
        if observed is not None:
            item['Résultat final'] = ['Scénario observé'] if list(row['sequence']) == observed else []
        display.append(item)
    if observed is not None:
        config['Résultat final'] = st.column_config.MultiselectColumn(options=['Scénario observé'], color='green')
    table_views.dataframe(display, hide_index=True, width='stretch', height=650,
                         column_config=config, table_id='scenario_catalog_panel:render:table:1',
                         key=scope + '_table')


def _csv(rows, offset, actual):
    buffer = StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=['Rang', 'Scénario', 'Cote cumulée',
                                               'Probabilité (%)', 'Scénario observé'])
    writer.writeheader()
    for number, row in enumerate(rows, offset + 1):
        writer.writerow({'Rang': number, 'Scénario': ' → '.join(row['sequence']),
                         'Cote cumulée': combined_odds(row['sequence']),
                         'Probabilité (%)': row['probability'] * 100,
                         'Scénario observé': ('Oui' if list(row['sequence']) == actual else 'Non')
                         if actual is not None else ''})
    return buffer.getvalue().encode('utf-8-sig')


def _compact(method, descriptor, fixtures):
    value = dict(scope='complete_frozen_distribution', catalogue=deepcopy(descriptor),
                 method=deepcopy(method), note='Catalogue des possibilités, distinct des propositions historiques.',
                 reference_odds=dict(source='manual_fixed_reference', per_outcome=dict(DEFAULT_ODDS),
                     calculation='product_of_all_outcomes_in_sequence', rounding='display_only_2_decimals'))
    if fixtures:
        value['ordered_fixtures'] = [{name: item[name] for name in
            ('match_id', 'league', 'home', 'away', 'timestamp') if name in item} for item in fixtures]
    return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode('utf-8')


@st.fragment
def render(methods, fixtures=None, actual=None, *, key='scenario_catalog', title=None):
    """Show the complete ordered catalogue using only the supplied snapshots.

    Only one presentation page is materialized at a time. The compact download
    carries the whole immutable distribution and its catalogue descriptor.
    """
    available, descriptors, rejected = {}, {}, []
    order = list(tables.METHOD_ORDER) + sorted(set(methods) - set(tables.METHOD_ORDER))
    for name in order:
        method = methods.get(name)
        if not isinstance(method, Mapping) or method.get('distribution') is None:
            continue
        try:
            descriptor = catalog.descriptor(method)
            if method.get('scenario_catalog') is not None:
                catalog.validate_descriptor(method, method['scenario_catalog'])
            available[name] = method
            descriptors[name] = descriptor
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            rejected.append(f'{tables.METHOD_LABELS.get(name, name)} : {error}')
    if not available:
        st.caption('Catalogue complet indisponible : cette archive ne conserve pas de distribution complète vérifiable. '
                   'Les choix explicitement enregistrés restent consultables ci-dessous.')
        for error in rejected:
            st.warning(error)
        return None
    st.subheader(title or 'Tous les scénarios possibles', icon=':material/view_list:')
    st.caption('Classement complet par probabilité décroissante, sans coupure à trois. '
               'Les pourcentages concernent le groupe entier et ne sont pas recalculés entre les lignes affichées.')
    st.caption('Cotes cumulées indicatives : produit des références fixes manuelles '
               '1st = 3,10 · Égal = 3,00 · 2nd = 2,10. Ce ne sont pas des cotes relevées par l’API.')
    base = str(key) + '_catalog'
    method_key = base + '_method'
    if st.session_state.get(method_key) not in available:
        st.session_state[method_key] = next(iter(available))
    name = st.selectbox('Méthode du classement complet', list(available),
                        format_func=lambda value: tables.METHOD_LABELS.get(value, value), key=method_key,
                        persist_state='session')
    if name not in available:
        st.warning('Choisissez une méthode disponible.')
        return None
    method, descriptor = available[name], descriptors[name]
    size, total = descriptor['size'], descriptor['total']
    identity = descriptor.get('sha256') or sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest()
    scope = f'{base}_{name}_{identity[:16]}'
    rows_key = scope + '_rows'
    if st.session_state.get(rows_key) not in ROW_OPTIONS:
        st.session_state[rows_key] = PAGE_SIZE
    selected_rows = st.selectbox('Scénarios à afficher', ROW_OPTIONS,
        format_func=lambda value: 'Tout afficher' if value == 'all' else str(value),
        key=rows_key, persist_state='session', on_change=_change_rows, args=(scope,))
    if selected_rows not in ROW_OPTIONS:
        st.warning('Choisissez un nombre de lignes disponible.')
        return None
    page_size = total if selected_rows == 'all' else selected_rows
    pages = max(1, (total + page_size - 1) // page_size)
    page_key = scope + '_page'
    # Widget keys disappear when another method is displayed. Keep a separate
    # non-widget value so returning to a method restores its page.
    saved_page_key = scope + '_saved_page'
    value = st.session_state.get(saved_page_key, 1)
    st.session_state[page_key] = max(1, min(pages, value if type(value) is int else 1))
    observed = list(actual) if isinstance(actual, (list, tuple)) and len(actual) == size and all(
        value in VALUES for value in actual) else None
    st.metric('Scénarios possibles pour ce groupe', f'{total:,}'.replace(',', ' '))
    pages_text = f'{pages:,}'.replace(',', ' ')
    rows_label = 'Toutes les lignes demandées' if selected_rows == 'all' else f'{page_size} lignes par page'
    st.caption(f'{size} matchs · {rows_label} · {pages_text} page(s). '
               'La pagination change uniquement les lignes affichées, pas le catalogue conservé.')
    if method.get('scenario_catalog') is not None:
        st.caption('Le catalogue complet est lié à la distribution conservée avec ce calcul.')
    else:
        st.caption('Archive ancienne : consultation mathématique de la distribution conservée. '
                   'Les possibilités supplémentaires ne deviennent pas des propositions enregistrées avant match.')
    st.caption('Le catalogue contient toutes les possibilités. Y retrouver le résultat final ne mesure pas '
               'la réussite d’une sélection ; les bilans « premier choix » et « trois premiers » restent séparés.')
    if observed is not None:
        try:
            found = catalog.lookup(method, observed)
            tables.render_sequence(observed, label='Scénario observé')
            st.caption('Probabilité conservée du scénario observé : ' + _percent(found['probability']))
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            st.warning('Probabilité du résultat observé indisponible : ' + str(error))
    with st.form(scope + '_search_form'):
        query = st.text_input('Rechercher un scénario complet', key=scope + '_search',
            placeholder='Ex. 2nd → 1st → Égal',
            help=f'Saisissez {size} résultats, dans l’ordre des matchs, séparés par des flèches ou des espaces.')
        submitted = st.form_submit_button('Consulter sa probabilité', icon=':material/search:')
    lookup_key = scope + '_lookup'
    if submitted:
        try:
            sequence = _parse_sequence(query, size)
            st.session_state[lookup_key] = dict(result=catalog.lookup(method, sequence))
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            st.session_state[lookup_key] = dict(error=str(error))
    selected = st.session_state.get(lookup_key)
    if isinstance(selected, dict):
        if selected.get('error'):
            st.warning(selected['error'])
        elif selected.get('result'):
            result = selected['result']
            tables.render_sequence(result['sequence'], label='Scénario recherché')
            st.metric('Probabilité conservée de ce scénario', _percent(result['probability']))
            st.caption('Cote cumulée indicative de ce scénario : ' + scenario_odds_text(result['sequence']))
    st.download_button('Exporter le catalogue complet (JSON compact)',
        data=lambda: _compact(method, descriptor, fixtures),
        file_name=f'catalogue_complet_{name}_{size}_matchs.json', mime='application/json',
        key=scope + '_json', icon=':material/download:', on_click='ignore',
        help='La distribution complète et son empreinte sont conservées sans énumérer toutes les combinaisons.')
    current_page = st.number_input('Page du classement complet', min_value=1, max_value=pages,
                                   step=1, key=page_key, on_change=_remember_page,
                                   args=(page_key, saved_page_key), persist_state='session',
                                   disabled=pages == 1)
    if type(current_page) is not int or not 1 <= current_page <= pages:
        st.warning('Le numéro de page est invalide.')
        return None
    with st.container(horizontal=True):
        st.button('Page précédente', key=scope + '_previous', disabled=current_page <= 1,
                  icon=':material/arrow_back:', on_click=_navigate, args=(page_key, saved_page_key, -1, pages))
        st.button('Page suivante', key=scope + '_next', disabled=current_page >= pages,
                  icon=':material/arrow_forward:', on_click=_navigate, args=(page_key, saved_page_key, 1, pages))
    offset = (current_page - 1) * page_size
    try:
        with st.spinner('Lecture du classement conservé…'):
            page = _page(method, offset, page_size)
        rows = page['ranking']
    except (ValueError, TypeError, KeyError, OverflowError, TimeoutError) as error:
        st.warning('Page non chargée : ' + str(error))
        st.caption('Aucun classement partiel n’est présenté comme complet. La recherche directe reste disponible ci-dessus.')
        return None
    _render_rows(rows, offset, observed, scope, size)
    total_text = f'{total:,}'.replace(',', ' ')
    st.caption(f'Scénarios {offset + 1}–{offset + len(rows)} sur {total_text} · '
               'Les rangs portent sur la distribution complète, pas uniquement sur cette page.')
    with st.container(horizontal=True):
        st.download_button('Exporter cette page (CSV)', lambda: _csv(rows, offset, observed),
            file_name=f'scenarios_{name}_page_{current_page}.csv', mime='text/csv',
            key=scope + '_csv', icon=':material/download:', on_click='ignore')
    for error in rejected:
        st.warning(error)
    return dict(method=name, total=total, offset=offset, visible=len(rows), page=current_page)
