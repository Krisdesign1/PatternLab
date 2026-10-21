"""Explicit local training and an auditable chronological model report."""
import json

import streamlit as st

import catboost_scenarios as ml
import model_campaign as campaign
import workspace_sources as sources
import seasonal_validation_panel
import contextual_validation_panel
import model_campaign_panel
from ux_theme import render_page_header

METHODS = {'frequency': 'Répétitions seules', 'clubs': 'Clubs seuls',
           'combined': 'Clubs + répétitions', 'catboost': 'CatBoost + répétitions apprises'}


@st.cache_data(max_entries=8, show_spinner=False)
def _rows(source, signature):
    return sources.load_matches(source)


def comparison_table(metrics):
    return [{'Méthode': METHODS.get(name, name), 'Groupes testés': value['tested'],
             'Scénario exact en premier': value['top1'],
             'Scénario exact dans les 3 choix': value['top3'],
             'Erreur probabiliste moyenne ↓': round(value['loss'], 4)}
            for name in METHODS for value in [metrics[name]]]


def show_report(report):
    st.subheader('Résultats du test réservé', icon=':material/fact_check:')
    st.caption(f"Source : {report['source']} · Compétition : {report['league']} · "
               'Scénarios de 4 matchs, dans leur ordre chronologique.')
    st.dataframe(comparison_table(report['test']), hide_index=True, width='stretch')
    delta = report['test']['catboost']['loss'] - report['test']['clubs']['loss']
    st.caption('Sur ce test, CatBoost réduit l’erreur probabiliste par rapport aux clubs seuls.'
               if delta < 0 else 'Sur ce test, CatBoost ne réduit pas l’erreur probabiliste par rapport aux clubs seuls.')
    st.caption('Une réussite exige les quatre résultats dans le bon ordre. Une erreur probabiliste '
               'plus petite est meilleure. Ce test compare les groupes consécutifs définis par le protocole ; '
               'il ne mesure pas tous les groupes que vous pourriez sélectionner à la main.')
    with st.expander('Périodes, apprentissage et méthode de comparaison'):
        period_names = {'train': 'Entraînement', 'validation': 'Validation', 'test': 'Test réservé'}
        st.dataframe([{'Période': period_names.get(name, name), 'Premier jour UTC': details['start'],
                       'Dernier jour UTC': details['end'], 'Matchs': details['matches'],
                       'Groupes complets': details['groups']} for name in period_names for details in [report['split'][name]]],
                     hide_index=True, width='stretch')
        st.markdown('**Entraînement** : apprendre les paramètres. **Validation** : choisir les réglages '
                    'et la force des répétitions. **Test réservé** : mesurer les résultats une fois ces choix figés.')
        st.caption('Les séparations respectent des jours entiers en UTC. Pour chaque groupe évalué, '
                   'toutes les affiches utilisent seulement les résultats des jours précédant son premier match.')
        st.markdown('**Comparaison pendant la validation**')
        st.dataframe(comparison_table(report['validation']), hide_index=True, width='stretch')
        st.json(report['selection'], expanded=False)
        if report['selection'].get('alpha') == 0:
            st.caption('La validation a retenu une force de répétition nulle : les fréquences des scénarios '
                       'n’ajustent pas les probabilités du modèle CatBoost de cette compétition.')
        st.caption('Après l’évaluation, un modèle distinct est entraîné sur tout l’historique admissible '
                   'pour les prochains matchs. Les scores du test ci-dessus concernent le modèle évalué, '
                   'pas ce nouvel ajustement sur toutes les données.')
    deployment = report['deployment']
    st.info(f"CatBoost disponible pour les prochains groupes : {deployment['rows']} matchs appris, "
            f"jusqu’au {deployment['trained_through']} inclus (UTC). "
            'Choisissez ce moteur dans Analyser des matchs pour l’utiliser.')
    st.download_button('Télécharger le rapport complet', json.dumps(report, ensure_ascii=False, indent=2),
                       file_name=f"patternlab_ml_{report['league']}.json", mime='application/json',
                       icon=':material/download:', key='model_report_download')


def render_training():
    render_page_header('Modèles et validation',
                       'Mesurer les répétitions d’une saison à l’autre et entraîner les modèles par compétition.',
                       eyebrow='APPRENTISSAGE · VALIDATION · TEST', icon=':material/model_training:')
    try:
        source = sources.selected_source()
        st.caption(f'Source active : {source}. L’entraînement utilise uniquement les données locales de ce fournisseur.')
        rows, audit = _rows(source, sources.data_signature(source))
    except sources.ERRORS as error:
        st.error(str(error))
        return
    view = st.segmented_control('Évaluation du modèle', ['campaign', 'context', 'seasons', 'deployment'],
                                format_func=lambda value: {'campaign': 'Campagne toutes compétitions',
                                                           'context': 'Répétitions par contexte',
                                                           'seasons': 'Historique par saison',
                                                           'deployment': 'Modèle pour les prochains matchs'}[value],
                                default='campaign', required=True, key='model_validation_view')
    if view == 'campaign':
        model_campaign_panel.render(rows, source)
        return
    leagues = {}
    for row in rows:
        code = str(row['league'])
        item = leagues.setdefault(code, {'count': 0, 'name': row.get('competition', code),
                                        'country': row.get('country', '')})
        item['count'] += 1
    if not leagues:
        st.info('Importez d’abord des saisons dans Données et réglages.')
        st.page_link('ui_pages/settings.py', label='Ouvrir les données et réglages', icon=':material/tune:')
        return
    ordered = sorted(leagues, key=lambda code: (-leagues[code]['count'], leagues[code]['name']))
    with st.container(border=True):
        league = st.selectbox('Compétition', ordered,
                              format_func=lambda code: f"{leagues[code]['country']} · {leagues[code]['name']} "
                                  f"({leagues[code]['count']} résultats)", key=f'model_league_{source}')
        st.caption('Chaque compétition possède son propre modèle. Les clubs, leur rôle domicile/extérieur, '
                   'leurs précédents par mi-temps et leur forme récente servent à prédire les trois résultats. '
                   'La force de la correction des scénarios répétés est choisie pendant la validation.')
    if view == 'context':
        contextual_validation_panel.render(rows, league, source)
        return
    if view == 'seasons':
        seasonal_validation_panel.render(rows, league, source)
        return
    try:
        frozen_campaign = campaign.load_manifest(league)
    except (campaign.CampaignError, OSError, ValueError) as error:
        st.error(str(error))
        return
    locked = frozen_campaign is not None
    if locked:
        st.info('Une première campagne est déjà verrouillée pour cette compétition. '
                'Le modèle enregistré reste consultable ; les paramètres ne sont pas réoptimisés. '
                'Utilisez la vue Campagne toutes compétitions pour suivre son état.')
    with st.container(border=True):
        st.markdown('**Découpage chronologique : environ 60 % pour apprendre, 20 % pour valider, 20 % pour tester.**')
        st.caption('Les jours restent entiers. Les réglages sont fixés avant de consulter le test. '
                   'L’entraînement ne consomme aucune requête API et peut prendre quelques minutes.')
        action = st.button('Entraîner et évaluer CatBoost', type='primary', icon=':material/model_training:',
                           key='model_train', disabled=locked or leagues[league]['count'] < 160)
        if leagues[league]['count'] < 160:
            st.info('Cette compétition manque d’historique. Importez davantage de saisons avant l’entraînement.')
    if action:
        try:
            if campaign.load_manifest(league) is not None:
                raise campaign.CampaignError('Une campagne vient de verrouiller cette compétition ; le modèle est conservé.')
            with st.spinner('Apprentissage, choix sur validation puis mesure sur le test réservé…'):
                ml.train(rows, league, source)
            st.success('Le modèle et son rapport ont été enregistrés localement.')
        except (ml.ModelError, ValueError) as error:
            st.error(str(error))
    try:
        report = ml.load_report(source, league)
    except ml.ModelError as error:
        st.error(str(error))
        return
    if report:
        show_report(report)
    else:
        st.info('Aucun modèle CatBoost enregistré pour cette compétition. Lancez l’entraînement ci-dessus.')
