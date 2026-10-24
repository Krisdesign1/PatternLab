"""Read-only paired model comparison, computed from saved forecasts alone."""
import streamlit as st
from hashlib import sha256

import journal_protocol
import prediction_journal as journal
from probability_reliability_panel import _storage_signature
import saved_model_comparison as comparison


NAMES = {'frequency':'Répétitions', 'clubs':'Clubs', 'catboost':'CatBoost'}


@st.cache_data(ttl=60, max_entries=4, show_spinner=False)
def _load(path, signature, protocol, version):
    return comparison.build(journal.list_entries(path), protocol)


def render():
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
    st.caption(f"{exclusions.get('before_protocol',0)} groupe(s) antérieur(s) au protocole · "
               f"{exclusions.get('missing_catboost',0)} sans CatBoost · "
               f"{exclusions.get('invalid_snapshot',0)} incompatible(s) · "
               f"{report['duplicates_removed']} répétition(s) de matchs retirée(s) dans leur cohorte.")
    if not report['cohorts']:
        st.info('Aucune cohorte comparable disponible. Une méthode manquante ou un résultat en attente '
                'ne compte jamais comme un échec. Les prochaines prévisions alimenteront ce suivi.')
        return
    index = st.selectbox('Cohorte de comparaison', range(len(report['cohorts'])),
        format_func=lambda i: f"Compétition {report['cohorts'][i]['league']} · "
            f"groupes de {report['cohorts'][i]['size']} matchs · versions {i+1}",
        key='saved_model_comparison_cohort')
    cohort = report['cohorts'][index]
    with st.container(horizontal=True):
        st.metric('Matchs comparés',cohort['evaluated'])
        st.metric('En attente',cohort['pending'])
        st.metric('Résultats exclus',cohort['excluded'])
    st.dataframe([{'Méthode':NAMES[method],
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
                 key='saved_model_comparison_scores')
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
        st.dataframe(categories,hide_index=True,width='stretch',key='saved_model_comparison_categories')
    st.caption('Chaque match compte une fois dans cette cohorte : son premier enregistrement commun '
               'aux trois méthodes est conservé, même s’il attend encore un résultat. Les probabilités '
               'par match sont les marginales exactes des distributions de scénarios sauvegardées.')
    st.caption('Une erreur probabiliste ou un score de Brier plus petit est meilleur. Ces chiffres '
               'restent descriptifs : les matchs peuvent être liés et les petits effectifs sont fragiles. '
               'Ce bilan ne confirme aucun avantage prédictif et ne remplace ni ne réentraîne un modèle. '
               'Le suivi d’incertitude par groupes reste disponible dans Mes analyses.')
