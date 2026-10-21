"""Joint scenario forecasts for an ordered group of clubs in one competition."""
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json

import streamlit as st

import match_sources
from contextual_scenarios import forecast, evaluate


@st.cache_data(max_entries=16, show_spinner=False)
def prepare_forecast(result, fixtures):
    return forecast(result, fixtures)


@st.cache_data(max_entries=16, show_spinner=False)
def prepare_evaluation(result):
    return evaluate(result)


@st.cache_data(max_entries=16, show_spinner=False)
def prepare_calendar(source, league, signature):
    return match_sources.scheduled_fixtures(source, league)


def _fixture_table(fixtures):
    return [{'Place': i + 1, 'Date': row['timestamp'].replace('T', ' '),
             'Domicile': row['home'], 'Extérieur': row['away']}
            for i, row in enumerate(fixtures)]


def _show_forecast(estimate, fixtures):
    st.dataframe(_fixture_table(fixtures), hide_index=True, width='stretch')
    if not estimate['eligible']:
        st.info(f"Historique insuffisant avant le {estimate['before']} : "
                f"{estimate['training_groups']} groupes complets et {estimate['training_matches']} matchs. "
                'Il faut au moins 20 groupes et 30 matchs de cette compétition sur 3 jours.')
        return
    st.markdown('#### Scénarios adaptés à ce groupe de clubs')
    st.dataframe([
        {'Rang': i + 1, 'Scénario': ' → '.join(row['sequence']),
         'Estimation combinée (%)': 100 * row['probability'],
         'Clubs seuls (%)': 100 * row['club_probability'],
         'Répétitions seules (%)': 100 * row['frequency_probability'],
         'Apparitions avant la date': row['observed']}
        for i, row in enumerate(estimate['ranking'])
    ], hide_index=True, width='stretch', column_config={
        name: st.column_config.NumberColumn(name, format='%.3g')
        for name in ('Estimation combinée (%)', 'Clubs seuls (%)', 'Répétitions seules (%)')})
    st.caption('Les scénarios suivent l’ordre des matchs affichés. Les trois estimations ne sont pas '
               'renormalisées pour totaliser 100 %. Ce sont des probabilités expérimentales du scénario '
               'entier, pas des taux de réussite mesurés.')
    st.caption(f"Apprentissage : {estimate['training_groups']} groupes et {estimate['training_matches']} matchs, "
               f"sur les jours strictement antérieurs au {estimate['before']}. "
               f"Profils des deux clubs disponibles pour {estimate['personalized_count']} match(s) sur {len(fixtures)}.")
    if estimate['personalized_count'] < len(fixtures):
        st.info('Pour certaines affiches, les précédents des clubs à domicile ou à l’extérieur sont '
                'insuffisants. Ces affiches utilisent la tendance de la compétition.')
    with st.expander('Historique des clubs utilisé pour ce groupe'):
        st.dataframe([
            {'Place': i + 1, 'Match': f"{fixture['home']} — {fixture['away']}",
             'Précédents domicile': item['home_profile']['matches'],
             'Précédents extérieur': item['away_profile']['matches'],
             'Deux profils utilisés': item['personalized']}
            for i, (fixture, item) in enumerate(zip(fixtures, estimate['club_estimates']))
        ], hide_index=True, width='stretch')
    with st.expander('Pourquoi ces scénarios sont-ils proposés ?'):
        for row in estimate['ranking']:
            st.write('**' + ' → '.join(row['sequence']) + '**')
            st.write(f"Observé {row['observed']} fois avant la date ; "
                     f"{row['expected']:.3g} apparition(s) attendue(s) selon la référence par position.")
            st.caption('La correction des répétitions est combinée aux habitudes des clubs. '
                       'Le nombre attendu dans cette explication est lissé pour permettre '
                       'l’estimation de tous les scénarios, y compris ceux jamais observés.')
    st.download_button('Sauvegarder cette estimation de scénarios',
                       json.dumps({'fixtures': fixtures, 'forecast': estimate}, ensure_ascii=False, indent=2),
                       file_name='scenarios_contextuels.json', mime='application/json', key='contextual_export')


def render_contextual_scenarios(result, league, source):
    size = result.get('size', 4)
    st.markdown(f'### Classer les scénarios de {size} matchs avec les clubs')
    st.caption('Les répétitions de cette compétition et les habitudes des clubs par mi-temps '
               'contribuent ensemble au classement du scénario complet.')
    if not st.checkbox('Utiliser les clubs pour classer les scénarios', key='show_contextual_scenarios'):
        return
    scope = f'{source}_{league}_{size}'
    history_signature = sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds')
    try:
        calendar = [row for row in prepare_calendar(source, league, match_sources.data_signature(source))
                    if row['timestamp'] > now]
    except match_sources.SOURCE_ERRORS as error:
        st.warning(str(error))
        calendar = []
    mode = st.selectbox('Définir le groupe de matchs',
                        ['Calendrier importé', 'Saisir les affiches'] if calendar else ['Saisir les affiches'],
                        key=f'contextual_mode_{scope}')
    if source == 'Forebet':
        st.caption('Les dates suivent les horaires du relevé Forebet. Les affiches saisies sont des simulations.')
    else:
        st.caption('Dates et ordre chronologique en UTC. La liste du calendrier provient du dernier import local.')
    if not calendar:
        st.info('Aucune affiche future confirmée dans le calendrier local de cette compétition. '
                'Tu peux saisir un groupe ou actualiser la saison dans Historique & données.')
    fixtures = []
    with st.form(f'contextual_fixtures_{scope}_{mode}'):
        if mode == 'Calendrier importé':
            indexed = {row['match_id']: row for row in calendar}
            chosen = st.multiselect(f'Choisir exactement {size} matchs de cette compétition', list(indexed),
                                     format_func=lambda key: f"{indexed[key]['timestamp'].replace('T', ' ')} · "
                                         f"{indexed[key]['home']} — {indexed[key]['away']}",
                                     max_selections=size, key=f'contextual_calendar_{scope}')
            fixtures = sorted([indexed[key] for key in chosen],
                              key=lambda row: (row['timestamp'], int(row['match_id'])))
        else:
            latest = max(row['timestamp'][:10] for row in result['matches'])
            cutoff = st.date_input('Date du premier match du groupe',
                                   value=max(datetime.now(timezone.utc).date(), date.fromisoformat(latest) + timedelta(days=1)),
                                   key=f'contextual_date_{scope}')
            teams = sorted({row['home'] for row in result['matches']} | {row['away'] for row in result['matches']})
            st.caption('Saisis les affiches dans l’ordre chronologique du groupe. '
                       'La même date de coupure est utilisée pour tous les matchs.')
            for i in range(size):
                st.write(f'**Match {i+1}**')
                home = st.selectbox('Club à domicile', teams, index=None,
                                    key=f'contextual_home_{scope}_{i}')
                away = st.selectbox('Club à l’extérieur', teams, index=None,
                                    key=f'contextual_away_{scope}_{i}')
                fixtures.append({'league': league, 'home': home, 'away': away,
                                 'timestamp': cutoff.isoformat() + 'T12:00:00'})
        submitted = st.form_submit_button('Classer les scénarios de ce groupe', icon=':material/schema:')
    if submitted:
        st.session_state.pop(f'contextual_last_{scope}', None)
        if len(fixtures) != size or any(not row['home'] or not row['away'] for row in fixtures):
            st.warning(f'Choisis les deux clubs de chacun des {size} matchs.')
        else:
            try:
                with st.spinner('Calcul des scénarios avec les clubs et les répétitions…'):
                    estimate = prepare_forecast(result, fixtures)
                st.session_state[f'contextual_last_{scope}'] = {
                    'mode': mode, 'history': history_signature, 'estimate': estimate, 'fixtures': fixtures}
            except ValueError as error:
                st.warning(str(error))
    previous = st.session_state.get(f'contextual_last_{scope}')
    if previous and previous['mode'] == mode and previous['history'] == history_signature:
        st.caption('Dernier groupe validé avec le bouton de classement :')
        _show_forecast(previous['estimate'], previous['fixtures'])

    st.markdown('#### Comparer les trois méthodes sur les scénarios suivants')
    if st.button('Évaluer les scénarios sur les dates suivantes', key=f'contextual_evaluate_{scope}'):
        with st.spinner('Comparaison chronologique des scénarios complets…'):
            evaluation = prepare_evaluation(result)
        st.session_state[f'contextual_test_{scope}'] = {'history': history_signature, 'evaluation': evaluation}
    previous_test = st.session_state.get(f'contextual_test_{scope}')
    if previous_test and previous_test['history'] == history_signature:
        evaluation = previous_test['evaluation']
        tested = evaluation['summary']['tested']
        if not tested:
            st.info('Pas encore de groupe testable après 20 groupes d’apprentissage '
                    'entièrement situés sur des jours précédents.')
        else:
            names = {'frequency': 'Répétitions seules', 'clubs': 'Clubs seuls',
                     'combined': 'Clubs + répétitions'}
            st.dataframe([
                {'Méthode': names[name], 'Groupes testés': metrics['tested'],
                 'Premier scénario exact': metrics['top1'], 'Dans les trois scénarios': metrics['top3'],
                 'Erreur probabiliste moyenne': metrics['loss']}
                for name, metrics in evaluation['summary']['models'].items()
            ], hide_index=True, width='stretch')
            st.caption('Une réussite exige le scénario entier dans le bon ordre. '
                       'L’erreur probabiliste moyenne est meilleure quand elle est plus petite. '
                       'Les trois méthodes sont évaluées sur les mêmes groupes et les mêmes dates.')
            metrics = evaluation['summary']['models']
            if metrics['combined']['loss'] >= min(metrics[name]['loss'] for name in ('clubs', 'frequency')):
                st.info('Sur ce test, la combinaison ne donne pas la meilleure erreur probabiliste moyenne. '
                        'Consulte aussi les résultats des deux références avant de retenir une méthode.')
            st.info('Ces résultats mesurent ce test historique. Un gain doit être confirmé sur '
                    'de nouvelles journées ; les paramètres ne sont pas ajustés sur ce test.')
            st.download_button('Sauvegarder la comparaison chronologique',
                               json.dumps(evaluation, ensure_ascii=False, indent=2),
                               file_name='evaluation_scenarios_contextuels.json', mime='application/json',
                               key='contextual_evaluation_export')
    with st.expander('Méthode du classement contextuel'):
        st.write('Pour chaque affiche, le modèle calcule des estimations par mi-temps à partir '
                 'des précédents des clubs à domicile et à l’extérieur dans cette compétition. '
                 'Le produit de ces estimations forme une première distribution des scénarios.')
        st.write('La fréquence de chaque scénario historique est comparée à celle attendue '
                 'avec des positions indépendantes. Cette correction, lissée et atténuée par '
                 'une racine carrée, ajuste la distribution des clubs. Le résultat est normalisé '
                 'sur tous les scénarios possibles, même si seuls les trois premiers sont affichés.')
        st.caption('Tous les résultats utilisés précèdent strictement le jour du premier match du groupe. '
                   'Les clubs peu documentés utilisent la tendance de la compétition. '
                   'Les paramètres sont fixes ; les anciennes saisons ne sont pas pondérées par récence. '
                   'Le test porte sur les groupes chronologiques de la ligue ; sa portée ne couvre '
                   'pas automatiquement tous les sous-ensembles de matchs choisis manuellement.')
