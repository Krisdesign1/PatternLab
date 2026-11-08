"""Read-only browsing of the exact calculations saved by manual analysis."""
import table_views
from datetime import date, datetime
import re
from zoneinfo import ZoneInfo

import streamlit as st

import analysis_date
import api_football as api
import group_size_filter
import manual_analysis_store as archive
import manual_scenario_progress as progress
import manual_proposal_archive as proposals
import scenario_tables as tables
from scenario_presentation import result_column


ZONE = ZoneInfo('Africa/Kinshasa')
ENGINES = {'statistical': 'Clubs + répétitions', 'catboost': 'CatBoost entraîné',
           'context_neighbors': 'Répétitions par contexte'}


def _now():
    return datetime.now(ZONE)


def _time(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=ZoneInfo('UTC'))
    return stamp.astimezone(ZONE).strftime('%d/%m/%Y %H:%M:%S')


def _sequence(value):
    return ' → '.join(value) if value else '—'


def _verdict(value):
    return 'Oui' if value is True else 'Non' if value is False else 'Non évalué'


def _competition(entry):
    info = entry.get('competition')
    return ((info.get('name') or info.get('competition')) if isinstance(info, dict) else info) or entry['fixtures'][0]['league']


def _unavailable(entry):
    return dict(status='unavailable', status_label='Résultats locaux indisponibles', actual=None, top1=None, top3=None,
                scenario_verdicts=[dict(row, rank=index, matches_all=None)
                                   for index, row in enumerate(entry['forecast']['ranking'], 1)])


def _overview(entry, result):
    following = progress.project(entry, result)
    row = {'Calcul du': _time(entry['created_at']), 'Compétition': _competition(entry),
           'Matchs': len(entry['fixtures']), 'Moteur': ENGINES.get(entry['engine'], entry['engine']),
           'État des résultats locaux': result['status_label'],
           'Résultats confirmés': f"{following['known_count']}/{following['total']}",
           'Suivi des scénarios': following['summary'],
           'Résultat ordonné': _sequence(result.get('actual')),
           'Archivage des propositions': 'Complet' if following['comparison_complete'] else 'Comparaison incomplète',
           'Propositions distinctes conservées': len(following['rows']),
           'Exact en premier (moteur choisi)': _verdict(result.get('top1') if following['status']=='settled' else None),
           'Exact dans les 3 (moteur choisi)': _verdict(result.get('top3') if following['status']=='settled' else None)}
    if entry.get('proposal_selection') is None:
        for rank, scenario in enumerate(entry['forecast']['ranking'], 1):
            row[f'Scénario {rank}'] = _sequence(scenario['sequence'])
            row[f'Probabilité {rank} (%)'] = 100 * scenario['probability']
    else:
        row['Règle des propositions'] = 'Seuil historique conservé'
    return row


def _badge(label, color):
    return f':{color}-badge[{label}]'


def _colored_sequence(sequence):
    colors = {'1st':'blue', 'Égal':'violet', '2nd':'orange'}
    return ' → '.join(f'{index} {_badge(value, colors[value])}'
                      for index, value in enumerate(sequence, 1))


def _percentage(value):
    return '< 0,01 %' if 0 < value*100 < .01 else f'{value*100:.2f} %'.replace('.', ',')


def _detail(entry, result):
    following = progress.project(entry, result)
    coverage = following['coverage']
    if coverage.get('abstained'):
        st.info(following['summary'])
    elif not coverage['complete']:
        st.warning('Comparaison initiale incomplète : ' + coverage['reason'] +
                   ' Les trois choix du moteur conservé restent vérifiables. '
                   'Cette archive ne permet pas de mesurer le taux de réussite de toutes les propositions initialement affichées.')
    else:
        st.caption(f"Archivage complet : {coverage['proposal_count']} scénario(s) distinct(s), "
                   f"{len(coverage['method_names'])} méthode(s), avec les probabilités et rangs enregistrés avant match.")
    if entry.get('proposal_selection') is not None:
        st.caption('Le nombre de propositions découle du seuil historique conservé. Les indicateurs « exact en premier » '
                   'et « exact dans les 3 » décrivent uniquement le classement de référence original du moteur.')
        for name, reason in coverage.get('unavailable_methods', {}).items():
            st.caption(f"{tables.METHOD_LABELS.get(name, name)} : {reason}")
    st.subheader('Le calcul enregistré', icon=':material/table_view:')
    table_views.dataframe([{'Date des matchs': date.fromisoformat(entry['day']).strftime('%d/%m/%Y'),
               'Calcul enregistré à Kinshasa': _time(entry['created_at']),
               'Compétition': _competition(entry),
               'Moteur': ENGINES.get(entry['engine'], entry['engine']),
               'État du groupe': following['group_label'],
               'Résultats confirmés': f"{following['known_count']}/{following['total']}",
               'Suivi des scénarios': following['summary'],
               'Résultat ordonné': _sequence([value or '?' for value in following['actual']]),
               'Archivage des propositions': 'Complet' if coverage['complete'] else 'Comparaison incomplète',
               'Propositions distinctes conservées': len(following['rows']),
               'Exact en premier (moteur choisi)': _verdict(result.get('top1') if following['status']=='settled' else None),
               'Exact dans les 3 (moteur choisi)': _verdict(result.get('top3') if following['status']=='settled' else None)}], hide_index=True,
               height='content', width='stretch', row_height=64, key=f'manual_summary_{entry["id"]}', table_id='manual_analysis_panel:_detail:dataframe:1')
    observed = {match['match_id']: match for match in result.get('matches', [])}
    match_rows = []
    for index, fixture in enumerate(entry['fixtures'], 1):
        match = observed.get(fixture['match_id'], {})
        actual = following['actual'][index-1]
        status = match.get('status') or ('settled' if actual else following['status'])
        label = {'settled':'Résultat confirmé', 'pending':'En attente', 'void':'Annulé du suivi',
                 'non_evaluable':'À vérifier', 'unavailable':'Indisponible'}.get(status, 'À vérifier')
        match_rows.append({'Ordre': index, 'Coup d’envoi à Kinshasa': _time(fixture['timestamp']),
                           'Domicile': fixture['home'], 'Extérieur': fixture['away'],
                           'Mi-temps': match.get('ht') or 'Non reçu', 'À 90 minutes': match.get('ft') or 'Non reçu',
                           'Résultat': [actual] if actual else [], 'Statut du match':label,
                           'Motif': match.get('reason') or ('Résultat confirmé.' if actual else 'Résultat encore attendu.')})
    st.subheader('Les matchs du groupe', icon=':material/sports_soccer:')
    table_views.dataframe(match_rows,
                 hide_index=True, height='content', width='stretch', row_height=56,
                 column_config={'Résultat':result_column(),
                                'Domicile':st.column_config.TextColumn(width=190),
                                'Extérieur':st.column_config.TextColumn(width=190),
                                'Motif':st.column_config.TextColumn(width=320)},
                 key=f'manual_fixtures_{entry["id"]}', table_id='manual_analysis_panel:_detail:dataframe:2')
    tables.render_matrix(proposals.methods(entry), actual=following['actual'] if following['status']=='settled' else None,
                         key=f'manual_matrix_{entry["id"]}', title='Scénarios enregistrés avant match')
    st.subheader('Suivi de toutes les propositions conservées', icon=':material/schema:')
    colors = {'hit':'green', 'miss':'red', 'contradicted':'orange', 'possible':'blue',
              'pending':'blue', 'void':'gray', 'non_evaluable':'orange', 'unavailable':'gray'}
    table_views.table([{'Rang enregistré (moteur choisi)': str(item['rank']) if item['rank'] else 'Hors des choix proposés',
               'Scénario complet': _colored_sequence(item['sequence']),
               'Probabilité enregistrée (moteur choisi)': _percentage(item['probability']) if item['probability'] is not None else 'Non conservée',
               'Choix des méthodes': ' · '.join(f"{tables.METHOD_LABELS.get(name, name)} : choix {cell['rank']}"
                                               for name, cell in item['cells'].items() if cell['rank']),
               'Suivi du scénario': _badge(item['label'], colors[item['state']]),
               'Résultats confirmés': f"{following['known_count']}/{following['total']}",
               'Motif': item['reason']}
              for item in following['rows']], hide_index=True, border='horizontal', width='stretch', table_id='manual_analysis_panel:_detail:table:1')


def render_manual_history():
    """Read stored payloads and current local scores without refreshing or writing."""
    now = _now()
    today, selected_day = now.date(), analysis_date.get_selected_day(now)
    st.caption('Historique partagé du site. Les scénarios et leurs probabilités sont ceux du calcul '
               'enregistré, sans nouveau classement. Les résultats utilisent le catalogue local ; '
               'aucune nouvelle confirmation API n’est demandée ici.')
    st.caption('Ces calculs restent séparés des prévisions confirmées dans le suivi avant match. '
               'Des groupes peuvent partager des rencontres : leurs nombres et réussites ne constituent '
               'pas des preuves indépendantes de la précision d’un modèle.')
    try:
        linked = None
        requested = st.query_params.get('manual_analysis')
        if requested:
            if isinstance(requested, str) and re.fullmatch(r'[0-9a-f]{64}', requested):
                linked = archive.get(requested)
                if linked is None:
                    st.info('Ce calcul manuel n’est pas disponible dans l’historique partagé.')
            else:
                st.warning('Le lien vers le calcul manuel est invalide.')
        counts = archive.days()
        past = sorted((day for day in counts if date.fromisoformat(day) < today), reverse=True)
        previous = st.session_state.get('manual_archive_day_context')
        if previous is not None and previous != selected_day.isoformat():
            st.session_state['manual_archive_view'] = 'selected'
        st.session_state['manual_archive_day_context'] = selected_day.isoformat()
        if st.session_state.get('manual_archive_view') not in ('selected', 'history'):
            st.session_state['manual_archive_view'] = 'selected'
        modes = {'selected': 'Aujourd’hui' if selected_day == today else 'Date sélectionnée',
                 'history': 'Historique'}
        view = st.segmented_control('Consulter les calculs manuels', list(modes), format_func=modes.get,
                                    required=True, key='manual_archive_view', persist_state='page')
        day = selected_day.isoformat()
        if view == 'history':
            if past:
                if st.session_state.get('manual_archive_history_day') not in past:
                    st.session_state['manual_archive_history_day'] = past[0]
                day = st.selectbox('Journée des calculs manuels', past,
                    format_func=lambda value: f'{date.fromisoformat(value).strftime("%d/%m/%Y")} · {counts[value]} calcul(s)',
                    key='manual_archive_history_day', persist_state='page')
                if day not in past:
                    st.error('Choisissez une journée passée disponible dans cet historique.')
                    return
            else:
                day = None
                st.info('Aucun calcul manuel enregistré pour une journée passée pour le moment.')
        entries = archive.list_entries(day=day) if day is not None else []
        size = group_size_filter.render('manual_archive_size')
        entries = [entry for entry in entries if group_size_filter.matches(len(entry['fixtures']), size)]
        to_evaluate = {entry['id']: entry for entry in entries}
        if linked:
            to_evaluate[linked['id']] = linked
        results = {}
        if to_evaluate:
            ids = sorted({str(fixture['match_id']) for entry in to_evaluate.values()
                          for fixture in entry['fixtures']}, key=int)
            try:
                raw = api.load_fixtures(ids=ids)
            except (api.ApiFootballError, ValueError, OSError) as error:
                st.warning('Les calculs restent consultables ; les résultats locaux sont indisponibles : ' + str(error))
                results = {identity: _unavailable(entry) for identity, entry in to_evaluate.items()}
            else:
                results = {identity: archive.evaluate(entry, raw, now=now)
                           for identity, entry in to_evaluate.items()}
        if day is not None:
            st.caption(f"Matchs du {date.fromisoformat(day).strftime('%d/%m/%Y')} · {len(entries)} calcul(s) affiché(s) · Africa/Kinshasa")
        if entries:
            table_views.dataframe([_overview(entry, results[entry['id']]) for entry in entries],
                         hide_index=True, width='stretch', key='manual_archive_overview',
                         column_config={f'Probabilité {rank} (%)':st.column_config.NumberColumn(format='%.2f')
                                        for rank in range(1,4)}, table_id='manual_analysis_panel:render_manual_history:dataframe:1')
            indexed = {entry['id']: entry for entry in entries}
            key = 'manual_archive_selected_id'
            if requested in indexed and st.session_state.get('manual_archive_opened_link') != requested:
                st.session_state[key] = requested
                st.session_state['manual_archive_opened_link'] = requested
            if st.session_state.get(key) not in indexed:
                st.session_state[key] = entries[0]['id']
            identity = st.selectbox('Calcul à consulter', list(indexed),
                format_func=lambda value: f"{_time(indexed[value]['created_at'])} · {_competition(indexed[value])} · "
                    f"{len(indexed[value]['fixtures'])} matchs · {ENGINES.get(indexed[value]['engine'], indexed[value]['engine'])}",
                key=key, persist_state='page')
            if identity in indexed:
                _detail(indexed[identity], results[identity])
        elif day is not None:
            st.info('Aucun calcul manuel enregistré pour cette journée et cette taille de groupe.')
        if linked and linked['id'] not in {entry['id'] for entry in entries}:
            st.caption(f"Calcul ouvert depuis le lien · matchs du {date.fromisoformat(linked['day']).strftime('%d/%m/%Y')}")
            _detail(linked, results[linked['id']])
    except (archive.ManualAnalysisError, api.ApiFootballError, ValueError, OSError) as error:
        st.error('Historique des calculs manuels indisponible : ' + str(error))
