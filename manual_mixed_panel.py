"""Manual selection across competitions; explicit local forecast and immutable save."""
from copy import deepcopy
from datetime import datetime, timezone
import json

import pandas as pd
import streamlit as st

import admin_access
from analysis_date import get_selected_day
import calendar_data
import calendar_panel as calendar
import manual_analysis_store as archive
import manual_mixed_analysis as analysis
import manual_match_coverage as match_coverage
import manual_proposal_archive as proposals
import scenario_tables
import table_views
import workspace_sources as sources


FIELDS = ('match_id', 'league', 'home', 'away', 'timestamp', 'provider', 'home_id', 'away_id', 'season')
DATA_FILTERS = {'all': 'Tous les matchs', 'eligible': 'Exploitables pour Clubs',
                'unavailable': 'Non exploitables pour Clubs'}


def _now():
    return datetime.now(timezone.utc)


def _draft_key(source, day):
    return f'mixed_manual_draft_{source}_{day.isoformat()}'


def _identity(row):
    return tuple(str(row.get(name, '')) for name in ('match_id', 'league', 'provider', 'home_id', 'away_id'))


def _stamp(row):
    value = datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00'))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _resolve(draft, current):
    """Retain absent or changed identities, requiring a fresh explicit choice."""
    result, missing = [], []
    for mid, saved in draft.items():
        latest = current.get(mid)
        if latest is None or _identity(latest) != _identity(saved):
            result.append(deepcopy(saved)); missing.append(mid)
        else:
            result.append(deepcopy(latest))
    return sorted(result, key=lambda row: (_stamp(row), int(row['match_id']))), missing


def _future(row, day, now):
    return bool(row.get('is_upcoming')) and _stamp(row) > now and _stamp(row).astimezone(calendar.ZONE).date() == day


@st.cache_data(ttl=60, max_entries=8, show_spinner=False)
def _history(signature, source, leagues):
    provider = sources._provider(source)
    return provider.load_matches(provider.STORE_PATH, leagues=list(leagues))[0]


def _competition(info, league):
    value = info.get(str(league), {})
    return ' · '.join(filter(None, (value.get('country'), value.get('name')))) or str(league)


@st.cache_data(ttl=60, max_entries=8, show_spinner=False)
def _coverage(signature, source, candidates, group, decision_day):
    """Read only the needed histories; never infer, collect or save a forecast.

    History eligibility changes with UTC dates, fixtures, the current group or
    the store revision. The exact current clock still gates selectable rows in
    render() and on add; this cache only describes prior-day data availability.
    """
    leagues = tuple(sorted({row['league'] for row in (*candidates, *group)}, key=int))
    rows = _history(signature, source, leagues) if leagues else []
    decision = datetime.fromisoformat(decision_day).replace(tzinfo=timezone.utc)
    return match_coverage.coverage(rows, candidates, decision_at=decision, group_fixtures=group)


def _coverage_columns(value):
    return {'Exploitable pour Clubs': 'Oui' if value.get('eligible') else 'Non',
            'Base du calcul': ('Profils des clubs' if value.get('personalized') else
                              'Tendance de la compétition') if value.get('eligible') else 'Indisponible',
            'Résultats exploitables': value.get('training_matches'),
            'Journées historiques UTC': value.get('training_days'),
            'Historique avant (UTC)': value.get('before'),
            'Motif des données': value.get('reason') or 'Contrôle des données indisponible.'}


def _unavailable_group(payload):
    """Name every affected match even when the full Clubs group is unavailable."""
    support = {str(item['league']): item for item in payload.get('competition_support', [])}
    rows = []
    for index, fixture in enumerate(payload['fixtures'], 1):
        item = support.get(str(fixture['league']), {})
        if item.get('eligible'):
            continue
        rows.append({'Ordre': index, 'Match': f"{fixture['home']} — {fixture['away']}",
                     'Compétition': _competition(payload.get('competitions', {}), fixture['league']),
                     'ID compétition': fixture['league'],
                     'Résultats exploitables': item.get('training_matches'),
                     'Journées historiques UTC': item.get('training_days'),
                     'Historique avant (UTC)': item.get('before')})
    if rows:
        st.caption('Matchs concernés par le manque de données :')
        table_views.dataframe(rows, hide_index=True, width='stretch',
                             table_id='manual_mixed_panel:unavailable_group')


def _same(payload, day, source, chosen):
    return bool(payload and payload.get('day') == day.isoformat() and payload.get('source') == source
                and {_identity(row) for row in payload['fixtures']} == {_identity(row) for row in chosen})


def _show(payload):
    st.subheader('Les scénarios de mon groupe', icon=':material/schema:')
    if not payload['forecast'].get('eligible'):
        st.warning(payload['forecast'].get('diagnostic') or 'Historique insuffisant pour ce groupe.')
        _unavailable_group(payload)
        return
    if payload.get('save_error'):
        st.warning('Analyse calculée, mais non sauvegardée : ' + payload['save_error'])
    else:
        saved = payload['saved']
        coverage = proposals.coverage(saved)
        if coverage.get('catalog_complete'):
            st.success((f"Analyse sauvegardée dans Mes analyses · {coverage['catalog_total']:,} scénarios possibles "
                       'conservés avec leurs probabilités, consultables par pages.').replace(',', ' '))
        else:
            st.success(f"Analyse sauvegardée dans Mes analyses · {coverage['proposal_count']} scénario(s) distinct(s).")
        st.page_link('ui_pages/journal.py', label='Voir cette analyse sauvegardée', icon=':material/history:',
                     query_params={'manual_analysis': saved['id']})
    st.caption('Moteur choisi : profils des clubs, avec l’historique propre à chaque compétition. '
               'La combinaison suppose l’indépendance des matchs ; elle n’apprend pas de dépendance entre compétitions.')
    st.caption('Chaque scénario suit l’ordre du groupe enregistré. Son pourcentage concerne tous les matchs ensemble. '
               f"Ce groupe possède {3 ** len(payload['fixtures']):,} possibilités théoriques.".replace(',', ' '))
    scenario_tables.render_matrix(payload['comparison_methods'], key='mixed_manual_scenarios',
                                  title='Scénarios possibles et probabilités par méthode')
    for method, reason in payload.get('unavailable', {}).items():
        st.caption(f"{scenario_tables.METHOD_LABELS.get(method, method)} : {reason}")
    positions = payload['forecast'].get('club_estimates', [])
    if positions:
        rows = []
        for index, (fixture, estimate) in enumerate(zip(payload['fixtures'], positions), 1):
            probabilities = estimate.get('probabilities', {})
            rows.append({'Ordre': index, 'Compétition': _competition(payload['competitions'], fixture['league']),
                         'Match': f"{fixture['home']} — {fixture['away']}",
                         **{name + ' (%)': 100 * probabilities.get(name, 0) for name in ('1st', 'Égal', '2nd')},
                         'Base du calcul': 'Profils des clubs' if estimate.get('personalized') else 'Tendance de la compétition',
                         'Précédents domicile': estimate.get('home_profile', {}).get('matches', 0),
                         'Précédents extérieur': estimate.get('away_profile', {}).get('matches', 0)})
        st.subheader('Probabilités par match')
        table_views.dataframe(rows, table_id='manual_mixed_panel:match_probabilities', hide_index=True,
            width='stretch', column_config={name+' (%)': st.column_config.NumberColumn(format='%.2f')
                                           for name in ('1st','Égal','2nd')})
        if any(not estimate.get('personalized') for estimate in positions):
            st.caption('Quand les précédents des clubs sont insuffisants, le moteur utilise la tendance '
                       'de leur compétition. La colonne « Base du calcul » identifie ces matchs.')
    st.download_button('Exporter cette analyse (JSON)', json.dumps(
        payload.get('saved') or {key: value for key, value in payload.items() if key != 'save_error'},
        ensure_ascii=False, indent=2), file_name=f"patternlab_groupe_{payload['day']}.json",
        mime='application/json', key='mixed_manual_download')


@st.fragment
def render(source):
    import scenario_workspace_panel as workspace
    now = _now()
    day = get_selected_day(now.astimezone(calendar.ZONE).date())
    key = _draft_key(source, day)
    st.caption('Cochez les matchs du tableau, puis ajoutez-les à votre groupe. '
               'Les filtres et les changements de compétition conservent les matchs déjà ajoutés.')
    if st.button('Actualiser le calendrier', icon=':material/refresh:', key='mixed_manual_refresh',
                 disabled=not admin_access.can_admin()):
        try:
            admin_access.require_admin()
            sources.refresh_day(source, day)
        except sources.ERRORS as error:
            st.warning('Le calendrier local reste disponible : ' + str(error))
    try:
        snapshot = workspace._day_snapshot(workspace._snapshot(day, workspace._signature(), source), day)
    except sources.ERRORS as error:
        st.error(str(error)); return
    competitions = snapshot['competitions']
    fixtures = {row['match_id']: row for row in snapshot['fixtures']}
    display = calendar.display_rows(snapshot, day)
    draft = dict(st.session_state.get(key, {}))
    chosen, missing = _resolve(draft, fixtures)
    filter_key = f'mixed_manual_leagues_{source}_{day}'
    options = sorted(competitions, key=lambda code: (_competition(competitions, code), int(code)))
    if filter_key in st.session_state:
        st.session_state[filter_key] = [code for code in st.session_state[filter_key] if code in options]
    selected_leagues = st.multiselect('Filtrer les compétitions', options, format_func=lambda code: _competition(competitions, code),
                                     key=filter_key, persist_state='session', placeholder='Toutes les compétitions')
    status = st.segmented_control('Matchs à consulter', ['À venir','Tous','En cours','Terminés','Autres'],
        default='À venir', required=True, key='mixed_manual_status', persist_state='session')
    query = st.text_input('Rechercher un match, un pays ou une compétition', type='search', key='mixed_manual_query',
                          persist_state='session')
    data_filter = st.selectbox('Disponibilité des données', list(DATA_FILTERS), format_func=DATA_FILTERS.get,
        key='mixed_manual_data_filter', persist_state='session',
        help='Exploitables pour Clubs : les minimums existants de 30 résultats de compétition sur 3 dates UTC '
             'antérieures sont atteints. Cela indique la possibilité de calculer, sans garantir la précision.')
    filtered = calendar_data.filter_rows(display, None, status, query)
    if selected_leagues:
        filtered = [row for row in filtered if row['league'] in selected_leagues]
    if status == 'À venir':
        candidates = filtered
        filtered = [row for row in candidates if row['match_id'] in fixtures
                    and _future(fixtures[row['match_id']], day, now)]
        if len(filtered) < len(candidates):
            st.caption(f'{len(candidates)-len(filtered)} rencontre(s) annoncée(s) à venir dans le relevé '
                       'ont un horaire passé ou des identités non confirmées. Elles restent consultables dans « Tous ».')
    coverage = {}
    if data_filter != 'all':
        candidates = [fixtures[row['match_id']] for row in filtered
                      if row['match_id'] in fixtures and _future(fixtures[row['match_id']], day, now)]
        group = [row for row in chosen if row['match_id'] not in missing and _future(row, day, now)]
        coverage_fixtures = {row['match_id']: row for row in (*candidates, *group)}
        try:
            with st.spinner('Vérification de l’historique local…'):
                coverage = _coverage(workspace._signature(), source,
                    [{name: row[name] for name in FIELDS if name in row} for row in coverage_fixtures.values()],
                    [{name: row[name] for name in FIELDS if name in row} for row in group],
                    now.date().isoformat())
        except (sources.ERRORS + (ValueError, OSError)):
            coverage = {row['match_id']: dict(eligible=False, invalid=True,
                reason='Contrôle de l’historique local indisponible.') for row in coverage_fixtures.values()}
            st.warning('La disponibilité des données ne peut pas être vérifiée pour le moment.')
        eligible = sum(bool(coverage.get(row['match_id'], {}).get('eligible')) for row in candidates)
        st.caption(f'{eligible} match(s) exploitable(s) pour Clubs · '
                   f'{len(candidates)-eligible} non exploitable(s) parmi les rencontres encore à venir de ces filtres. '
                   'Les résultats utilisables précèdent la coupure UTC du groupe.')
        wanted = data_filter == 'eligible'
        candidate_ids = {row['match_id'] for row in candidates}
        filtered = [row for row in filtered if row['match_id'] in candidate_ids
                    and bool(coverage.get(row['match_id'], {}).get('eligible')) == wanted]
    st.caption(f'{len(filtered)} matchs affichés sur {snapshot["day_count"]} à la date choisie · '
               'Seuls les matchs encore à venir avec leurs clubs confirmés peuvent être ajoutés.')
    selected_ids = []
    if filtered:
        frame = pd.DataFrame([{'ID match': row['match_id'], **calendar.table_rows([row])[0],
                              **(_coverage_columns(coverage.get(row['match_id'], {})) if data_filter != 'all' else {})}
                              for row in filtered])
        displayed, event = table_views.selectable_dataframe(frame, table_id='manual_mixed_panel:calendar_selection',
            key=f'mixed_manual_calendar_{source}_{day}_{st.session_state.get("mixed_manual_selection_revision", 0)}',
            hide_index=True, width='stretch', height=450,
            column_config={'ID match': None, 'Verdict final': calendar.verdict_column()})
        selected_ids = [str(displayed.iloc[index]['ID match']) for index in event.selection.rows
                        if type(index) is int and 0 <= index < len(displayed)]
    else:
        st.info('Aucun match ne correspond à ces filtres dans le calendrier local.')
    if st.button('Ajouter les matchs cochés à mon groupe', icon=':material/add:', key='mixed_manual_add',
                 disabled=not selected_ids):
        rejected = []
        for mid in selected_ids:
            row = fixtures.get(mid)
            if row is None or not _future(row, day, _now()):
                rejected.append(mid); continue
            draft[mid] = {name: row[name] for name in (*FIELDS, 'is_upcoming', 'status') if name in row}
        st.session_state[key] = draft
        st.session_state['mixed_manual_selection_revision'] = st.session_state.get('mixed_manual_selection_revision', 0)+1
        if rejected:
            st.session_state['mixed_manual_notice'] = 'Certaines lignes ne sont plus sélectionnables : actualisez leur horaire ou leur statut.'
        st.rerun(scope='fragment')
    notice = st.session_state.pop('mixed_manual_notice', None)
    if notice: st.warning(notice)
    chosen, missing = _resolve(st.session_state.get(key, {}), fixtures)
    st.subheader(f'Mon groupe · {len(chosen)} matchs', icon=':material/checklist:')
    if chosen:
        rows = [{'Ordre': index, 'Heure (Kinshasa)': _stamp(row).astimezone(calendar.ZONE).strftime('%H:%M'),
                 'Compétition': _competition(competitions, row['league']),
                 'Domicile': row['home'], 'Extérieur': row['away'],
                 'État': 'À revérifier' if row['match_id'] in missing else
                         'À venir' if _future(row, day, now) else 'Coup d’envoi passé ou statut modifié',
                 **(_coverage_columns(coverage[row['match_id']]) if row['match_id'] in coverage else {})}
                for index, row in enumerate(chosen, 1)]
        table_views.dataframe(rows, table_id='manual_mixed_panel:chosen_group', hide_index=True, width='stretch')
        remove_key = f'mixed_manual_remove_{day}'
        remove_options = [row['match_id'] for row in chosen]
        if remove_key in st.session_state:
            st.session_state[remove_key] = [mid for mid in st.session_state[remove_key] if mid in remove_options]
        remove = st.multiselect('Matchs à retirer du groupe', remove_options,
            format_func=lambda mid: next(f"{row['home']} — {row['away']}" for row in chosen if row['match_id'] == mid),
            key=remove_key)
        if st.button('Retirer les matchs indiqués', key='mixed_manual_remove_action', disabled=not remove):
            st.session_state[key] = {mid: row for mid, row in st.session_state.get(key, {}).items() if mid not in remove}
            st.rerun(scope='fragment')
    else:
        st.info('Ajoutez au moins deux matchs, dans une ou plusieurs compétitions.')
    ready = 2 <= len(chosen) <= 20 and not missing and all(_future(row, day, now) for row in chosen)
    if len(chosen) > 20:
        st.warning('Choisissez de 2 à 20 matchs pour le calcul de ce groupe.')
    if chosen and (missing or any(not _future(row, day, now) for row in chosen)):
        st.warning('La sélection est conservée. Un horaire, un statut ou une identité doit être revérifié avant un nouveau calcul.')
    if st.button('Analyser mon groupe', type='primary', icon=':material/schema:', key='mixed_manual_analyze',
                 disabled=not ready):
        try:
            workspace._require_today(day, chosen)
            leagues = tuple(sorted({row['league'] for row in chosen}, key=int))
            rows = _history(workspace._signature(), source, leagues)
            fixtures_to_predict = [{name: row[name] for name in FIELDS if name in row} for row in chosen]
            with st.spinner('Analyse des clubs dans leurs compétitions et construction des scénarios…'):
                payload = analysis.build(rows, fixtures_to_predict, source, decision_at=_now())
            workspace._require_today(day, chosen)
            payload.update(day=day.isoformat(), source=source, competitions=deepcopy(competitions))
            payload['competition'] = {'name': ' · '.join(_competition(competitions, code) for code in leagues),
                'country':'Compétitions multiples' if len(leagues)>1 else competitions.get(leagues[0],{}).get('country','Pays non précisé'),
                'competitions': {code: deepcopy(competitions.get(code, {})) for code in leagues}}
            try:
                if payload['forecast'].get('eligible'):
                    archive_input = {name: payload[name] for name in ('source', 'engine', 'fixtures', 'forecast',
                        'comparison_methods', 'history_cutoff_exclusive', 'model_generation', 'competition')}
                    payload['saved'] = archive.record(archive_input, require_complete=True)
                    payload['comparison_methods'] = deepcopy(payload['saved']['comparison_methods'])
                    payload['forecast'] = deepcopy(payload['saved']['forecast'])
            except (archive.ManualAnalysisError, OSError) as error:
                payload['save_error'] = str(error)
            st.session_state['mixed_manual_forecast'] = payload
        except (sources.ERRORS + (ValueError, OSError)) as error:
            st.error(str(error))
    previous = st.session_state.get('mixed_manual_forecast')
    if _same(previous, day, source, chosen):
        _show(previous)
    elif previous and previous.get('day') == day.isoformat():
        st.caption('Votre groupe a changé. Lancez son analyse pour obtenir de nouveaux scénarios ; '
                   'les calculs déjà sauvegardés restent dans Mes analyses.')
