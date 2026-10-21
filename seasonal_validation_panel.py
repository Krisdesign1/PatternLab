"""Local, explicitly requested seasonal evaluation, separate from deployment."""
import json
from datetime import datetime

import streamlit as st

import seasonal_validation as evaluation


METHODS = {
    'frequency': 'Répétitions seules',
    'clubs': 'Clubs seuls',
    'combined': 'Clubs + répétitions fixes',
    'catboost_plain': 'CatBoost sans correction',
    'catboost_repeats': 'CatBoost + répétitions validées',
}


def metric_rows(metrics):
    return [{'Méthode': METHODS.get(name, name),
             'Groupes testés': value['tested'],
             'Scénario exact en premier': value['top1'],
             'Scénario exact dans les 3 choix': value['top3'],
             'Erreur probabiliste moyenne ↓': round(value['loss'], 4) if value['loss'] is not None else None}
            for name in METHODS for value in [metrics[name]]]


def _season_names(partition):
    return ', '.join(str(value) for value in partition['seasons'])


def show_report(report, league):
    st.subheader('Résultats sur les saisons suivantes', icon=':material/fact_check:')
    generated = datetime.fromisoformat(report['generated_at'].replace('Z', '+00:00')).strftime('%d/%m/%Y à %H:%M UTC')
    st.caption(f"Rapport calculé le {generated} · Résultats antérieurs au {report['cutoff']} (UTC). "
               'La saison 2026 est partielle. Cette analyse rétrospective réutilise un historique déjà disponible.')
    st.caption('Les années désignent le début des saisons : 2024 correspond à 2024/25, '
               '2025 à 2025/26 et 2026 à 2026/27.')
    size = st.segmented_control('Nombre de matchs par scénario', report['sizes'],
                                default=4 if 4 in report['sizes'] else report['sizes'][0],
                                required=True, key=f'seasonal_size_{league}')
    scopes = [name for name, partial in [('complete', False), ('partial', True)]
              if any(bool(fold['partial']) == partial for fold in report['folds'])] + ['all']
    scope = st.segmented_control('Saisons à comparer', scopes,
                                 format_func=lambda value: {'complete': '2024–2025',
                                                            'partial': '2026 · partielle',
                                                            'all': 'Toutes les saisons testées'}[value],
                                 default=scopes[0], required=True, key=f'seasonal_scope_{league}')
    aggregate_key = {'complete': 'aggregate_complete', 'partial': 'aggregate_partial', 'all': 'aggregate'}[scope]
    metrics = report[aggregate_key][str(size)]
    st.dataframe(metric_rows(metrics), hide_index=True, width='stretch')
    st.caption('Une réussite exige tous les résultats dans le bon ordre. L’erreur probabiliste '
               'mesure la probabilité attribuée au scénario réalisé : plus elle est faible, mieux c’est. '
               'Les cinq méthodes sont comparées sur les mêmes groupes pour cette taille.')
    rows = []
    folds = [fold for fold in report['folds'] if scope == 'all' or
             bool(fold['partial']) == (scope == 'partial')]
    for fold in folds:
        current = fold['sizes'][str(size)]
        plain, repeated = current['test']['catboost_plain'], current['test']['catboost_repeats']
        selection = current['selection']
        rows.append({'Saison testée': str(fold['test_season']),
                     'Groupes': plain['tested'],
                     'Erreur CatBoost seul ↓': round(plain['loss'], 4),
                     'Erreur avec répétitions ↓': round(repeated['loss'], 4),
                     'Force des répétitions': selection['alpha'],
                     'Historique des répétitions': 'Tout l’apprentissage' if selection['window'] == 'all' else 'Dernière saison'})
    st.markdown('**Stabilité et apport des répétitions par saison**')
    st.dataframe(rows, hide_index=True, width='stretch')
    wins = sum(fold['sizes'][str(size)]['test']['catboost_repeats']['loss'] <
               fold['sizes'][str(size)]['test']['catboost_plain']['loss'] - 1e-12 for fold in folds)
    st.info(f'Pour les groupes de {size} matchs, la correction des répétitions réduit l’erreur '
            f'sur {wins} des {len(folds)} saisons affichées. '
            'Une force nulle signifie que la validation n’a pas retenu cette correction.')
    st.caption('Les tailles et les saisons sont présentées séparément. Les petits échantillons, '
               'en particulier en 2026, limitent la portée des résultats. Les groupes partagent des clubs '
               'et un historique : ils ne constituent pas des essais indépendants.')
    with st.expander('Périodes, réglages et comparaisons détaillés'):
        for fold in report['folds']:
            st.markdown(f"**Test sur la saison {fold['test_season']}**")
            st.dataframe([{'Période': label, 'Saisons': _season_names(fold['split'][name]),
                           'Matchs': fold['split'][name]['matches'],
                           'Début UTC': fold['split'][name]['start'],
                           'Fin UTC': fold['split'][name]['end']}
                          for name, label in [('train', 'Entraînement'), ('validation', 'Validation'), ('test', 'Test')]],
                         hide_index=True, width='stretch')
            st.dataframe(metric_rows(fold['sizes'][str(size)]['test']), hide_index=True, width='stretch')
            st.json(fold['sizes'][str(size)]['selection'], expanded=False)
        st.caption('Les paramètres CatBoost sont choisis sur la validation des groupes de 4 matchs ; '
                   'la force et la fenêtre des répétitions sont ensuite choisies sur la validation de chaque taille. '
                   'Le modèle est réajusté sur entraînement + validation avant le test. '
                   'Pour chaque groupe, les caractéristiques des clubs utilisent uniquement les résultats '
                   'antérieurs au premier jour UTC de ce groupe. Les réglages restent figés pendant le test.')
        st.caption('Ces essais utilisent des groupes consécutifs, construits séparément dans chaque saison. '
                   'Leurs scores ne mesurent pas tous les groupes sélectionnables manuellement dans l’application.')
        st.caption('La Ligue 1 inclut les barrages renvoyés sous ce même identifiant de compétition par API-Football.')
    st.download_button('Télécharger l’évaluation par saison', json.dumps(report, ensure_ascii=False, indent=2),
                       file_name=f'patternlab_saisons_{league}.json', mime='application/json',
                       icon=':material/download:', key=f'seasonal_download_{league}')


def render(rows, league, source):
    st.subheader('Évaluer les répétitions de 2022 à 2026', icon=':material/history:')
    st.markdown('Apprendre sur les premières saisons, régler sur la suivante, puis prédire celle d’après. '
                'Chaque évaluation avance d’une saison et conserve un test distinct de la validation.')
    try:
        plan = evaluation.plan(rows, league, source)
    except ValueError as error:
        st.info(str(error))
        return
    if not plan.get('suitable', True):
        st.info(' '.join(plan.get('diagnostics', [])) or 'Historique insuffisant pour les évaluations par saison.')
        return
    with st.expander('Historique disponible et plan des évaluations'):
        st.caption('L’année affichée désigne le début de la saison. Les résultats de 2026/27 sont encore partiels.')
        st.dataframe(plan['seasons'], hide_index=True, width='stretch')
        st.dataframe([{'Apprendre sur': _season_names(fold['train']),
                       'Valider sur': _season_names(fold['validation']),
                       'Tester sur': _season_names(fold['test'])}
                      for fold in plan['folds']], hide_index=True, width='stretch')
    st.caption('Groupes de 2, 4, 6 et 8 matchs. Calcul local, sans requête API. '
               'Les modèles utilisés pour les prochains matchs et le journal restent distincts de cette évaluation.')
    if st.button('Évaluer l’historique par saison', type='primary', icon=':material/analytics:',
                 key=f'seasonal_run_{league}'):
        progress = st.progress(0.0, text='Préparation des évaluations…')
        try:
            report = evaluation.run(rows, league, source,
                                    progress=lambda value, message: progress.progress(value, text=message))
            evaluation.save_report(report)
            st.success('L’évaluation par saison est enregistrée localement.')
        except (ValueError, OSError) as error:
            st.error(str(error))
        finally:
            progress.empty()
    try:
        report = evaluation.load_report(league)
    except (ValueError, OSError) as error:
        st.error(str(error))
        return
    if report:
        if report.get('fingerprint') != plan.get('fingerprint'):
            st.warning('L’historique local a changé depuis ce rapport. Relancez l’évaluation pour le mettre à jour.')
        show_report(report, league)
    else:
        st.info('Aucune évaluation par saison enregistrée pour cette compétition.')
