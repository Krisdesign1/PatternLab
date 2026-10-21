"""Plain-language presentation of experimental scenario forecasts."""
import streamlit as st
from scenario_predictor import predict_league, MIN_TRAIN, MIN_TEST


@st.cache_data(max_entries=64, show_spinner=False)
def prepare_prediction(result):
    return predict_league(result)


def render_prediction(result):
    size = result.get('size', 4)
    st.markdown(f'### Prédire un nouveau groupe de {size} matchs')
    st.caption('Modèle expérimental propre à la compétition sélectionnée. '
               '1st = plus de buts avant la pause · Égal = autant dans chaque période · '
               '2nd = plus de buts après la pause.')
    if not st.checkbox('Afficher les prédictions et leur test historique', key='show_league_prediction'):
        return
    forecast = prepare_prediction(result)
    if not forecast['eligible']:
        st.info(f"Historique insuffisant : {forecast['training']} groupes disponibles. "
                f'Il en faut au moins {MIN_TRAIN} pour proposer une prédiction expérimentale.')
        return
    with st.container(border=True):
        st.write('**Les trois scénarios proposés**')
        for i, row in enumerate(forecast['ranking'][:3], 1):
            st.write(f"**{i}. {' → '.join(row['sequence'])}** — estimation : {100*row['probability']:.3g} %")
        st.caption('Seuls les trois premiers scénarios sont affichés ; leurs probabilités ne sont pas renormalisées. '
                   'Les pourcentages sont des estimations du modèle, pas des taux de réussite vérifiés. '
                   'En cas d’égalité, l’ordre d’affichage est conventionnel.')
    st.write(f'Ces propositions concernent un **nouveau groupe de {size} matchs futurs**, '
             'dans l’ordre chronologique de cette compétition. Elles ne sont pas associées '
             'à des équipes ou à un calendrier de matchs à venir.')
    st.caption(f"Historique utilisé jusqu’au {forecast['latest'][:10]} : "
               f"{forecast['training']} groupes complets. "
               'Les résultats hors groupe complet ne servent pas à entraîner ce modèle.')
    trials = forecast['trials']
    n = len(trials)
    st.markdown('#### A-t-il retrouvé les scénarios suivants dans l’historique ?')
    if n:
        hits = sum(t['top3'] for t in trials)
        baseline = sum(t['baseline_top3'] for t in trials)
        st.write(f'Les trois propositions contenaient le scénario exact **{hits} fois sur {n} groupes testés**. '
                 f'La méthode de comparaison en retrouvait **{baseline} sur {n}**.')
        st.caption('Il faut retrouver tous les résultats dans le bon ordre pour compter une réussite.')
    if not forecast['tested']:
        st.info(f'Test encore trop court : {n} groupes évalués, seuil de présentation fixé à {MIN_TEST}. '
                'La qualité prédictive reste à établir.')
    else:
        st.info('Le test décrit les résultats sur cet historique. '
                'Il ne démontre pas encore une amélioration durable sur de nouvelles journées.')
    with st.expander('Comprendre le modèle et son test'):
        st.write(f'Le modèle estime les {3**size:,} scénarios possibles de {size} matchs dans cette ligue. '
                 'Pour éviter de donner zéro à un scénario jamais observé, il combine ces fréquences '
                 'avec celles de chaque position du groupe. Le poids de cette correction est fixé à 10 groupes, '
                 'sans ajustement sur le test. La comparaison utilise uniquement les fréquences par position.')
        st.write('Pour chaque groupe testé, l’apprentissage utilise uniquement les groupes entièrement '
                 'situés sur des jours précédents, avec au moins 20 groupes. Les résultats du groupe testé '
                 'et ceux de la même journée ne sont jamais utilisés pour sa prédiction. '
                 'Le modèle final est ensuite réentraîné sur tous les groupes complets disponibles.')
        st.caption('Le relevé peut être incomplet. Le découpage change si l’on ajoute des matchs plus anciens. '
                   'Les horaires identiques suivent l’ordre des identifiants de la source choisie. '
                   'Ce modèle n’utilise ni les équipes ni les compositions ni les cotes.')
        if n:
            st.dataframe([{'Groupe testé': t['group'], 'Date': t['day'],
                           'Groupes appris': t['training'], 'Premier choix exact': t['top1'],
                           'Dans les trois choix': t['top3'],
                           'Comparaison dans les trois choix': t['baseline_top3']} for t in trials],
                         hide_index=True, width='stretch')
