"""User-selected fixtures and an honest league-specific evaluation."""
from datetime import date, timedelta
import streamlit as st
from league_model import VALUES
from coupon_sizes import SIZES
from match_predictor import estimate, backtest, summarize, group_scenarios


@st.cache_data(max_entries=64, show_spinner=False)
def evaluation(rows, league):
    return backtest(rows, league)


def render_match_prediction(result, league):
    st.markdown('### Estimer un match avec ses deux équipes')
    if not st.checkbox('Choisir mes matchs et comparer le modèle', key='show_team_prediction'):
        return
    rows = result['matches']
    st.write('Choisis les équipes de la compétition sélectionnée. '
             'Les affiches saisies ici sont des simulations. Le calendrier des API enregistré '
             'peut être consulté dans Historique & données.')
    st.caption('1st : plus de buts avant la pause · Égal : autant dans chaque mi-temps · '
               '2nd : plus de buts après la pause. Ce ne sont pas des pronostics de vainqueur.')
    teams = sorted({r['home'] for r in rows} | {r['away'] for r in rows})
    size = st.selectbox('Combien de matchs veux-tu estimer ?', [1, *SIZES], key='team_group_size')
    latest = max(r['timestamp'][:10] for r in rows)
    with st.form(f'team_fixtures_{league}_{size}'):
        cutoff = st.date_input('Date de la simulation',
                               value=max(date.today(), date.fromisoformat(latest)+timedelta(days=1)),
                               key=f'team_date_{league}_{size}',
                               help='Seuls les jours strictement antérieurs servent à apprendre. '
                                    'Pour un groupe, utilise la date du premier match.')
        fixtures = []
        for i in range(size):
            st.write(f'**Match {i+1}**')
            home = st.selectbox('Équipe à domicile', teams, index=None,
                                placeholder='Choisis une équipe', key=f'team_home_{league}_{size}_{i}')
            away = st.selectbox('Équipe à l’extérieur', teams, index=None,
                                placeholder='Choisis une équipe', key=f'team_away_{league}_{size}_{i}')
            fixtures.append((home, away))
        submitted = st.form_submit_button('Estimer mes matchs')
    if submitted:
        flat = [t for fixture in fixtures for t in fixture]
        if None in flat:
            st.warning('Choisis les deux équipes de chaque match.')
        elif any(h == a for h, a in fixtures):
            st.warning('Un match doit opposer deux équipes différentes.')
        elif len(set(fixtures)) != len(fixtures):
            st.warning('Chaque affiche doit apparaître une seule fois dans cette sélection.')
        else:
            estimates = [estimate(rows, league, h, a, cutoff.isoformat()) for h,a in fixtures]
            for i, ((home, away), e) in enumerate(zip(fixtures, estimates), 1):
                with st.container(border=True):
                    st.write(f'**{i}. {home} — {away}**')
                    if not e['eligible']:
                        st.info(f"Historique insuffisant avant cette date : {e['training']} matchs. "
                                'Il faut au moins 30 matchs de la ligue répartis sur 3 jours.')
                        continue
                    p = e['probabilities']
                    leaders = [v for v in VALUES if abs(p[v]-max(p.values())) < 1e-12]
                    st.write('**Tendance : ' + ' / '.join(leaders) + '**')
                    hp, ap = e['home_profile'], e['away_profile']
                    if e['personalized']:
                        st.write(f"Le modèle utilise {hp['matches']} matchs de {home} à domicile "
                                 f"et {ap['matches']} matchs de {away} à l’extérieur, "
                                 'avec les buts marqués et encaissés dans chaque mi-temps.')
                    else:
                        st.info('Historique des équipes insuffisant : tendance de la ligue uniquement. '
                                'Il faut au moins 3 matchs pour chacune dans son rôle domicile ou extérieur.')
                        st.caption(f"Domicile : {hp['matches']} matchs · Extérieur : {ap['matches']} matchs.")
                    with st.expander(f'Voir les habitudes des équipes — match {i}'):
                        table = []
                        for name, role, profile in ((home, 'Domicile', hp), (away, 'Extérieur', ap)):
                            count = profile['matches']
                            if count:
                                table.append({'Équipe': name, 'Lieu': role, 'Matchs': count,
                                              'Marqués avant la pause / match': round(profile['scored'][0]/count, 2),
                                              'Marqués après la pause / match': round(profile['scored'][1]/count, 2),
                                              'Encaissés avant la pause / match': round(profile['conceded'][0]/count, 2),
                                              'Encaissés après la pause / match': round(profile['conceded'][1]/count, 2)})
                        if table:
                            st.dataframe(table, hide_index=True, width='stretch')
                        else:
                            st.info('Aucun précédent dans ces rôles avant la date choisie.')
                    st.caption(f"{e['training']} matchs de la ligue utilisés, jusqu’au {e['last_training_day']}.")
                    st.write(' · '.join(f'{v} : {p[v]:.0%}' for v in VALUES))
                    st.caption('Estimations expérimentales ; les pourcentages ne sont pas des taux de réussite vérifiés.')
            if size >= 2 and all(e['eligible'] for e in estimates):
                st.write('**Les trois scénarios de ton groupe** — dans l’ordre des matchs saisis')
                for i, s in enumerate(group_scenarios(estimates, limit=3), 1):
                    st.write(f"{i}. **{' → '.join(s['sequence'])}** — {100*s['probability']:.3g} %")
                st.caption('Combinaison calculée en supposant les résultats des matchs indépendants, '
                           'une simplification notamment si une équipe revient dans plusieurs matchs. '
                           'Seuls les trois premiers scénarios sont affichés, sans renormaliser les probabilités. '
                           'La réussite de ces groupes personnalisés n’a pas encore été évaluée. '
                           'Les ex æquo sont départagés par un ordre conventionnel.')
    st.markdown('#### Le modèle fait-il mieux que la tendance de la ligue ?')
    records = evaluation(rows, league)
    summary = summarize(records)
    n = summary['tested']
    if not n:
        st.info('Pas encore de match testable après une période d’apprentissage suffisante dans cette ligue.')
    else:
        st.write(f"Sur **{n} matchs testés**, le modèle trouve le bon résultat **{summary['correct']} fois**, "
                 f"contre **{summary['baseline_correct']}** pour la tendance de la ligue.")
        st.caption(f"Les habitudes des équipes ont pu être utilisées sur {summary['personalized']} de ces matchs. "
                   'Les autres utilisent uniquement la tendance de la ligue. '
                   'Ces chiffres portent sur un résultat par match, pas sur des groupes de plusieurs matchs.')
        personal = summarize([r for r in records if r['personalized']])
        if personal['tested']:
            st.write(f"Sur les {personal['tested']} matchs avec historique des deux équipes : "
                     f"{personal['correct']} résultats trouvés, contre {personal['baseline_correct']} pour la référence.")
        if personal['tested'] < 30:
            st.info('Pas encore assez de matchs testés avec les deux profils d’équipe pour juger leur apport.')
        if n < 30:
            st.info('Échantillon de test trop court pour juger la méthode : moins de 30 matchs.')
        elif summary['correct'] <= summary['baseline_correct']:
            st.info('Ce test ne montre pas de gain en nombre de résultats trouvés par rapport à la référence.')
        else:
            st.info('Un avantage apparaît sur cet échantillon. Il reste à le confirmer sur de nouvelles journées.')
    with st.expander('Méthode et limites du test par match'):
        st.write('À chaque date, seuls les matchs des jours précédents de cette même compétition sont utilisés. '
                 'Les buts de seconde période sont calculés en soustrayant HT de FT pour chaque équipe. '
                 'Les profils domicile et extérieur sont séparés ; aucun résultat d’une autre ligue n’est utilisé.')
        st.write('Les moyennes de buts marqués et encaissés par période sont rapprochées de celles de la ligue '
                 'avec un poids fixe de 8 matchs. Une loi de Poisson indépendante pour chaque période transforme '
                 'ces moyennes en estimations 1st, Égal et 2nd. Ces estimations sont ensuite mélangées à la '
                 'tendance de la ligue, d’autant plus fortement que l’historique des équipes est court. '
                 'Les paramètres sont fixes et n’ont pas été optimisés sur ces tests.')
        st.caption('Les adversaires passés, compositions, blessures et cotes ne sont pas modélisés. '
                   'La source peut omettre des matchs et ses scores sont ceux relevés après les rencontres. '
                   'Ce test rétrospectif ne remplace pas un suivi de prédictions enregistrées avant les matchs.')
        if n:
            st.write(f"Erreur probabiliste moyenne (plus petit = mieux) : "
                     f"{summary['loss']:.3f} pour le modèle ; {summary['baseline_loss']:.3f} pour la référence.")
