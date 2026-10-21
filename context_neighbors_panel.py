"""Show the actual historical neighbours and their contribution to a scenario."""
import json
from math import prod

import streamlit as st
import scenario_presentation as presentation
import scenario_tables


def render_result(payload):
    forecast, fixtures = payload['forecast'], payload['fixtures']
    distribution = forecast['distribution']
    support = forecast['support']
    eta = distribution['eta']
    st.caption('Les profils des clubs et des mi-temps servent à rechercher les groupes historiques '
               'les plus proches, dans cette seule compétition et dans le même ordre domicile/extérieur.')
    local = {tuple(row['sequence']): row['probability'] for row in distribution['local_mass']}
    neighbors = forecast['neighbors']
    presentation.render_leader(forecast)
    presentation.render_analysis_matrix(payload)
    presentation.render_diagnostic(forecast,fixtures,engine='context_neighbors')
    st.caption('1st : plus de buts avant la pause · Égal : autant dans chaque mi-temps · '
               '2nd : plus de buts après la pause. Chaque proposition conserve sa probabilité '
               'dans l’ensemble des scénarios possibles.')
    with st.container(horizontal=True):
        st.badge(f'{support["neighbors"]} voisins retenus', icon=':material/history:', color='gray')
        st.badge(f'Effectif pondéré : {support["ess"]:.1f}', icon=':material/groups:', color='gray')
        st.badge(f'Poids effectif des répétitions : {100 * eta:.1f} %',
                 icon=':material/tune:', color='blue')
    if not forecast.get('validated_size', True):
        st.info('Cette taille n’a pas été évaluée : les voisins restent descriptifs et le classement '
                'conserve les probabilités des clubs.')
    elif forecast['parameters']['mix'] == 0:
        st.info('La validation a retenu un poids nul pour cette taille : les voisins sont présentés '
                'pour expliquer les précédents ; les probabilités proviennent des profils des clubs.')
    elif eta == 0:
        st.info('Le support historique est insuffisant pour appliquer la correction. '
                'Le classement utilise les probabilités des clubs.')
    else:
        st.caption('Le poids choisi sur validation est réduit lorsque le support pondéré est faible. '
                   'Le reste de la probabilité provient des profils des clubs.')
    for message in forecast.get('diagnostics', []):
        st.caption(message)
    st.caption('L’effectif pondéré décrit la quantité de précédents retenus ; ces groupes peuvent partager '
               'des clubs. Cette méthode reste évaluée sur un historique déjà consulté.')
    with st.expander('Voir les groupes historiques les plus proches'):
        st.dataframe([{'Début UTC': row['first_day'], 'Fin UTC': row['end_day'],
                       'Saison': str(row['season']),
                       'Scénario observé': ' → '.join(row['sequence']),
                       'Distance des profils ↓': round(row['distance'], 4),
                       'Poids parmi les voisins (%)': round(100 * row['weight'], 2),
                       'Identifiants API des matchs': ', '.join(row['match_ids'])}
                      for row in neighbors], hide_index=True, width='stretch')
        st.caption('La distance compare les profils avant match et les identités des clubs, position par position. '
                   'La pondération combine proximité et ancienneté. Tous ces groupes sont terminés '
                   f'avant le {forecast["before"]} (UTC), premier jour de votre groupe.')
        for number, neighbor in enumerate(neighbors[:3], 1):
            if neighbor.get('matches'):
                st.markdown(f'**Précédent {number} · {neighbor["first_day"]}**')
                st.dataframe([{'Ordre': index + 1, 'Domicile': row['home'], 'Extérieur': row['away'],
                               'Coup d’envoi UTC': row['timestamp'], 'Identifiant API': row['match_id']}
                              for index, row in enumerate(neighbor['matches'])],
                             hide_index=True, width='stretch')
    with st.expander('Comprendre le calcul de ces probabilités'):
        st.dataframe([{'Scénario': ' → '.join(row['sequence']),
                       'Clubs seuls (%)': 100 * prod(p[label] for p, label in zip(distribution['baseline'], row['sequence'])),
                       'Fréquence pondérée chez les voisins (%)': 100 * local.get(tuple(row['sequence']), 0),
                       'Estimation finale (%)': 100 * row['probability']}
                      for row in forecast['ranking']], hide_index=True, width='stretch')
        st.caption('L’estimation finale mélange la probabilité des clubs et la fréquence pondérée '
                   'des voisins. Le nombre de voisins, l’ancienneté et le poids maximal des répétitions '
                   'ont été choisis sur validation, avant le test.')
        st.json(forecast['parameters'], expanded=False)
    st.page_link('ui_pages/models.py', label='Consulter les comparaisons des répétitions par contexte',
                 icon=':material/fact_check:')
    st.download_button('Sauvegarder mon analyse',
                       json.dumps({'source': payload['source'], 'engine': 'context_neighbors',
                                   'fixtures': fixtures, 'forecast': forecast}, ensure_ascii=False, indent=2),
                       file_name='patternlab_scenarios_contexte.json', mime='application/json',
                       icon=':material/download:', key='workspace_download')
