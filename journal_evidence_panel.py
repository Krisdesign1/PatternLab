"""Read-only, protocol-scoped uncertainty beside descriptive journal results."""
from datetime import datetime, timezone
from math import isfinite

import streamlit as st

import journal_protocol as protocol_store
import journal_uncertainty as uncertainty


NAMES = {'frequency': 'Répétitions seules', 'clubs': 'Clubs seuls',
         'combined': 'Clubs + répétitions', 'catboost': 'CatBoost',
         'context_neighbors': 'Répétitions par contexte'}
STATES = {'insufficient': 'Données insuffisantes', 'inconclusive': 'Écart incertain',
          'favorable': 'Écart favorable observé', 'unfavorable': 'Écart défavorable observé'}


@st.cache_data(ttl=300, max_entries=32, show_spinner=False)
def _evaluate(entries, methods, protocol, week_signature, _now):
    # Cache complete cohorts, including result revisions and frozen versions.
    # A week change invalidates even a still-live cache entry; now stays the
    # actual clock of its calculation rather than an invented week boundary.
    return uncertainty.evaluate(entries, methods, protocol, now=_now)


def _value(value, scale=1):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int,float)) or not isfinite(value):
        raise ValueError('Valeur d’incertitude absente ou non finie.')
    return scale*value


def _interval(item, scale=1):
    lower, upper = _value(item.get('lower'), scale), _value(item.get('upper'), scale)
    if lower is None or upper is None:
        return 'Non disponible'
    return f'[{lower:+.4f} ; {upper:+.4f}]'


def _estimate(item, scale=1):
    value = _value(item.get('estimate'), scale)
    return 'Non disponible' if value is None else f'{value:+.4f}'


def render(entries, methods, *, protocol=None):
    """Add uncertainty without changing selected methods, cohorts or old scores."""
    st.subheader('Fiabilité des écarts entre méthodes', icon=':material/insights:')
    try:
        protocol = protocol_store.load_protocol() if protocol is None else protocol
        if protocol is None:
            st.info('Aucun protocole prospectif enregistré. Les résultats ci-dessus restent descriptifs ; '
                    'aucun intervalle d’incertitude n’est calculé.')
            return
        protocol = protocol_store.validate_protocol(protocol)
        registered = datetime.fromisoformat(protocol['registered_at'].replace('Z','+00:00'))
        registered = registered.astimezone(timezone.utc)
        st.caption(f"Protocole {protocol['version']} enregistré le {registered.strftime('%d/%m/%Y à %H:%M:%S UTC')}. "
                   'Seules les prévisions enregistrées à partir de cette date participent à ce suivi ; '
                   'les anciennes prévisions restent descriptives.')
        st.caption('Référence fixe : Clubs seuls. La réduction de l’erreur probabiliste est le critère principal ; '
                   'la réussite exacte dans les trois choix est un complément. Une valeur positive favorise la méthode comparée.')
        if 'clubs' not in methods:
            st.info('Ajoutez « Clubs seuls » aux méthodes à comparer pour mesurer les écarts avec la référence du protocole.')
            return
        if not any(method != 'clubs' for method in methods):
            st.info('Choisissez une autre méthode avec « Clubs seuls » pour consulter leurs écarts.')
            return
        now = datetime.now(timezone.utc)
        result = _evaluate(entries, list(methods), protocol, now.strftime('%G-W%V'), now)
        st.caption(f"{result.get('legacy_count', 0)} ancienne(s) prévision(s) descriptive(s), exclue(s) de ce suivi · "
                   f"{result.get('prospective_count', 0)} prévision(s) enregistrée(s) depuis le protocole dans la cohorte sélectionnée.")
        comparisons = result.get('comparisons') or []
        if not comparisons:
            st.info('Aucune comparaison disponible pour les méthodes sélectionnées dans ce protocole.')
            return
        if all(row.get('status') == 'insufficient' for row in comparisons):
            st.info('Le recul disponible est insuffisant pour estimer la fiabilité des écarts. '
                    'Les prochaines prévisions et leurs résultats alimenteront ce bilan ; '
                    'aucun gain n’est confirmé pour le moment.')
        rows = []
        for comparison in comparisons:
            loss = comparison.get('delta_loss') or {}
            top3 = comparison.get('delta_top3') or {}
            rows.append({'Méthode': NAMES.get(comparison['method'], comparison['method']),
                         'Référence': NAMES.get(comparison.get('baseline'), comparison.get('baseline', 'clubs')),
                         'Groupes comparés': comparison.get('groups', 0),
                         'Blocs de dépendance': comparison.get('blocks', 0),
                         'État': STATES.get(comparison.get('status'), 'Données insuffisantes'),
                         'Réduction d’erreur probabiliste': _estimate(loss),
                         'Intervalle à 95 % · erreur': _interval(loss),
                         'Écart dans les 3 (points de %)': _estimate(top3,100),
                         'Intervalle à 95 % · dans les 3 (points)': _interval(top3,100)})
        st.dataframe(rows, hide_index=True, width='stretch', key='journal_evidence_comparisons')
        for comparison in comparisons:
            excluded = comparison.get('excluded') or {}
            if comparison.get('reason'):
                st.caption(f"{NAMES.get(comparison['method'], comparison['method'])} : {comparison['reason']}")
            st.caption(f"{NAMES.get(comparison['method'], comparison['method'])} — hors calcul : "
                       f"{excluded.get('pending', 0)} en attente, {excluded.get('missing_methods', 0)} sans toutes les méthodes, "
                       f"{excluded.get('invalid', 0)} non évaluables, {excluded.get('open_week', 0)} dans des semaines encore ouvertes, "
                       f"{excluded.get('incomplete_block', 0)} dans des blocs encore incomplets, "
                       f"{excluded.get('void', 0)} annulés du suivi.")
        st.caption('Les blocs relient les semaines UTC et les groupes partageant des matchs. Les semaines encore ouvertes '
                   'et les blocs contenant des résultats en attente ou à vérifier sont exclus jusqu’à leur fermeture complète. '
                   f"Au moins {protocol.get('min_blocks', 20)} blocs sont requis ; les intervalles reposent sur "
                   f"{protocol.get('bootstrap_samples', 4000)} rééchantillonnages de ces blocs, pas sur des matchs supposés indépendants.")
        st.caption('Le minimum de 20 blocs est un seuil opérationnel et ne garantit pas une puissance statistique suffisante. '
                   'L’analyse suppose une indépendance approximative entre les blocs ; des liens sportifs peuvent subsister.')
        st.caption('Ces intervalles à 95 % sont descriptifs. Les consultations répétées et les comparaisons multiples '
                   'limitent leur interprétation ; un écart favorable observé ne confirme pas un gain prédictif '
                   'et ne promeut aucun modèle.')
        with st.expander('Règles du bilan et protocole enregistré'):
            st.markdown('Un écart favorable exige une réduction de l’erreur probabiliste dont tout l’intervalle est '
                        'positif, et un intervalle du top 3 sans recul. Un indicateur sans variation observée '
                        'ne suffit pas à établir une équivalence : son intervalle reste indisponible.')
            st.caption('Les comparaisons portent uniquement sur la cohorte sélectionnée : une compétition, '
                       'une taille et les mêmes versions. Un changement de filtre change la population décrite.')
            st.json(protocol, expanded=False)
    except (ValueError, OSError, KeyError, TypeError) as error:
        st.warning(f'Bilan d’incertitude indisponible : {error}')
