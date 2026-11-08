"""Explicit, local candidate reviews, separate from the deployed first campaign."""
import table_views
import json

import streamlit as st

import admin_access
import model_reassessment as review
import candidate_tracking_panel
from seasonal_validation_panel import METHODS


STATES = {'building': 'En cours', 'completed': 'Candidat évalué', 'error': 'À reprendre'}
METHOD_NAMES = dict(METHODS, catboost='CatBoost + correction retenue')


@st.cache_data(ttl=60, max_entries=8, show_spinner=False)
def _plan(rows, league, source, sizes):
    return review.plan(rows, league, source, sizes=sizes)


def _number(value):
    return round(value, 4) if value is not None else None


def _percent(value):
    return round(value * 100, 2) if value is not None else None


def _metrics(metrics):
    return [{'Méthode': METHOD_NAMES.get(name, name), 'Groupes testés': value['tested'],
             'Scénario exact en premier': value['top1'],
             'Scénario exact dans les 3 choix': value['top3'],
             'Erreur probabiliste ↓': _number(value['loss'])}
            for name, value in metrics.items()]


def show_report(item):
    """Only render the selected immutable report; never fit or publish a model."""
    report = item.get('report')
    if not report:
        st.info('Cette réévaluation ne possède pas encore de rapport terminé. '
                'Un nouveau lancement avec le même historique reprend son instantané.')
        if item.get('error'):
            st.warning(item['error'])
        return
    st.subheader('Candidat conservé à part', icon=':material/science:')
    st.info('Ce candidat est expérimental. Il ne remplace pas le modèle utilisé pour les prévisions. '
            'Un meilleur résultat sur un historique déjà disponible ne prouve pas un avantage sur les prochains matchs.')
    native = report['native']
    deployment = native['deployment']
    st.caption(f"Historique figé : {item['history_matches']} résultats antérieurs au {item['cutoff']} UTC · "
               f"Apprentissage du candidat jusqu’au {deployment['trained_through']} inclus.")
    st.caption(f"Modèle en service au lancement : {(report.get('incumbent_generation') or 'Aucun')[:16]} · "
               f"Candidat : {report['candidate_generation'][:16]}.")
    st.markdown('**Test natif réservé · groupes de 4 matchs**')
    table_views.dataframe(_metrics(native['test']), hide_index=True, width='stretch', key='reassessment_native_test', table_id='model_reassessment_panel:show_report:dataframe:1')
    st.caption('Le protocole natif choisit ensemble les paramètres CatBoost et la force de correction '
               'sur la validation de groupes de 4 matchs ; les répétitions utilisent tout l’apprentissage. '
               'Le test porte sur un modèle appris sur entraînement + validation. '
               'Le candidat conservé est ensuite réajusté sur tout l’historique : ce test ne mesure pas ses performances futures.')

    seasonal = report['seasonal']
    if seasonal.get('procedure') == 'native':
        st.markdown('**Évaluation saisonnière alignée sur l’entraînement natif**')
        st.caption('Même sélection conjointe des paramètres et de la correction sur les groupes de 4 matchs, '
                   'mêmes blocs chronologiques et répétitions sur tout l’apprentissage. Les autres tailles '
                   'réutilisent ces paramètres figés ; elles ne choisissent pas leurs propres réglages.')
    else:
        st.markdown('**Comparaisons saisonnières distinctes · ancien protocole conservé**')
        st.caption('Ces essais choisissent d’abord les paramètres avec CatBoost sans correction, '
               'puis la force et la fenêtre des répétitions pour chaque taille '
               '(tout l’apprentissage ou dernière saison). Cette procédure diffère du protocole natif : '
               'ces résultats ne valident pas directement le candidat conservé.')
    sizes = seasonal['sizes']
    size_key = f"reassessment_size_{item['run_id']}"
    size = st.segmented_control('Taille des scénarios évalués', sizes,
                                default=4 if 4 in sizes else sizes[0], required=True, key=size_key)
    st.caption('Chaque taille garde sa propre comparaison. Les erreurs de groupes de tailles différentes '
               'ne sont pas regroupées en un score global.')
    table_views.dataframe(_metrics(seasonal['aggregate'][str(size)]), hide_index=True, width='stretch',
                 key='reassessment_metrics', table_id='model_reassessment_panel:show_report:dataframe:2')
    st.caption('Ce tableau inclut les saisons partielles indiquées ci-dessous. Une erreur plus faible est meilleure. '
               'Les méthodes utilisent les mêmes groupes pour la taille affichée.')
    table_views.dataframe([{'Saison testée': fold['test_season'],
                   'Saison partielle': fold['partial'],
                   'Groupes testés': fold['sizes'][str(size)]['test']['clubs']['tested'],
                   'Erreur Clubs ↓': _number(fold['sizes'][str(size)]['test']['clubs']['loss']),
                   'Erreur CatBoost seul ↓': _number(fold['sizes'][str(size)]['test']['catboost_plain']['loss']),
                   'Erreur CatBoost + répétitions ↓': _number(fold['sizes'][str(size)]['test']['catboost_repeats']['loss']),
                   'Force des répétitions': fold['sizes'][str(size)]['selection']['alpha']}
                  for fold in seasonal['folds']], hide_index=True, width='stretch', key='reassessment_seasons', table_id='model_reassessment_panel:show_report:dataframe:3')

    seasons = [fold['test_season'] for fold in seasonal['folds']]
    season = st.selectbox('Saison à examiner en détail', seasons, key=f"reassessment_season_{item['run_id']}")
    fold = next(fold for fold in seasonal['folds'] if fold['test_season'] == season)
    diagnostics = fold['sizes'][str(size)].get('diagnostics', {})
    calibration = diagnostics.get('calibration', {}).get('test', {})
    if calibration:
        st.markdown('**Contrôle des pourcentages par match sur le test**')
        table_views.dataframe([{'Méthode': METHODS.get(name, name), 'Matchs distincts': value['matches'],
                       'Erreur logarithmique ↓': _number(value['logloss']),
                       'Score de Brier ↓': _number(value['brier'])}
                      for name, value in calibration.items()], hide_index=True, width='stretch',
                     key='reassessment_calibration', table_id='model_reassessment_panel:show_report:dataframe:4')
        st.caption('Ces diagnostics utilisent les probabilités par match de Clubs et de CatBoost sans correction. '
                   'Ils ne mesurent pas la calibration de la distribution finale des scénarios. '
                   'Un Brier plus faible ne suffit pas à prouver une meilleure calibration.')
        with st.expander('Comparer les pourcentages annoncés aux fréquences observées'):
            label = st.segmented_control('Résultat à examiner', ['1st', 'Égal', '2nd'], default='1st',
                                          required=True, key=f"reassessment_label_{item['run_id']}")
            bins = [{'Méthode': METHODS.get(name, name),
                     'Intervalle annoncé (%)': f"{row['lower'] * 100:.0f}–{row['upper'] * 100:.0f}",
                     'Matchs': row['count'], 'Probabilité moyenne (%)': _percent(row['mean_probability']),
                     'Fréquence observée (%)': _percent(row['observed_rate'])}
                    for name, value in calibration.items()
                    for row in value['reliability'][label] if row['count']]
            table_views.dataframe(bins, hide_index=True, width='stretch', key='reassessment_reliability', table_id='model_reassessment_panel:show_report:dataframe:5')
            st.caption('Exemple : parmi les prévisions proches de 60 %, ce résultat devrait se produire '
                       'environ six fois sur dix sur un effectif suffisant. Les petits effectifs restent fragiles. '
                       'Aucune correction des probabilités n’est apprise par ce diagnostic.')
    coverage = diagnostics.get('group_coverage', {}).get('test', {})
    if coverage:
        with st.expander('Probabilités des scénarios et réalisations observées'):
            table_views.dataframe([{'Méthode': METHODS.get(name, name), 'Groupes': value['groups'],
                           'Probabilité moyenne du premier (%)': _percent(value['mean_top1_probability']),
                           'Premier réalisé (%)': _percent(value['observed_top1_rate']),
                           'Probabilité moyenne des 3 choix (%)': _percent(value['mean_top3_probability']),
                           'Un des 3 réalisé (%)': _percent(value['observed_top3_rate'])}
                          for name, value in coverage.items()], hide_index=True, width='stretch', table_id='model_reassessment_panel:show_report:dataframe:6')
            st.caption('Ces moyennes descriptives ne sont pas une garantie pour le prochain groupe. '
                       'Les groupes peuvent partager des clubs et des antécédents.')
    with st.expander('Règles conservées avant le calcul et rapport du candidat'):
        st.json(report['policy'], expanded=False)
        st.json(native['selection'], expanded=False)
        st.caption('Le test ne choisit pas les réglages. Le candidat natif choisit ensemble les paramètres '
                   'et la correction sur les groupes de 4 matchs. Les rapports portant la mention « alignée » '
                   'emploient cette même procédure ; les anciennes évaluations gardent leur protocole distinct. '
                   'Les autres tailles réutilisent les réglages sélectionnés sur 4 matchs.')
    st.warning('Remplacement non autorisé par ces seuls résultats : il reste à comparer les prévisions '
               'enregistrées avant de nouveaux matchs, sur les mêmes rencontres, avec des périodes d’évaluation closes. '
               'La première campagne et le journal gardent leurs versions d’origine.')
    candidate_tracking_panel.render(item)
    st.download_button('Télécharger cette réévaluation', json.dumps(item, ensure_ascii=False, indent=2),
                       file_name=f"patternlab_reevaluation_{item['league']}_{item['run_id'][:12]}.json",
                       mime='application/json', icon=':material/download:', key='reassessment_download')


def render(rows, league, source='API-Football'):
    st.subheader('Réévaluer sans remplacer le modèle en service', icon=':material/science:')
    st.markdown('Entraîner un candidat sur un nouvel instantané local, puis comparer les méthodes '
                'sur les saisons suivantes. Les entrées, règles et résultats de chaque lancement sont conservés.')
    st.caption('Ouvrir cette vue ne lance ni entraînement ni requête API. Le calcul manuel peut prendre plusieurs minutes.')
    sizes = st.multiselect('Tailles à comparer', list(range(2, 21)), default=list(review.DEFAULT_SIZES),
                           key='reassessment_sizes')
    if not sizes:
        st.info('Choisissez au moins une taille de groupe.')
        return
    scoped = [row for row in rows if str(row.get('league')) == str(league)]
    try:
        planned = _plan(scoped, league, source, tuple(sizes))
        runs = review.list_runs(league)
    except (ValueError, OSError) as error:
        st.error(str(error))
        return
    with st.container(horizontal=True):
        st.metric('Résultats dans l’instantané actuel', planned['history_matches'])
        st.metric('Nouveaux matchs depuis le modèle en service',
                  planned['new_matches'] if planned['new_matches'] is not None else '—')
        st.metric('Réévaluations conservées', len(runs))
    if not planned['ready']:
        st.info(planned.get('reason') or 'Historique insuffisant pour cette réévaluation.')
    if planned.get('incumbent_history_changed'):
        st.warning('Une partie de l’historique utilisé par le modèle en service a été corrigée ou complétée. '
                   'Le nouveau candidat conservera son propre instantané ; les anciennes prévisions restent inchangées.')
    with st.expander('Périodes prévues pour la comparaison par saison'):
        table_views.dataframe([{'Saison testée': fold['test_season'], 'Partielle': fold['partial'],
                       'Fin de l’apprentissage UTC': fold['train']['end'],
                       'Fin de la validation UTC': fold['validation']['end'],
                       'Début du test UTC': fold['test']['start']}
                      for fold in planned['seasonal_folds']], hide_index=True, width='stretch', table_id='model_reassessment_panel:render:dataframe:1')
        st.caption('Le découpage respecte des journées UTC entières. Les paramètres sont choisis sur la validation. '
                   'Les tests historiques déjà disponibles restent exploratoires, même après un nouveau calcul.')
    action = st.button('Entraîner et évaluer un candidat à part', type='primary',
                       icon=':material/model_training:', key='reassessment_run',
                       disabled=not planned['ready'] or not admin_access.can_admin())
    if action:
        admin_access.require_admin()
        progress = st.progress(0.0, text='Préparation de l’instantané et des règles…')
        try:
            result = review.run(scoped, league, source, sizes=tuple(sizes),
                                progress=lambda value, message: progress.progress(value, text=message))
            _plan.clear()
            runs = review.list_runs(league)
            st.session_state['reassessment_run_selected'] = result['run_id']
            st.success('Le candidat et son évaluation sont conservés. Le modèle en service reste en place.')
        except (ValueError, OSError) as error:
            st.error(str(error))
        finally:
            progress.empty()
    if not runs:
        st.info('Aucune réévaluation enregistrée pour cette compétition.')
        return
    table_views.dataframe([{'Créée le (UTC)': item['frozen_at'], 'État': STATES.get(item['status'], item['status']),
                   'Résultats': item['history_matches'], 'Tailles': ', '.join(map(str, item['sizes'])),
                   'Identifiant': item['run_id'][:12]}
                  for item in runs], hide_index=True, width='stretch', key='reassessment_runs', table_id='model_reassessment_panel:render:dataframe:2')
    indexed = {item['run_id']: item for item in runs}
    if st.session_state.get('reassessment_run_selected') not in indexed:
        st.session_state['reassessment_run_selected'] = runs[0]['run_id']
    selected = st.selectbox('Réévaluation à consulter', list(indexed),
                            format_func=lambda run_id: f"{indexed[run_id]['frozen_at']} · "
                                f"{STATES.get(indexed[run_id]['status'], indexed[run_id]['status'])} · {run_id[:12]}",
                            key='reassessment_run_selected')
    try:
        show_report(review.load_run(league, selected))
    except (ValueError, OSError) as error:
        st.error(str(error))
