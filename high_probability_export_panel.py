"""Explicit multi-day exports of saved individual probabilities, without I/O."""
import streamlit as st

import high_probability_export as export
import prediction_journal as journal
from high_probability_history import VALUES, ZONE
from scenario_odds import DEFAULT_ODDS, format_odds


MODES = {'all': 'Tout ensemble', '1st': '1st', 'Égal': 'Égal', '2nd': '2nd'}
PREPARED = 'hp_archive_export_prepared'


def _set_days(days):
    st.session_state['hp_archive_export_days'] = list(days)
    st.session_state.pop(PREPARED, None)


def _source_signature(rows, now):
    return tuple((row['day'], row['id'], row['status'], row.get('checked_at'),
                  row.get('actual'), row.get('ht'), row.get('ft'),
                  journal._stamp(row['fixture']['timestamp']) <= now,
                  tuple(row['probabilities'].items()), str(row.get('baseline')),
                  str(row.get('result_explanation') or {})) for row in rows)


def _filename(days, mode):
    ordered = sorted(days)
    dates = ordered[0] if len(ordered) == 1 else f'{ordered[0]}_au_{ordered[-1]}_{len(ordered)}journees'
    kind = 'tous_resultats' if mode == 'all' else 'egal' if mode == 'Égal' else mode
    return f'fortes_probabilites_{dates}_{kind}'


def render_export(rows, *, default_day, now):
    days = sorted({row['day'] for row in rows}, reverse=True)
    key = 'hp_archive_export_days'
    if key not in st.session_state:
        st.session_state[key] = [default_day] if default_day in days else []
    else:
        selected = [day for day in st.session_state[key] if day in days]
        if selected != st.session_state[key]:
            st.session_state[key] = selected
    with st.container(border=True):
        st.markdown('**Exporter une ou plusieurs journées**')
        st.caption('Toutes les pages et compétitions des journées choisies sont incluses. '
                   'Seuls les réglages de ce bloc s’appliquent à l’export.')
        with st.container(horizontal=True):
            st.button('Toutes les journées', key='hp_archive_export_all_days',
                      on_click=_set_days, args=(days,))
            st.button('Effacer les journées', key='hp_archive_export_clear_days',
                      on_click=_set_days, args=([],))
        selected_days = st.multiselect('Journées à exporter', days, key=key,
            format_func=lambda value: '/'.join(reversed(value.split('-'))),
            placeholder='Choisir une ou plusieurs journées')
        mode = st.segmented_control('Résultats à exporter', list(MODES), format_func=MODES.get,
            default='all', required=True, key='hp_archive_export_outcomes')
        minimum = st.number_input('Probabilité minimale à exporter (%)', min_value=0, max_value=100,
            value=0, step=1, key='hp_archive_export_minimum')
        st.caption('Cotes de référence fixes renseignées manuellement : '+
            ' · '.join(f'{label} : {format_odds(DEFAULT_ODDS[label])}' for label in VALUES)+'.')
        st.caption('Une ligne par match et catégorie choisie. Avec « Tout ensemble », chaque catégorie '
                   'est testée séparément contre le seuil ; un seuil de 0 % conserve les trois.')
        options = (tuple(sorted(selected_days)), mode, minimum, tuple(DEFAULT_ODDS.items()))
        signature = _source_signature([row for row in rows if row['day'] in selected_days], now)
        prepared = st.session_state.get(PREPARED)
        if prepared and (prepared['options'] != options or prepared['source_signature'] != signature):
            st.session_state.pop(PREPARED, None)
            prepared = None
        if st.button('Préparer l’export', icon=':material/download:', type='primary',
                     key='hp_archive_export_prepare', disabled=not selected_days):
            outcomes = list(VALUES) if mode == 'all' else [mode]
            try:
                values = export.build(rows, days=selected_days, outcomes=outcomes,
                                      minimum=minimum/100, now=now)
                prepared = dict(options=options, source_signature=signature, rows=values,
                    csv=export.to_csv(values), json=export.to_json(values, days=selected_days, outcomes=outcomes),
                    filename=_filename(selected_days, mode), prepared_at=now.astimezone(ZONE).strftime('%d/%m/%Y à %H:%M'))
                st.session_state[PREPARED] = prepared
            except (ValueError, TypeError, KeyError) as error:
                st.session_state.pop(PREPARED, None)
                prepared = None
                st.error(f'Export indisponible : {error}')
        if not selected_days:
            st.info('Choisis au moins une journée pour préparer l’export.')
        if not prepared:
            return
        count = len(prepared['rows'])
        if not count:
            st.info('Aucune probabilité ne correspond aux journées, catégories et seuil choisis.')
        else:
            st.caption(f"Export préparé le {prepared['prepared_at']} (Kinshasa) · "
                       f"{len(selected_days)} journée(s) · {MODES[mode]} · {count} ligne(s).")
        with st.container(horizontal=True):
            st.download_button('Télécharger le CSV (Excel)', data=prepared['csv'],
                file_name=prepared['filename']+'.csv', mime='text/csv', on_click='ignore',
                disabled=not count, key='hp_archive_export_csv')
            st.download_button('Télécharger le JSON', data=prepared['json'],
                file_name=prepared['filename']+'.json', mime='application/json', on_click='ignore',
                disabled=not count, key='hp_archive_export_json')
