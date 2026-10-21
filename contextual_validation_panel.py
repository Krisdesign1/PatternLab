"""Inspect and explicitly run exploratory context-neighbour evaluations."""
from datetime import datetime
import json

import streamlit as st

import contextual_validation as evaluation
import model_campaign as campaign


METHODS = {'frequency': 'Répétitions seules', 'clubs': 'Clubs seuls',
           'combined': 'Clubs + répétitions fixes', 'catboost_plain': 'CatBoost seul',
           'context_neighbors': 'Répétitions par contexte'}


def metrics_table(metrics):
    return [{'Méthode': METHODS[name], 'Groupes testés': values['tested'],
             'Scénario exact en premier': values['top1'],
             'Scénario exact dans les 3 choix': values['top3'],
             'Erreur probabiliste moyenne ↓': round(values['loss'], 4) if values['loss'] is not None else None}
            for name in METHODS for values in [metrics[name]]]


def show_report(report, league):
    generated = datetime.fromisoformat(report['generated_at'].replace('Z', '+00:00')).strftime('%d/%m/%Y à %H:%M UTC')
    st.subheader('Apport des groupes au contexte proche', icon=':material/compare_arrows:')
    st.caption(f'Calcul du {generated}. Les années désignent le début des saisons : '
               '2024 = 2024/25, 2025 = 2025/26 et 2026 = 2026/27.')
    st.info('Comparaison exploratoire : cet historique a déjà été examiné. '
            'Les améliorations observées devront être confirmées par les prévisions enregistrées avant match.')
    size = st.segmented_control('Nombre de matchs par scénario', report['sizes'],
                                default=4 if 4 in report['sizes'] else report['sizes'][0], required=True,
                                key=f'contextual_size_{league}')
    scopes = [name for name, partial in [('complete', False), ('partial', True)]
              if any(bool(fold['partial']) == partial for fold in report['folds'])] + ['all']
    scope = st.segmented_control('Saisons à comparer', scopes, required=True, default=scopes[0],
                                 format_func=lambda name: {'complete': '2024–2025', 'partial': '2026 · partielle',
                                                           'all': 'Toutes les saisons testées'}[name],
                                 key=f'contextual_scope_{league}')
    key = {'complete': 'aggregate_complete', 'partial': 'aggregate_partial', 'all': 'aggregate'}[scope]
    metrics = report[key][str(size)]
    st.dataframe(metrics_table(metrics), hide_index=True, width='stretch')
    st.caption('Une réussite exige tous les résultats dans le bon ordre. Les méthodes utilisent exactement '
               'les mêmes groupes pour cette taille. Une erreur probabiliste plus faible est meilleure.')
    folds = [fold for fold in report['folds'] if scope == 'all' or bool(fold['partial']) == (scope == 'partial')]
    rows = []
    for fold in folds:
        current = fold['sizes'][str(size)]
        support = current['support_summary']['test']
        rows.append({'Saison testée': str(fold['test_season']),
                     'Groupes': current['test']['context_neighbors']['tested'],
                     'Erreur clubs seuls ↓': round(current['test']['clubs']['loss'], 4),
                     'Erreur avec contexte ↓': round(current['test']['context_neighbors']['loss'], 4),
                     'Poids effectif moyen (%)': round(100 * support['mean_eta'], 2),
                     'Groupes avec correction': support['active_groups']})
    st.markdown('**Réglages choisis sur validation et résultats sur la saison suivante**')
    st.dataframe(rows, hide_index=True, width='stretch')
    wins = sum(fold['sizes'][str(size)]['test']['context_neighbors']['loss'] <
               fold['sizes'][str(size)]['test']['clubs']['loss'] - 1e-12 for fold in folds)
    st.caption(f'La méthode par contexte réduit l’erreur face aux clubs seuls sur {wins} des '
               f'{len(folds)} saisons affichées. Un poids maximal nul désactive la correction des répétitions.')
    st.caption('Les groupes partagent des clubs et des antécédents. Les tailles restent séparées, '
               'et les petits effectifs de 2026 limitent les conclusions.')
    configuration = report['deployment']
    parameters = configuration['parameters'].get(str(size))
    if parameters:
        st.success('Réglages disponibles dans Analyser des matchs → Moteur de classement → Répétitions par contexte. '
                   f'Choix effectués avec les résultats jusqu’au {configuration["selection_through"]} (UTC).')
        if parameters['mix'] == 0:
            st.caption('Pour cette taille, les derniers réglages validés donnent un poids nul aux répétitions. '
                       'Les prochains classements présenteront les voisins et conserveront les probabilités des clubs.')
    with st.expander('Protocole, support et détail des essais'):
        st.markdown('Les profils avant match comprennent les buts marqués et encaissés par mi-temps, '
                    'les résultats récents, le repos et les précédents domicile/extérieur. '
                    'Les identités des clubs participent à la proximité, dans leur position respective.')
        st.caption('La distance utilise des échelles fixes. Le nombre de voisins (20 ou 50), '
                   'la demi-vie de l’ancienneté (aucune ou 365 jours) et le poids maximal '
                   '(0, 10, 25 ou 50 %) sont choisis sur validation pour chaque taille. '
                   'La demi-vie est la durée au bout de laquelle le poids lié à l’ancienneté est divisé par deux.')
        st.caption('Chaque groupe utilise uniquement les résultats antérieurs à son premier jour UTC. '
                   'Les contextes historiques sont eux-mêmes calculés avant leur propre premier jour. '
                   'Le catalogue du test est figé à entraînement + validation. Aucun groupe du test n’y entre.')
        for fold in report['folds']:
            st.markdown(f'**Test sur la saison {fold["test_season"]}**')
            st.dataframe([{'Période': label, 'Saisons': ', '.join(map(str, fold['split'][name]['seasons'])),
                           'Matchs': fold['split'][name]['matches'], 'Début UTC': fold['split'][name]['start'],
                           'Fin UTC': fold['split'][name]['end']}
                          for name, label in [('train', 'Entraînement'), ('validation', 'Validation'), ('test', 'Test')]],
                         hide_index=True, width='stretch')
            st.json({'selection': fold['sizes'][str(size)]['selection'],
                     'support': fold['sizes'][str(size)]['support_summary']}, expanded=False)
        st.caption('Le support doit atteindre au moins 12 voisins et un effectif pondéré de 8. '
                   'Le poids des répétitions est ensuite réduit selon ce support. '
                   'Ces valeurs décrivent les précédents disponibles et ne constituent pas une garantie de précision.')
        st.caption('Les réglages utilisés pour les prochains matchs proviennent de la dernière validation, '
                   'sans sélection sur les résultats de test. Les longueurs non évaluées conservent une correction nulle. '
                   'La Ligue 1 comprend les barrages renvoyés sous le même identifiant API.')
    st.download_button('Télécharger la comparaison par contexte', json.dumps(report, ensure_ascii=False, indent=2),
                       file_name=f'patternlab_contexte_{league}.json', mime='application/json',
                       icon=':material/download:', key=f'contextual_download_{league}')


def render(rows, league, source):
    st.subheader('Apprendre les répétitions selon leur contexte', icon=':material/manage_search:')
    st.markdown('Rechercher les groupes historiques aux profils les plus proches, puis mesurer '
                'si leurs scénarios améliorent les probabilités obtenues avec les clubs seuls.')
    try:
        manifest = campaign.load_manifest(league)
        plan = evaluation.plan(rows, league, source)
    except (ValueError, OSError) as error:
        st.info(str(error))
        return
    if not plan.get('suitable', False):
        st.info(' '.join(plan.get('diagnostics', [])) or 'Importez davantage de saisons pour cette compétition.')
        return
    with st.expander('Historique et périodes disponibles'):
        st.dataframe(plan['seasons'], hide_index=True, width='stretch')
        st.caption('Apprendre sur 2022, valider sur 2023 et tester sur 2024 ; '
                   'puis avancer d’une saison, jusqu’au test partiel de 2026.')
    st.caption('Calcul local sur des groupes de 2, 4, 6 et 8 matchs, sans requête API. '
               'Les modèles CatBoost enregistrés et les prévisions du journal restent distincts de ces essais.')
    if manifest is not None:
        st.info('Une première campagne est verrouillée pour cette compétition. Le rapport enregistré est consultable ; '
                'ses réglages ne seront pas remplacés par une nouvelle évaluation.')
    if st.button('Évaluer les répétitions par contexte', type='primary', icon=':material/analytics:',
                 key=f'contextual_run_{league}', disabled=manifest is not None):
        progress = st.progress(0.0, text='Préparation des contextes…')
        try:
            if campaign.load_manifest(league) is not None:
                raise campaign.CampaignError('Une campagne vient de verrouiller les réglages de cette compétition.')
            report = evaluation.run(rows, league, source,
                                    progress=lambda value, message: progress.progress(value, text=message))
            evaluation.save_report(report)
            st.success('Comparaison enregistrée. Les réglages de la dernière validation sont disponibles pour cette compétition.')
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
        if report['fingerprint'] != plan['fingerprint']:
            st.warning('L’historique local a changé depuis cette comparaison. Les réglages de la campagne restent figés.'
                       if manifest is not None else
                       'L’historique local a changé depuis cette comparaison. Relancez-la pour actualiser le rapport.')
        show_report(report, league)
    else:
        st.info('Aucune comparaison par contexte enregistrée pour cette compétition.')
