"""Daily cross-competition dossiers, with frozen selection and result history."""
import json
from datetime import datetime
from math import isfinite

import streamlit as st

import api_football as api
import daily_analysis_data as data
import daily_analysis_worker as worker
import journal_performance as performance
import journal_tracking as tracking
import prediction_journal as journal
import scenario_presentation as presentation
import scenario_tables as tables
from journal_browser import _markdown
from ux_theme import render_page_header


COLORS = {'pending': 'blue', 'settled': 'gray', 'unresolved': 'orange', 'void': 'gray'}
PREPARATION = {'running': 'Calculs en cours', 'completed': 'Passage terminé', 'pending': 'File en cours de traitement',
               'paused': 'Reprise au prochain passage', 'waiting_history': 'Historique à compléter',
               'awaiting_preflight': 'Confirmation API attendue'}


def _now():
    return datetime.now(performance.LOCAL_ZONE)


def _names(row):
    return ' · '.join(data.competition_name(row, league) for league in
                      sorted({fixture['league'] for fixture in row['entry']['fixtures']}, key=int))


def _detail(row, clock_day):
    entry = row['entry']
    if st.button('Retour aux analyses', icon=':material/arrow_back:', key='daily_back'):
        st.session_state.pop('daily_selected', None)
        st.rerun()
    st.subheader(f"Analyse de {entry['size']} matchs", icon=':material/schema:')
    st.badge(row['status_label'], color=COLORS[row['status']])
    tables.render_outcome(row['scenario_outcome'], show_sequence=True)
    st.caption(_markdown(_names(row)))
    st.caption(f"Matchs du {row['day'].strftime('%d/%m/%Y')} · Enregistrée avant match le {entry['created_at']} · "
               f"Référence {entry['id'][:8]}")
    st.markdown('**Pourquoi cette analyse a été retenue**')
    if row['support'] is not None:
        st.metric('Appui des données à la sélection', f"{row['support']:.1f} / 100".replace('.', ','))
    st.caption('Cet indice décrit la couverture historique des rencontres. Il ne mesure pas un taux de réussite. '
               'Le classement utilise les informations conservées avant match, indépendamment des cotes et des résultats.')
    selection = row['selection']
    if selection.get('explanation') or selection.get('reason'):
        st.markdown(_markdown(selection.get('explanation') or selection['reason']))
    with st.expander('Appui historique conservé pour la sélection'):
        support = selection.get('support') or {}
        st.dataframe([{'Indicateur': label, 'Minimum dans le groupe': support.get(key, '—')}
                      for key, label in [('history_matches_min', 'Résultats par compétition'),
                                         ('history_days_min', 'Jours historiques UTC'),
                                         ('club_role_matches_min', 'Précédents dans le rôle de chaque club')]],
                     hide_index=True, width='stretch')
        st.caption(f"Coupure historique conservée : {selection.get('before', 'non conservée')}")
    st.caption('Les scénarios combinent les modèles propres aux compétitions, sous une hypothèse d’indépendance entre elles.')
    observed = {item['match_id']: item for item in entry.get('observed', [])}
    matches = []
    for position, fixture in enumerate(entry['fixtures'], 1):
        result = observed.get(fixture['match_id'], {})
        matches.append({'Ordre': position, 'Compétition': data.competition_name(row, fixture['league']),
                        'Coup d’envoi (Kinshasa)': journal._stamp(fixture['timestamp']).astimezone(
                            performance.LOCAL_ZONE).strftime('%d/%m/%Y %H:%M'),
                        'Domicile': fixture['home'], 'Extérieur': fixture['away'],
                        'Score à la pause': result.get('ht', '—'), 'Score à 90 minutes': result.get('ft', '—'),
                        'Résultat': result.get('value', '—')})
    presentation.render_match_results(matches, key='daily_match_results')
    if entry['status'] == 'pending':
        st.info(f"En attente : {performance.observed_count(entry)}/{entry['size']} résultats connus. "
                'L’analyse sera évaluée lorsque tous les scores seront disponibles.')
    for issue in entry.get('issues', []):
        st.warning(issue)
    tables.render_matrix(entry['methods'], actual=row['scenario_outcome'].get('actual'),
                         key=f'daily_matrix_{entry["id"]}', title='Les scénarios de cette analyse')
    if row['scenario_outcome']['state'] in ('hit', 'miss') and entry.get('evaluations'):
        st.subheader('Évaluation de l’analyse')
        ranks = {match['method']: match['rank'] for match in row['scenario_outcome']['matches']}
        evaluations = []
        for name in entry['methods']:
            saved = entry['evaluations'].get(name) or {}
            probability = saved.get('probability') if isinstance(saved, dict) else None
            valid = (not isinstance(probability, bool) and isinstance(probability, (int, float))
                     and isfinite(probability) and 0 <= probability <= 1)
            evaluations.append({'Méthode': tables.METHOD_LABELS.get(name, name),
                                'Premier choix exact': ranks.get(name) == 1,
                                'Scénario exact dans les 3 choix': name in ranks,
                                'Probabilité attribuée au scénario réalisé (%)': 100 * probability if valid else None})
        st.dataframe(evaluations, hide_index=True, width='stretch')
    unavailable = entry['metadata'].get('unavailable', {})
    if unavailable:
        with st.expander('Comprendre les méthodes indisponibles'):
            for name, reason in unavailable.items():
                st.caption(f"{tables.METHOD_LABELS.get(name, name)} : {reason}")
    if st.button('Actualiser les résultats de cette analyse', icon=':material/refresh:', key='daily_refresh'):
        if _now().date() != clock_day:
            st.rerun()
        try:
            tracking.refresh_entry(entry)
            st.rerun()
        except (journal.JournalError, api.ApiFootballError, ValueError) as error:
            st.error(str(error))
    with st.expander('Historique des évaluations et corrections'):
        st.json(entry.get('result_history', []), expanded=False)


@st.fragment(run_every=60)
def render_daily():
    today = _now().date()
    render_page_header('Analyses du jour',
                       'Des groupes de matchs sélectionnés au-delà des frontières des compétitions.',
                       eyebrow='GROUPES MULTIPLES · SCÉNARIOS FIGÉS · SUIVI DES RÉSULTATS', icon=':material/stars:')
    st.caption('API-Football · Heure de Kinshasa · Groupes de 2 à 20 matchs, plusieurs scénarios par analyse.')
    modes = {'today': 'Aujourd’hui', 'history': 'Historique'}
    mode = st.segmented_control('Consulter les analyses sélectionnées', list(modes), format_func=modes.get,
                                default='today', required=True, key='daily_view', persist_state='page')
    try:
        tracking.reconcile_local()
        entries = journal.list_entries()
        selections = worker.load_selections(api.DATA_DIR)
        status = worker.load_status(api.DATA_DIR)
        rows = data.project(entries, selections)
    except (journal.JournalError, api.ApiFootballError, ValueError, OSError) as error:
        st.error(str(error))
        return
    if _now().date() != today:
        st.rerun()
    day = today
    if mode == 'history':
        dates = sorted({row['day'] for row in rows if row['day'] < today}, reverse=True)
        if not dates:
            st.info('Aucune analyse sélectionnée n’a encore rejoint l’historique. Les prévisions du jour y resteront consultables.')
            st.caption('Les anciennes analyses par compétition restent dans Mes analyses → Historique.')
            return
        if st.session_state.get('daily_history_date') not in dates:
            st.session_state.pop('daily_history_date', None)
        day = st.selectbox('Journée des analyses', dates, format_func=lambda value: value.strftime('%d/%m/%Y'),
                            key='daily_history_date', persist_state='page')
    else:
        if st.button('Préparer les analyses du jour', icon=':material/auto_awesome:', key='daily_prepare'):
            try:
                with st.spinner('Actualisation du calendrier et préparation des analyses…'):
                    report = worker.run_now()
                st.session_state['daily_last_run'] = report
                st.rerun()
            except (ValueError, OSError, api.ApiFootballError) as error:
                st.error(str(error))
        report = st.session_state.get('daily_last_run') or status
        if report and report.get('day') == today.isoformat():
            st.caption(f"Dernière préparation : {report.get('created', 0)} analyse(s) enregistrée(s), "
                       f"{report.get('remaining', 0)} à traiter. {PREPARATION.get(report.get('status'), 'Préparation disponible')}.")
        with st.expander('Comment les analyses sont-elles sélectionnées ?'):
            st.markdown('Les rencontres doivent disposer de profils historiques exploitables dans leur propre compétition. '
                        'Les groupes sont classés selon un indice d’appui des données, calculé par match pour comparer les tailles. '
                        'Les tailles de 2 à 20 sont explorées par regroupements déterministes ; toutes les combinaisons possibles '
                        'ne sont pas énumérées. La précision de cette nouvelle sélection sera mesurée dans son suivi prospectif.')
            st.caption('La préparation automatique reprend à chaque cycle du service. Les limites de traitement étalent '
                       'les calculs, sans fixer de nombre maximal d’analyses retenues.')
            st.page_link('ui_pages/daily_evaluation.py', label='Consulter le bilan de la sélection', icon=':material/fact_check:')
    context = (today.isoformat(), mode, day.isoformat())
    if st.session_state.get('daily_context') != context:
        st.session_state['daily_context'] = context
        for key in list(st.session_state):
            if key in ('daily_selected', 'daily_preferences') or key.startswith(('daily_matrix_', 'daily_page_', 'daily_state', 'daily_verdict', 'daily_search')):
                st.session_state.pop(key, None)
    selected_rows = data.on_day(rows, day, today=today)
    st.caption(f"Matchs du {day.strftime('%d/%m/%Y')} · L’ordre repose sur l’appui des données figé lors de la sélection.")
    st.caption('« Réalisé » signifie que les matchs ont été évalués. La réussite des scénarios est indiquée séparément dès la liste, puis dans la fiche.')
    stats = st.columns(3)
    for column, label, value in zip(stats, ('Analyses sélectionnées', 'En attente', 'Réalisées'),
                                  (len(selected_rows), sum(row['status'] == 'pending' for row in selected_rows),
                                   sum(row['status'] == 'settled' for row in selected_rows))):
        column.metric(label, value)
    indexed = {row['id']: row for row in selected_rows}
    selected = st.session_state.get('daily_selected')
    if selected in indexed:
        _detail(indexed[selected], today)
    else:
        st.session_state.pop('daily_selected', None)
        # These widgets disappear while a detail fiche is open. Keep a
        # non-widget copy so returning to the list preserves its filters.
        for key, value in st.session_state.get('daily_preferences', {}).items():
            if key not in st.session_state:
                st.session_state[key] = value
        with st.container(horizontal=True):
            state = st.selectbox('État des analyses', [None, *data.STATE_LABELS],
                                 format_func=lambda value: 'Tous les états' if value is None else data.STATE_LABELS[value],
                                 key='daily_state', persist_state='page')
            verdict = st.selectbox('Réalisation des scénarios', list(tables.OUTCOME_FILTERS),
                                    format_func=tables.OUTCOME_FILTERS.get, key='daily_verdict', persist_state='page')
            search = st.text_input('Rechercher un club ou une compétition', key='daily_search', persist_state='page')
        st.session_state['daily_preferences'] = dict(daily_state=state, daily_verdict=verdict, daily_search=search)
        needle = search.casefold().strip()
        filtered = [row for row in selected_rows if (state is None or row['status'] == state) and
                    (verdict == 'all' or row['scenario_outcome']['state'] == verdict) and
                    (not needle or needle in (' '.join(f"{fixture['home']} {fixture['away']}" for fixture in row['entry']['fixtures'])
                                              + ' ' + _names(row)).casefold())]
        if not filtered:
            if selected_rows:
                st.info('Aucune analyse ne correspond à ces filtres.')
            elif mode == 'today' and status.get('day') == today.isoformat() and status.get('remaining', 0):
                st.info(f"{status['remaining']} groupe(s) attendent encore leur préparation ou leur confirmation API. "
                        'Les analyses apparaîtront ici après leur enregistrement avant match.')
            else:
                st.info('Aucune analyse enregistrée pour cette journée. La préparation nécessite des rencontres à venir et des profils historiques suffisants.')
        else:
            pages = (len(filtered) + 9) // 10
            page_key = f'daily_page_{day.isoformat()}_{state}_{verdict}_{search}'
            page = (st.selectbox('Page des analyses', range(pages), format_func=lambda value: f'{value+1} / {pages}', key=page_key)
                    if pages > 1 else 0)
            for row in filtered[page*10:(page+1)*10]:
                entry = row['entry']
                try:
                    count = len({tuple(item['sequence']) for method in entry['methods'].values() for item in method['ranking']})
                    scenarios_label = f'{count} scénarios distincts enregistrés'
                except (KeyError, TypeError, AttributeError):
                    scenarios_label = 'Classement à vérifier'
                with st.container(border=True):
                    left, right = st.columns([4, 1], vertical_alignment='center')
                    with left:
                        st.markdown(f"**{entry['size']} matchs · {_markdown(_names(row))}**")
                        st.badge(row['status_label'], color=COLORS[row['status']])
                        tables.render_outcome(row['scenario_outcome'])
                        st.caption(f"{len(entry['methods'])} méthode(s) · {scenarios_label} · "
                                   f"{performance.observed_count(entry)}/{entry['size']} résultats connus")
                        st.caption(' · '.join(_markdown(f"{fixture['home']} — {fixture['away']}") for fixture in entry['fixtures'][:2]) +
                                   (f" · +{entry['size']-2} matchs" if entry['size'] > 2 else ''))
                        st.caption('Appui des données avant match : ' + (f"{row['support']:.1f}/100".replace('.', ',') if row['support'] is not None else 'non conservé'))
                    with right:
                        if st.button('Voir l’analyse', icon=':material/arrow_forward:', key=f'daily_open_{row["id"]}', width='stretch'):
                            st.session_state['daily_selected'] = row['id']
                            st.rerun()
            st.caption(f'{len(filtered)} analyse(s) disponible(s) · Aucun plafond de sélection ; affichage par pages de 10.')
    st.download_button('Exporter les analyses sélectionnées', json.dumps([row['entry'] for row in rows], ensure_ascii=False, indent=2),
                       file_name='patternlab_analyses_du_jour.json', mime='application/json', key='daily_export')
