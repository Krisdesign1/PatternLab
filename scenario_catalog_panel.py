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
import scenario_tables as tables
import table_views


PAGE_SIZE = 50
PAGE_SECONDS = 5.0
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


@st.cache_data(show_spinner=False, max_entries=64)
def _page(method, offset, page_size):
    started = monotonic()

    def check_time():
        if monotonic() - started > PAGE_SECONDS:
            raise TimeoutError('Le calcul de cette page demande davantage de temps. '
                               'Consultez une page plus proche ou recherchez directement un scénario précis.')

    return catalog.page(method, offset=offset, page_size=page_size, check_time=check_time)


def _remember_page(key, saved_key):
    value = st.session_state.get(key, 1)
    st.session_state[saved_key] = value if type(value) is int else 1


def _navigate(key, saved_key, amount, pages):
    value = st.session_state.get(key, 1)
    value = value if type(value) is int else 1
    st.session_state[key] = max(1, min(pages, value + amount))
    _remember_page(key, saved_key)


def _csv(rows, offset, actual):
    buffer = StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=['Rang', 'Scénario', 'Probabilité (%)', 'Scénario observé'])
    writer.writeheader()
    for number, row in enumerate(rows, offset + 1):
        writer.writerow({'Rang': number, 'Scénario': ' → '.join(row['sequence']),
                         'Probabilité (%)': row['probability'] * 100,
                         'Scénario observé': ('Oui' if list(row['sequence']) == actual else 'Non')
                         if actual is not None else ''})
    return buffer.getvalue().encode('utf-8-sig')


def _compact(method, descriptor, fixtures):
    value = dict(scope='complete_frozen_distribution', catalogue=deepcopy(descriptor),
                 method=deepcopy(method), note='Catalogue des possibilités, distinct des propositions historiques.')
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
    base = str(key) + '_catalog'
    method_key = base + '_method'
    if st.session_state.get(method_key) not in available:
        st.session_state[method_key] = next(iter(available))
    name = st.selectbox('Méthode du classement complet', list(available),
                        format_func=lambda value: tables.METHOD_LABELS.get(value, value), key=method_key)
    if name not in available:
        st.warning('Choisissez une méthode disponible.')
        return None
    method, descriptor = available[name], descriptors[name]
    size, total = descriptor['size'], descriptor['total']
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    identity = descriptor.get('sha256') or sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest()
    scope = f'{base}_{name}_{identity[:16]}'
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
    st.caption(f'{size} matchs · {PAGE_SIZE} lignes par page · {pages_text} page(s). '
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
    st.download_button('Exporter le catalogue complet (JSON compact)',
        data=lambda: _compact(method, descriptor, fixtures),
        file_name=f'catalogue_complet_{name}_{size}_matchs.json', mime='application/json',
        key=scope + '_json', icon=':material/download:', on_click='ignore',
        help='La distribution complète et son empreinte sont conservées sans énumérer toutes les combinaisons.')
    current_page = st.number_input('Page du classement complet', min_value=1, max_value=pages,
                                   step=1, key=page_key, on_change=_remember_page,
                                   args=(page_key, saved_page_key))
    if type(current_page) is not int or not 1 <= current_page <= pages:
        st.warning('Le numéro de page est invalide.')
        return None
    with st.container(horizontal=True):
        st.button('Page précédente', key=scope + '_previous', disabled=current_page <= 1,
                  icon=':material/arrow_back:', on_click=_navigate, args=(page_key, saved_page_key, -1, pages))
        st.button('Page suivante', key=scope + '_next', disabled=current_page >= pages,
                  icon=':material/arrow_forward:', on_click=_navigate, args=(page_key, saved_page_key, 1, pages))
    offset = (current_page - 1) * PAGE_SIZE
    try:
        with st.spinner('Lecture du classement conservé…'):
            page = _page(method, offset, PAGE_SIZE)
        rows = page['ranking']
    except (ValueError, TypeError, KeyError, OverflowError, TimeoutError) as error:
        st.warning('Page non chargée : ' + str(error))
        st.caption('Aucun classement partiel n’est présenté comme complet. La recherche directe reste disponible ci-dessus.')
        return None
    display = []
    for rank, row in enumerate(rows, offset + 1):
        item = {'Rang': rank, 'Scénario complet': _colored_sequence(row['sequence']),
                'Probabilité du groupe (%)': _percent(row['probability'])}
        if observed is not None:
            item['Résultat final'] = ':green-badge[Scénario observé]' if list(row['sequence']) == observed else ''
        display.append(item)
    table_views.table(display, hide_index=True, width='stretch',
                      table_id='scenario_catalog_panel:render:table:1', key=scope + '_table')
    total_text = f'{total:,}'.replace(',', ' ')
    st.caption(f'Scénarios {offset + 1}–{offset + len(rows)} sur {total_text} · '
               'Les rangs portent sur la distribution complète, pas uniquement sur cette page.')
    with st.container(horizontal=True):
        st.download_button('Exporter cette page (CSV)', _csv(rows, offset, observed),
            file_name=f'scenarios_{name}_page_{current_page}.csv', mime='text/csv',
            key=scope + '_csv', icon=':material/download:', on_click='ignore')
    for error in rejected:
        st.warning(error)
    return dict(method=name, total=total, offset=offset, visible=len(rows), page=current_page)
