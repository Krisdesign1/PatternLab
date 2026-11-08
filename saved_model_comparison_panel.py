"""Read-only paired model comparison, computed from saved forecasts alone."""
import table_views
import streamlit as st
from hashlib import sha256

import journal_protocol
import prediction_journal as journal
from probability_reliability_panel import _storage_signature
import saved_model_comparison as comparison
from group_size_filter import matches, render as render_group_size_filter


NAMES = {'frequency':'Répétitions seules', 'clubs':'Clubs', 'combined':'Clubs + répétitions',
         'catboost':'CatBoost', 'context_neighbors':'Contextes voisins'}
RESULT_REASONS={'void':'Groupe annulé','unknown_result_status':'Statut du résultat inconnu',
    'invalid_observed':'Observation invalide','missing_or_unresolved_result':'Résultat absent ou non résolu',
    'invalid_or_conflicting_result':'Scores invalides ou contradictoires',
    'observation_after_bilan':'Observation postérieure au bilan'}


@st.cache_data(ttl=60, max_entries=4, show_spinner=False)
def _load(path, signature, protocol, version):
    return comparison.build(journal.list_entries(path), protocol)


def _reset_cohort_index(key, count):
    index = st.session_state.get(key, 0)
    if type(index) is not int or not 0 <= index < count:
        st.session_state[key] = 0


def _render_strict():
    selected_size = render_group_size_filter('saved_model_comparison_group_size')
    st.subheader('Comparer les modèles sur les mêmes matchs', icon=':material/balance:')
    st.caption('Prévisions réellement enregistrées avant match, depuis le protocole prospectif. '
               'La comparaison exige Répétitions, Clubs et CatBoost dans le même enregistrement. '
               'Les calculs historiques reconstruits ne sont pas ajoutés à ce bilan.')
    try:
        protocol = journal_protocol.load_protocol()
        if protocol is None:
            st.info('Aucun protocole prospectif enregistré. Aucun taux comparatif n’est calculé.')
            return
        path = str(journal.DB_PATH)
        report = _load(path, _storage_signature(path), protocol, comparison.VERSION)
    except (ValueError, OSError, journal.JournalError):
        st.warning('La comparaison des modèles ne peut pas être lue. Aucune prévision n’a été recalculée.')
        return
    st.caption(f"Protocole {report['protocol_version']} enregistré le {report['registered_at']}. "
               'Les compétitions, tailles et versions restent séparées.')
    exclusions = report['snapshot_exclusions']
    st.caption('Exclusions et doublons : comptes globaux, toutes tailles confondues.')
    st.caption(f"{exclusions.get('before_protocol',0)} groupe(s) antérieur(s) au protocole · "
               f"{exclusions.get('missing_catboost',0)} sans CatBoost · "
               f"{exclusions.get('invalid_snapshot',0)} incompatible(s) · "
               f"{report['duplicates_removed']} répétition(s) de matchs retirée(s) dans leur cohorte.")
    cohorts = [cohort for cohort in report['cohorts'] if matches(cohort.get('size'), selected_size)]
    if not cohorts:
        st.info('Aucun groupe correspondant à cette taille dans les cohortes comparables.' if selected_size is not None
                else 'Aucune cohorte comparable disponible. Une méthode manquante ou un résultat en attente '
                'ne compte jamais comme un échec. Les prochaines prévisions alimenteront ce suivi.')
        return
    _reset_cohort_index('saved_model_comparison_cohort', len(cohorts))
    index = st.selectbox('Cohorte de comparaison', range(len(cohorts)),
        format_func=lambda i: f"Compétition {cohorts[i]['league']} · "
            f"groupes de {cohorts[i]['size']} matchs · versions {i+1}",
        key='saved_model_comparison_cohort')
    cohort = cohorts[index]
    with st.container(horizontal=True):
        st.metric('Matchs comparés',cohort['evaluated'])
        st.metric('En attente',cohort['pending'])
        st.metric('Résultats exclus',cohort['excluded'])
    table_views.dataframe([{'Méthode':NAMES[method],
                   'Version':cohort['versions'][method]['version'],
                   'Génération':cohort['versions'][method]['generation'] or ('Composants '+
                       sha256(cohort['versions'][method]['components'].encode()).hexdigest()[:12]
                       if cohort['versions'][method]['components'] else '—'),
                   'Matchs identiques':cohort['scores'][method]['evaluated'],
                   'Premier choix correct (%)':None if cohort['scores'][method]['accuracy'] is None
                       else 100*cohort['scores'][method]['accuracy'],
                   'Erreur probabiliste ↓':cohort['scores'][method]['logloss'],
                   'Score de Brier ↓':cohort['scores'][method]['brier']}
                  for method in comparison.METHODS],hide_index=True,width='stretch',
                 column_config={
                     'Erreur probabiliste ↓':st.column_config.NumberColumn(format='%.4f'),
                     'Score de Brier ↓':st.column_config.NumberColumn(format='%.4f'),
                     'Premier choix correct (%)':st.column_config.NumberColumn(format='%.2f')},
                 key='saved_model_comparison_scores', table_id='saved_model_comparison_panel:_render_strict:dataframe:1')
    label = st.segmented_control('Catégorie à comparer',['1st','Égal','2nd'],
                                 default='1st',required=True,key='saved_model_comparison_outcome')
    categories = []
    for method in comparison.METHODS:
        for row in cohort['scores'][method]['categories']:
            if row['label'] == label:
                categories.append({'Méthode':NAMES[method],
                    'Probabilité moyenne (%)':100*row['mean_probability'],
                    'Fréquence réellement observée (%)':100*row['observed_rate'],
                    'Choisi en premier':row['predicted_count'],
                    'Premiers choix réalisés':row['predicted_hits'],
                    'Brier de cette catégorie ↓':row['brier']})
    if categories:
        table_views.dataframe(categories,hide_index=True,width='stretch',key='saved_model_comparison_categories', table_id='saved_model_comparison_panel:_render_strict:dataframe:2')
    st.caption('Chaque match compte une fois dans cette cohorte : son premier enregistrement commun '
               'aux trois méthodes est conservé, même s’il attend encore un résultat. Les probabilités '
               'par match sont les marginales exactes des distributions de scénarios sauvegardées.')
    st.caption('Une erreur probabiliste ou un score de Brier plus petit est meilleur. Ces chiffres '
               'restent descriptifs : les matchs peuvent être liés et les petits effectifs sont fragiles. '
               'Ce bilan ne confirme aucun avantage prédictif et ne remplace ni ne réentraîne un modèle. '
               'Le suivi d’incertitude par groupes reste disponible dans Mes analyses.')


@st.cache_data(ttl=60, max_entries=4, show_spinner=False)
def _load_pairs(path, signature, protocol, version):
    return comparison.build_pairs(journal.list_entries(path),protocol)


def _interval(gain):
    if gain['estimate'] is None:
        return '—'
    point = f"{gain['estimate']:+.4f}"
    if gain['lower'] is None:
        return point+' · intervalle indisponible'
    return point+f" [{gain['lower']:+.4f} ; {gain['upper']:+.4f}]"


def _cohort_order(cohort):
    """Prioritize the amount of observed evidence, never a model's score."""
    versions=tuple((name,value['version'],value['generation'],value['components'])
                   for name,value in sorted(cohort['versions'].items()))
    return (-cohort['evaluated'],-cohort['dates'],int(cohort['league']),cohort['size'],versions)


def _explanations(report,protocol,challenger=None,cohort=None):
    with st.expander('Comprendre ce bilan et ses exclusions',icon=':material/info:'):
        st.caption(f"Prévisions enregistrées depuis le {report['registered_at']}. "
                   'La référence Répétitions seules est la distribution de scénarios figée pour la '
                   'compétition, sans informations des clubs; aucune fréquence n’est recréée après les résultats.')
        st.caption('Cette analyse complémentaire compare les probabilités par match. Le protocole '
                   'initial Clubs / top 3 des groupes reste inchangé; il compare notamment Clubs + '
                   'répétitions à Clubs pour étudier l’apport des répétitions.')
        exclusions=report['snapshot_exclusions']
        st.caption(f"{exclusions.get('before_protocol',0)} groupe(s) antérieur(s) au protocole · "
                   f"{exclusions.get('missing_frequency',0)} sans référence sauvegardée · "
                   f"{exclusions.get('invalid_snapshot',0)} enregistrement(s) incompatible(s) · "
                   f"{report['duplicates_removed']} doublon(s) de matchs retiré(s) dans leurs paires/cohortes.")
        if challenger is not None:
            st.caption(f"{exclusions.get('missing_'+challenger,0)} groupe(s) sans {NAMES[challenger]} · "
                       f"{exclusions.get('invalid_'+challenger,0)} paire(s) incompatible(s). "
                       'L’absence d’une autre méthode ne bloque pas cette paire.')
        st.caption('Le premier enregistrement commun est conservé par match, compétition, taille, '
                   'versions et générations, même s’il attend un résultat. Une méthode absente ou '
                   'un résultat en attente ne compte pas comme un échec. Les cohortes sont triées par '
                   'nombre de matchs évalués puis de dates, jamais par performance.')
        st.caption('Les scores utilisent les mêmes matchs au sein d’une paire. Les cohortes peuvent '
                   'partager des matchs entre elles : leurs effectifs et scores ne doivent pas être additionnés.')
        coverage=report['coverage']
        st.caption(f"Couverture globale : {coverage['unique_matches']} matchs uniques dans "
                   f"{coverage['comparable_groups']} groupes admissibles; "
                   f"{coverage['overlapping_matches']} matchs figurent dans plusieurs groupes. "
                   f"Les {coverage['group_match_appearances']} apparitions match/groupe et "
                   f"{coverage['cohort_match_observations']} observations par paire/cohorte "
                   'ne constituent pas autant de matchs indépendants.')
        st.caption(f"Pour ces matchs uniques, le premier enregistrement comparable donne "
                   f"{coverage['evaluated_unique_matches']} évalués, "
                   f"{coverage['pending_unique_matches']} en attente et "
                   f"{coverage['excluded_unique_matches']} exclus. Ce sont des comptes de couverture, "
                   'pas un taux global de réussite.')
        st.caption('Sont exclus du calcul : source ou identifiants incompatibles, enregistrement '
                   'antérieur au protocole ou postérieur au coup d’envoi, distribution ou classement '
                   'invalide, référence absente, coupure historique non vérifiable. Les absences de '
                   'méthodes sont comptées par paire et leurs compteurs peuvent se chevaucher.')
        if cohort is not None:
            estimate=cohort['uncertainty']
            st.caption(estimate['reason'])
            if cohort['result_exclusions']:
                st.caption('Résultats exclus dans cette cohorte : '+', '.join(
                    f"{RESULT_REASONS.get(name,'Résultat inexploitable')} : {count}"
                    for name,count in sorted(cohort['result_exclusions'].items())))
            st.caption(f"{estimate['matches']} match(s) dans les blocs clos complets · "
                       f"{estimate['excluded'].get('open_week',0)} dans une semaine encore ouverte · "
                       f"{estimate['excluded'].get('incomplete_block',0)} dans un bloc incomplet. "
                       'Les scores principaux incluent tous les résultats disponibles; les intervalles '
                       'utilisent uniquement les blocs clos complets.')
        st.caption(f"Minimum de {protocol['min_blocks']} blocs hebdomadaires clos, sans garantie de puissance. "
                   'Intervalles descriptifs : dépendances résiduelles possibles et comparaisons multiples '
                   'non corrigées. Aucun avantage confirmé ni promotion automatique.')
        st.caption('Probabilités marginales exactes des scénarios sauvegardés. Aucun appel API, '
                   'entraînement, changement du protocole ou réécriture du journal.')


def _render_pairs():
    selected_size = render_group_size_filter('saved_model_comparison_group_size')
    st.subheader('Les clubs apportent-ils plus que les répétitions seules ?',icon=':material/balance:')
    st.caption('Scores descriptifs de prévisions figées avant match, comparées sur les mêmes rencontres.')
    try:
        protocol = journal_protocol.load_protocol()
        if protocol is None:
            st.info('Aucun protocole prospectif enregistré. Aucun taux comparatif n’est calculé.')
            return
        path = str(journal.DB_PATH)
        report = _load_pairs(path,_storage_signature(path),protocol,comparison.PAIRED_VERSION)
    except (ValueError,OSError,journal.JournalError):
        st.warning('Le bilan prospectif ne peut pas être lu. Aucune donnée n’a été modifiée.')
        return
    coverage=report['coverage']
    st.caption(f"Couverture globale, toutes tailles confondues : {coverage['unique_matches']} matchs uniques · "
               f"{coverage['comparable_groups']} groupes admissibles · "
               f"{coverage['overlapping_matches']} matchs communs à plusieurs groupes. "
               f"{coverage['mature_cohorts']}/{coverage['cohorts']} cohortes atteignent le minimum "
               'de recul; aucun gain prédictif confirmé.')
    matching_cohorts = [cohort for cohort in report['cohorts'] if matches(cohort.get('size'), selected_size)]
    if not matching_cohorts:
        st.info('Aucun groupe correspondant à cette taille dans les paires comparables.' if selected_size is not None
                else 'Pas encore de paire exploitable enregistrée avant match depuis le protocole. '
                'Une méthode absente ou un résultat en attente ne compte pas comme un échec.')
        _explanations(report,protocol)
        return
    available = [name for name in comparison.CHALLENGERS
                 if any(cohort['challenger']==name for cohort in matching_cohorts)]
    challenger = st.selectbox('Méthode face aux répétitions seules',available,
        format_func=lambda name:NAMES[name],key='saved_pair_challenger')
    cohorts = sorted((cohort for cohort in matching_cohorts if cohort['challenger']==challenger),
                     key=_cohort_order)
    _reset_cohort_index('saved_pair_cohort', len(cohorts))
    index = st.selectbox('Compétition, taille et versions',range(len(cohorts)),
        format_func=lambda i:f"Compétition {cohorts[i]['league']} · {cohorts[i]['size']} matchs · "
                            f"{cohorts[i]['evaluated']} évalués · cohorte {i+1}",
        key='saved_pair_cohort')
    cohort = cohorts[index]
    _explanations(report,protocol,challenger,cohort)
    with st.container(horizontal=True):
        st.metric('Matchs identiques évalués',cohort['evaluated'])
        st.metric('Dates représentées',cohort['dates'])
        st.metric('En attente',cohort['pending'])
        st.metric('Exclus',cohort['excluded'])
    names = ('frequency',challenger)
    table_views.dataframe([{'Méthode':NAMES[name], 'Matchs identiques':cohort['scores'][name]['evaluated'],
                   'Erreur probabiliste ↓':cohort['scores'][name]['logloss'],
                   'Brier multiclasses ↓':cohort['scores'][name]['brier'],
                   'Premier choix correct (%)':None if cohort['scores'][name]['accuracy'] is None
                       else 100*cohort['scores'][name]['accuracy'],
                   'Version':cohort['versions'][name]['version'],
                   'Génération':cohort['versions'][name]['generation'] or ('Composants '+
                       sha256(cohort['versions'][name]['components'].encode()).hexdigest()[:12]
                       if cohort['versions'][name]['components'] else '—')}
                  for name in names],hide_index=True,width='stretch',key='saved_pair_scores',
                 column_config={
                     'Erreur probabiliste ↓':st.column_config.NumberColumn(format='%.4f'),
                     'Brier multiclasses ↓':st.column_config.NumberColumn(format='%.4f'),
                     'Premier choix correct (%)':st.column_config.NumberColumn(format='%.2f')}, table_id='saved_model_comparison_panel:_render_pairs:dataframe:1')
    estimate = cohort['uncertainty']
    st.subheader('Recul disponible et incertitude')
    st.metric('Blocs hebdomadaires clos',f"{estimate['blocks']} / {protocol['min_blocks']}")
    if estimate['status']=='insufficient':
        st.warning('Recul insuffisant pour estimer les intervalles; aucun avantage prédictif établi.')
    else:
        st.info('Intervalles descriptifs par semaines closes; aucun avantage prédictif confirmé.')
    if estimate['matches']:
        table_views.dataframe([{'Gain du challenger':label,
                   'Estimation et intervalle descriptif à 95 %':_interval(estimate['gains'][name])}
                  for name,label in [('logloss','Réduction de l’erreur probabiliste'),
                                     ('brier','Réduction du Brier'),
                                     ('accuracy','Gain de premiers choix corrects (proportion)')]],
                     hide_index=True,width='stretch',key='saved_pair_intervals', table_id='saved_model_comparison_panel:_render_pairs:dataframe:2')
        st.caption('Un gain positif favorise la méthode testée, sur les seuls blocs clos complets.')
    if not st.toggle('Afficher la calibration des probabilités',key='saved_pair_show_calibration'):
        return
    st.subheader('Les probabilités annoncées se réalisent-elles ?')
    label = st.segmented_control('Résultat à vérifier',list(comparison.VALUES),default='1st',
        required=True,key='saved_pair_outcome')
    st.caption('Tous les pourcentages de la catégorie comptent, même si elle n’est pas favorite. '
               'Les tranches vides restent visibles; elles n’indiquent pas 0 % de réussite.')
    rows = []
    for name in names:
        for row in cohort['calibration'][name][label]:
            rows.append({'Méthode':NAMES[name],'Probabilité annoncée':row['range'],
                         'Matchs évalués':row['n'],
                         'Moyenne annoncée (%)':None if row['announced'] is None else 100*row['announced'],
                         'Taux observé (%)':None if row['observed_rate'] is None else 100*row['observed_rate'],
                         'En attente':row['pending'],'Exclus':row['excluded']})
    table_views.dataframe(rows,hide_index=True,width='stretch',key='saved_pair_calibration',column_config={
        'Moyenne annoncée (%)':st.column_config.NumberColumn(format='%.2f'),
        'Taux observé (%)':st.column_config.NumberColumn(format='%.2f')}, table_id='saved_model_comparison_panel:_render_pairs:dataframe:3')


def render():
    view = st.segmented_control('Comparaison des prévisions',['pairs','strict'],
        format_func=lambda value:{'pairs':'Face aux répétitions seules','strict':'Trois méthodes communes'}[value],
        default='pairs',required=True,key='saved_model_comparison_view')
    if view == 'strict':
        _render_strict()
    else:
        _render_pairs()
