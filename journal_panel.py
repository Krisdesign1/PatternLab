"""Prospective results, never filled with retrospective example predictions."""
import table_views
import json
from datetime import datetime

import streamlit as st
from adaptive_scenario_proposals import effective_methods
from analysis_date import get_selected_day
import admin_access
from cloud_ui import guarded

import api_football
import journal_performance as performance
import journal_evidence_panel as evidence
import journal_tracking as tracking
import prediction_journal as journal
import journal_browser
import journal_browser_data
from ux_theme import render_page_header
import scenario_tables
import scenario_outcomes
import scenario_presentation as presentation
import result_explanations
import manual_analysis_panel
import manual_analysis_store as manual_archive
import manual_journal_rows
from result_explanations_panel import render_explanation

NAMES = {'frequency': 'Répétitions seules', 'clubs': 'Clubs seuls',
         'combined': 'Clubs + répétitions', 'catboost': 'CatBoost',
         'context_neighbors': 'Répétitions par contexte'}
STATUSES = {'pending': 'En attente', 'settled': 'Évalué', 'unresolved': 'À vérifier', 'void': 'Annulé du suivi'}
ORIGINS = {'all': 'Toutes les prévisions', 'manual': 'Sélection manuelle', 'automatic': 'Regroupement automatique'}


def _now():
    return datetime.now(performance.LOCAL_ZONE)


def _today():
    return _now().date()


def _export(entries):
    # Serialization can exceed the visible day's work by tens of megabytes.
    # Keep the same captured journal, but create its file only on request.
    st.download_button('Exporter les prévisions confirmées (JSON)', lambda: json.dumps(entries, ensure_ascii=False, indent=2),
                       file_name='patternlab_journal_api_football.json', mime='application/json',
                       icon=':material/download:', key='journal_export', on_click='ignore')


def _export_manual(entries, day):
    if entries:
        import manual_proposal_archive
        incomplete = sum(not manual_proposal_archive.coverage(entry)['complete'] for entry in entries)
        if incomplete:
            st.caption(f'{incomplete} archive(s) manuelle(s) de cette journée ont une comparaison initiale incomplète. '
                       'Le fichier conserve leurs données originales ; elles ne doivent pas entrer dans un taux '
                       'de réussite portant sur toutes les propositions initialement affichées.')
        st.download_button('Exporter les analyses manuelles de cette journée (JSON)',
            lambda: json.dumps(entries, ensure_ascii=False, indent=2),
            file_name=f'patternlab_analyses_manuelles_{day.isoformat()}.json', mime='application/json',
            icon=':material/download:', key='journal_manual_export', on_click='ignore')


def _comparison(scoped, league, size, selected_origin, focus_entry=None):
    st.subheader('Comparer les résultats réels', icon=':material/compare:')
    available = [name for name in NAMES if any(name in entry['methods'] for entry in scoped)]
    chosen = st.multiselect('Méthodes à comparer', available,
                            default=[m for m in ('frequency', 'clubs', 'combined') if m in available] or available,
                            format_func=lambda code: NAMES[code], key=f'journal_methods_{league}_{size}')
    versions = performance.signatures(scoped, chosen)
    selected = None
    if len(versions) > 1:
        preferred = performance.signature(focus_entry, chosen) if focus_entry and set(chosen).issubset(focus_entry['methods']) else None
        selected = st.selectbox('Versions comparées', versions,
            index=versions.index(preferred) if preferred in versions else 0,
            format_func=lambda value: ' · '.join(f'{NAMES[n]} : {g[:12] or v}' for n,v,g in value),
            key=f'journal_versions_{league}_{size}_{selected_origin}_{"_".join(sorted(chosen))}_{focus_entry["id"] if focus_entry else "all"}')
    result = performance.comparison(scoped, chosen, selected)
    if not chosen:
        st.info('Choisissez au moins une méthode pour consulter ses résultats avant match.')
        return
    if focus_entry and focus_entry['id'] not in {entry['id'] for entry in result['entries']}:
        st.info('L’analyse ouverte ne fait pas partie de cette cohorte avec les méthodes, versions et origine choisies. '
                'Sa prévision reste consultable dans sa fiche.')
    current = result['counts']
    st.caption(f"Cohorte commune : {current['total']} groupes avec les mêmes méthodes et versions, "
               f"dont {len(result['settled'])} évalués sur les scores à 90 minutes.")
    if result['missing_methods'] or result['other_versions']:
        st.caption(f"Exclus de cette comparaison : {result['missing_methods']} groupe(s) sans toutes les méthodes "
                   f"choisies et {result['other_versions']} avec d’autres versions. Leurs résultats ne sont pas comptés comme des échecs.")
    if result['summary']:
        st.caption('Ces résultats décrivent uniquement les prévisions évaluées de cette cohorte. '
                   'Le nombre de groupes communs permet d’en apprécier le recul disponible.')
        if len(result['settled']) == 1:
            st.info('Une seule prévision évaluée dans cette cohorte : ce résultat ne permet pas encore '
                    'de juger la précision du modèle sur plusieurs rencontres.')
        table_views.dataframe([{'Méthode': NAMES[row['method']], 'Version': row['generation'][:12] or row['version'],
                       'Groupes communs': row['tested'], 'Exact en premier': row['top1'],
                       'Réussite en premier (%)': 100*row['top1_rate'],
                       'Exact dans les 3 choix': row['top3'], 'Réussite dans les 3 (%)': 100*row['top3_rate'],
                       'Erreur probabiliste moyenne ↓': row['loss']} for row in result['summary']],
                     hide_index=True, width='stretch', table_id='journal_panel:_comparison:dataframe:1')
        if current['pending'] or current['unresolved']:
            st.caption(f"{current['pending']} groupe(s) attendent encore leurs résultats ; "
                       f"{current['unresolved']} nécessitent une vérification et restent exclus des taux.")
    elif current['pending']:
        st.info(f"Résultats en attente : {current['pending']} groupe(s) communs ne sont pas encore terminés. "
                'Aucun taux de réussite n’est calculé pour le moment.')
    else:
        st.info('Aucun groupe terminé et évaluable pour toutes ces méthodes et versions. '
                'Les groupes annulés ou à vérifier sont exclus des taux de réussite.')
    st.caption('Une réussite exige le scénario complet dans le bon ordre. « En premier » concerne le premier choix ; '
               '« dans les 3 » concerne l’un des trois scénarios figés. L’erreur probabiliste utilise la probabilité '
               'du scénario réalisé dans la distribution complète ; plus elle est faible, mieux c’est.')
    st.caption('Les méthodes sont comparées sur les mêmes groupes, dans une seule compétition et une seule taille. '
               'Les sélections manuelles ou automatiques peuvent partager des matchs : ces observations ne sont pas indépendantes.')
    evidence.render(result['entries'], chosen)
    if result['trend']:
        with st.expander('Évolution sur les mêmes prévisions'):
            chart = [{'Date des prévisions UTC': row['day'], 'Méthode': NAMES[row['method']],
                      'Réussite exacte dans les 3 (%)': 100*row['top3_rate'],
                      'Groupes communs cumulés': row['tested'], 'Erreur probabiliste cumulée': row['loss']}
                     for row in result['trend']]
            st.line_chart(chart, x='Date des prévisions UTC', y='Réussite exacte dans les 3 (%)', color='Méthode')
            st.caption('Cumul des seuls groupes actuellement évalués de cette cohorte, par date de prévision. '
                       'Les points sont recalculés lorsque des résultats arrivent ou sont corrigés ; la courbe ne mesure pas une certitude.')
            table_views.dataframe(chart, hide_index=True, width='stretch', table_id='journal_panel:_comparison:dataframe:2')


def _clear_analysis_link():
    st.query_params.pop('analysis', None)


def _detail(entry, day, daily, *, clock_day):
    outcome = scenario_outcomes.summarize(entry)
    scenario_tables.render_archive_scope(entry)
    competition = entry['metadata'].get('competition', entry['league'])
    display_competition = journal_browser._markdown(competition)
    st.subheader(f"{display_competition} · {entry['size']} matchs", icon=':material/schema:')
    st.caption(f"Matchs du {day.strftime('%d/%m/%Y')} · {journal_browser._markdown(entry['metadata'].get('country', ''))} · "
               f"Référence {entry['id'][:8]}")
    st.caption(f"Enregistré avant match : {entry['created_at']} · Révision des résultats : {entry.get('revision', 0)}")
    st.caption(f"Origine : {ORIGINS[performance.origin(entry)]} · {performance.observed_count(entry)}/{entry['size']} "
               f"matchs terminés avec scores exploitables · Statut : {STATUSES[entry['status']]}")
    scenario_tables.render_outcome(outcome, show_sequence=True)
    from scenario_group_contract_panel import render as render_group_contract
    render_group_contract(entry, key=f'journal_group_contract_{entry["id"]}')
    if entry['status'] == 'pending':
        st.info('Ce groupe attend encore des résultats. Ses prévisions restent visibles et ne sont pas comptées comme des échecs.')
    elif entry['status'] == 'void':
        st.info('Ce groupe est annulé du suivi et ne participe pas aux taux de réussite.')
    elif entry['status'] == 'unresolved':
        st.warning('Ce groupe nécessite une vérification des matchs indiqués ci-dessous. Il ne participe pas aux taux de réussite.')
    render_explanation(entry, key=f'journal_results_{entry["id"]}')
    if st.button('Actualiser les résultats API de ce groupe', icon=':material/refresh:', key='journal_refresh', disabled=not admin_access.can_admin()):
        admin_access.require_admin()
        if _today() != clock_day:
            st.rerun()
        try:
            with st.spinner('Vérification des scores à la pause et à 90 minutes…'):
                tracking.refresh_entry({key: value for key, value in entry.items() if key != 'result_explanation'})
            st.rerun()
        except (journal.JournalError, api_football.ApiFootballError, ValueError) as error:
            st.error(str(error))
    st.caption('Une requête API pour ce groupe. Les résultats déjà importés sont rapprochés localement '
               'à l’ouverture de cette page et lors des actualisations du calendrier. Aucun modèle n’est réentraîné.')
    observed = {row['match_id']: row for row in entry.get('observed', [])}
    explanation = {str(row['match_id']): row for row in
                   (entry.get('result_explanation') or {}).get('matches', [])}
    presentation.render_match_results([{'Ordre': i+1, 'Coup d’envoi (Kinshasa)':
                   journal._stamp(row['timestamp']).astimezone(performance.LOCAL_ZONE).strftime('%d/%m/%Y %H:%M'), 'Domicile': row['home'],
                   'Extérieur': row['away'], 'Score à la pause': observed.get(row['match_id'], {}).get('ht', '—'),
                   'Score à 90 minutes': observed.get(row['match_id'], {}).get('ft', '—'),
                   'Résultat': observed.get(row['match_id'], {}).get('value', '—'),
                   'État du résultat': explanation.get(str(row['match_id']), {}).get('label', '—'),
                   'Motif du suivi': explanation.get(str(row['match_id']), {}).get('reason', '—')}
                  for i, row in enumerate(entry['fixtures'])],key='journal_match_results')
    for issue in entry.get('issues', []):
        st.warning(issue)
    corrections = [row['schedule_resolution'] for row in entry.get('observed', [])
                   if row.get('schedule_resolution')]
    if corrections:
        st.info('Résultats rapprochés après une correction horaire confirmée : mêmes matchs, mêmes clubs et même compétition. '
                'Les scénarios conservent leur ordre et leurs probabilités d’origine.')
        with st.expander('Horaires corrigés et règle de rapprochement'):
            table_views.dataframe([{'Match API': row['match_id'], 'Horaire enregistré · UTC': row['original_timestamp'],
                           'Horaire confirmé · UTC': row['effective_timestamp'], 'Règle': row['version']}
                          for row in corrections], hide_index=True, width='stretch', key='journal_schedule_audit', table_id='journal_panel:_detail:dataframe:1')
            st.caption('Prévision antérieure aux deux horaires, coupure historique compatible avec le match, '
                       'statut final et scores à 90 minutes vérifiés. Les changements de clubs restent à vérifier.')
    proposed_methods = effective_methods(entry)
    scenario_tables.render_matrix(proposed_methods,actual=outcome.get('actual'),key='journal_scenarios',
                                  title='Scénarios enregistrés avant match')
    diagnostic_methods=[name for name in NAMES if name in proposed_methods]
    if diagnostic_methods:
        selected_method=st.selectbox('Méthode à expliquer',diagnostic_methods,format_func=NAMES.get,
                                     key=f'journal_explanation_{entry["id"]}')
        presentation.render_diagnostic({'size':entry['size']},entry['fixtures'],engine=selected_method,
                                       saved_methods=proposed_methods)
    for name, reason in entry['metadata'].get('unavailable', {}).items():
        st.caption(f"{NAMES.get(name,name)} non enregistré : {reason}")
    with st.expander('Versions et historique des corrections de résultats'):
        st.json({'méthodes': {n: {k:v for k,v in m.items() if k not in ('ranking','distribution')}
                              for n,m in entry['methods'].items()}, 'résultats': entry['result_history']}, expanded=False)

    if st.toggle('Comparer les méthodes sur des groupes comparables', value=False,
                 key=f'journal_compare_{entry["id"]}'):
        league, size = entry['league'], entry['size']
        selected_origin = st.selectbox('Origine des groupes comparés', list(ORIGINS), format_func=ORIGINS.get,
                                       key=f'journal_comparison_origin_{league}_{size}')
        scoped = performance.filter_entries(daily, league, size, selected_origin)
        st.caption(f"Comparaison limitée aux groupes de {size} matchs de {display_competition}, "
                   f"pour le {day.strftime('%d/%m/%Y')}. "
                   'Les filtres de la liste ne modifient pas cette cohorte.')
        _comparison(scoped, league, size, selected_origin, focus_entry=entry)


def _reset_view(context):
    if st.session_state.get('journal_day') != context:
        for key in list(st.session_state):
            if key in ('journal_league', 'journal_origin', 'journal_selected_id') or key.startswith((
                    'journal_entry_', 'journal_size_', 'journal_methods_', 'journal_versions_',
                    'journal_explanation_', 'journal_scenarios_', 'journal_browser_', 'journal_compare_',
                    'journal_comparison_origin_')):
                st.session_state.pop(key, None)
        st.session_state['journal_day'] = context


def _past_days(entries, today):
    """Count complete groups by their frozen Kinshasa kickoff day."""
    counts = {}
    for entry in entries:
        fixtures = entry.get('fixtures') or []
        if not fixtures or len(fixtures) != entry.get('size'):
            continue
        try:
            days = {journal._stamp(row['timestamp']).astimezone(performance.LOCAL_ZONE).date() for row in fixtures}
        except (journal.JournalError, KeyError, TypeError):
            continue
        if len(days) == 1:
            day = next(iter(days))
            if day < today:
                counts[day] = counts.get(day, 0) + 1
    return counts


@st.fragment(run_every=60)
@guarded
def render_journal():
    today = _today()
    selected_day = get_selected_day(_now())
    render_page_header('Mes analyses et évaluations',
                       'Retrouver vos groupes pour la date choisie et les évaluations des journées passées.',
                       eyebrow='PRÉVISIONS VERROUILLÉES · RÉSULTATS RÉELS', icon=':material/history_edu:')
    st.caption('Source unique : API-Football. Vos analyses manuelles et les regroupements automatiques '
               'sont réunis ici. Les scénarios et probabilités enregistrés sont conservés.')
    manual_counts, linked_manual = {}, None
    requested_manual = st.query_params.pop('manual_analysis', None)
    try:
        manual_counts = manual_archive.days()
        if requested_manual:
            linked_manual = manual_archive.get(requested_manual)
            if linked_manual:
                st.session_state['journal_view'] = 'today'
            else:
                st.info('Cette analyse manuelle n’est pas disponible dans l’historique partagé.')
    except (manual_archive.ManualAnalysisError, OSError, ValueError):
        st.warning('Les analyses manuelles enregistrées sont temporairement indisponibles. '
                   'Les prévisions confirmées restent consultables.')
    try:
        if admin_access.can_admin():
            admin_access.require_admin()
            tracking.reconcile_local(if_changed=True)
        all_entries = result_explanations.annotate_entries(journal.list_entries(), now=_now())
        entries = [entry for entry in all_entries if entry.get('group_kind') != 'mixed']
    except (journal.JournalError, api_football.ApiFootballError, ValueError) as error:
        st.error(str(error))
        return
    if _today() != today:
        st.rerun()
    previous_day = st.session_state.get('journal_selected_day_context')
    if previous_day != selected_day:
        st.session_state['journal_selected_day_context'] = selected_day
        if previous_day is not None and not st.session_state.get('analysis_follow_today', False):
            st.session_state['journal_view'] = 'today'
    st.session_state.setdefault('journal_view', 'today')
    modes = {'today': 'Aujourd’hui' if selected_day == today else 'Date sélectionnée', 'history': 'Historique'}
    view = st.segmented_control('Consulter les analyses', list(modes), format_func=modes.get,
                                required=True, key='journal_view', persist_state='page')
    historical = view == 'history'
    day = selected_day
    if historical:
        counts = _past_days(entries, today)
        for value, count in manual_counts.items():
            manual_day = datetime.fromisoformat(value).date()
            if manual_day < today:
                counts[manual_day] = counts.get(manual_day, 0) + count
        dates = sorted(counts, reverse=True)
        if not dates:
            _reset_view((today.isoformat(), 'history', None))
            st.info('Aucune analyse enregistrée pour une journée passée pour le moment.')
            _clear_analysis_link()
            if all_entries:
                _export(all_entries)
            return
        if st.session_state.get('journal_history_date') not in dates:
            st.session_state.pop('journal_history_date', None)
        day = st.selectbox('Journée à consulter', dates,
                            format_func=lambda value: f'{value.strftime("%d/%m/%Y")} · {counts[value]} analyse(s)',
                            key='journal_history_date', persist_state='page')
        if day not in dates:
            st.error('Choisissez une journée passée disponible dans le journal.')
            return
        st.caption(f'Historique des matchs du {day.strftime("%d/%m/%Y")} · Africa/Kinshasa · UTC+1')
        st.caption('Les résultats affichés sont les derniers résultats enregistrés. '
                   'Les scénarios et probabilités restent ceux figés avant match.')
    else:
        st.caption((f'Matchs du jour : {day.strftime("%d/%m/%Y")}' if day == today else
                    f'Matchs du {day.strftime("%d/%m/%Y")}') + ' · Africa/Kinshasa · UTC+1')
        if day > today:
            st.caption('Journée à venir : les prévisions déjà enregistrées restent en attente des résultats. '
                       'L’heure de calcul et les coupures historiques restent les heures réelles.')
    _reset_view((today.isoformat(), 'history' if historical else 'today', day.isoformat()))
    if not entries and not manual_counts and not linked_manual:
        st.info('Aucune prévision avant match enregistrée pour le moment. Choisissez une compétition, '
                'puis classez un groupe de matchs à venir dans Analyser des matchs.')
        st.page_link('ui_pages/analyze.py', label='Préparer une première prévision', icon=':material/calendar_month:')
        st.caption('Les anciens classements et les simulations sur des matchs passés ne sont pas ajoutés à ce journal.')
        _clear_analysis_link()
        if all_entries:
            st.page_link('ui_pages/daily.py', label='Consulter les analyses du jour et leur historique', icon=':material/stars:')
            _export(all_entries)
        return
    daily = performance.entries_on_day(entries, day)
    try:
        rows = journal_browser_data.build_rows(daily, now=_now(), day=day)
    except (ValueError, TypeError, KeyError) as error:
        st.error('Impossible d’afficher les analyses enregistrées : '+str(error))
        _export(all_entries)
        return
    manual_entries, manual_rows = [], []
    try:
        manual_entries = manual_archive.list_entries(day=day.isoformat())
        if manual_entries:
            ids = sorted({fixture['match_id'] for entry in manual_entries for fixture in entry['fixtures']}, key=int)
            raw = api_football.load_fixtures(ids=ids)
            manual_rows = manual_journal_rows.build_rows(manual_entries, raw, now=_now())
    except (manual_archive.ManualAnalysisError, api_football.ApiFootballError, OSError, ValueError):
        st.warning('Les résultats locaux des analyses manuelles ne peuvent pas être lus pour le moment.')
        if manual_entries:
            manual_rows = manual_journal_rows.build_rows(manual_entries, None, now=_now())
    rows = sorted(rows + manual_rows, key=lambda row: (row['created_at'], row['id']), reverse=True)
    indexed = {entry['id']: entry for entry in daily}
    manual_indexed = {'manual:' + entry['id']: entry for entry in manual_entries}
    manual_results = {row['id']: row['evaluation'] for row in manual_rows}
    requested = st.query_params.pop('analysis', None)
    if requested:
        if isinstance(requested, str) and requested in indexed:
            st.session_state['journal_selected_id'] = requested
        else:
            _clear_analysis_link()
            st.session_state.pop('journal_selected_id', None)
            st.info('Cette analyse n’est pas disponible dans la journée consultée. '
                    'Choisissez sa date dans le calendrier ou dans Historique pour retrouver une évaluation passée.')
    if linked_manual:
        identity = 'manual:' + linked_manual['id']
        manual_indexed[identity] = linked_manual
        st.session_state['journal_selected_id'] = identity
    selected = st.session_state.get('journal_selected_id')
    if isinstance(selected, str) and selected.startswith('manual:') and selected not in manual_indexed:
        try:
            saved = manual_archive.get(selected.removeprefix('manual:'))
        except (manual_archive.ManualAnalysisError, OSError, ValueError):
            saved = None
        if saved:
            manual_indexed[selected] = saved
    if selected not in indexed and selected not in manual_indexed:
        st.session_state.pop('journal_selected_id', None)
        selected = None
    st.caption((f'{len(rows)} prévision(s) pour les matchs du {day.strftime("%d/%m/%Y")}' if historical or day != today else
                f'{len(rows)} prévision(s) pour les matchs d’aujourd’hui') +
               f' · {len(entries) + sum(manual_counts.values())} prévision(s) conservée(s) dans le journal complet. '
               'Les journées passées sont consultables dans Historique et exportables.')
    if manual_entries:
        st.caption(f'{len(daily)} prévision(s) confirmée(s) et {len(manual_entries)} analyse(s) manuelle(s). '
                   'Le filtre « Sélection manuelle » inclut vos calculs sauvegardés. '
                   'Les calculs sans confirmation API restent exclus des cohortes de validation.')
    if not rows and not selected:
        st.info(('Aucune prévision enregistrée pour les matchs d’aujourd’hui. ' if day == today else
                 f'Aucune prévision enregistrée pour les matchs du {day.strftime("%d/%m/%Y")}. ') +
                'Les prévisions des autres journées ne sont pas affichées dans cette vue.')
        st.page_link('ui_pages/analyze.py', label='Consulter les matchs de cette date', icon=':material/calendar_month:')
        _export(all_entries)
        return
    if selected:
        if st.button('Retour à l’historique' if historical else
                     'Retour à mes analyses du jour' if day == today else 'Retour aux analyses de cette date',
                     icon=':material/arrow_back:', key='journal_back'):
            st.session_state.pop('journal_selected_id', None)
            _clear_analysis_link()
            st.rerun()
        if selected in manual_indexed:
            entry = manual_indexed[selected]
            result = manual_results.get(selected)
            if result is None:
                try:
                    raw = api_football.load_fixtures(ids=[fixture['match_id'] for fixture in entry['fixtures']])
                    result = manual_archive.evaluate(entry, raw, now=_now())
                except (manual_archive.ManualAnalysisError, api_football.ApiFootballError, OSError, ValueError):
                    result = manual_analysis_panel._unavailable(entry)
            st.caption('Sélection manuelle · calcul sauvegardé automatiquement. '
                       'Les résultats proviennent des scores API déjà importés ; '
                       'ce calcul reste distinct des prévisions confirmées pour la validation du modèle.')
            manual_analysis_panel._detail(entry, result)
        else:
            _detail(indexed[selected], day, daily, clock_day=today)
    else:
        current = manual_journal_rows.summary(rows)
        st.caption('Bilan de la journée, toutes compétitions, tailles et origines confondues.')
        metrics = st.columns(4)
        for column, code, label in zip(metrics, performance.STATUSES,
                ('Groupes évalués', 'En attente des résultats', 'Annulés du suivi', 'À vérifier')):
            column.metric(label, current[code])
        st.caption(f"{current['observed_matches']}/{current['expected_matches']} résultats de matchs connus. "
                   'Les groupes qui partagent des matchs restent des analyses distinctes.')
        chosen = journal_browser.render(rows, day, key='journal_browser', historical=historical,
                                        selected_date=day != today)
        if chosen in indexed or chosen in manual_indexed:
            st.session_state['journal_selected_id'] = chosen
            st.rerun()
    _export(all_entries)
    _export_manual(manual_entries, day)
