"""Read-only display of the reliability of saved Clubs match probabilities."""
from pathlib import Path

import streamlit as st

import prediction_journal as journal
import probability_reliability as reliability


def _storage_signature(path):
    """Include WAL updates; absent storage must remain absent on a read path."""
    result = []
    for name in (str(path), str(path) + '-wal'):
        try:
            stat = Path(name).stat()
            result.append((stat.st_size, stat.st_mtime_ns, stat.st_ino))
        except FileNotFoundError:
            result.append(None)
    return tuple(result)


@st.cache_data(ttl=60, max_entries=4, show_spinner=False)
def _load(path, storage_signature, method_version):
    return reliability.build(journal.list_entries(path))


def _table_rows(report, label):
    return [{'Probabilité annoncée': row['range'],
             'Matchs évalués': row['n'], 'Réalisations': row['hits'],
             'Moyenne annoncée (%)': None if row['announced'] is None else 100 * row['announced'],
             'Taux observé (%)': None if row['observed_rate'] is None else 100 * row['observed_rate'],
             'Écart observé − annoncé (points)': row['gap_points'],
             'En attente': row['pending'], 'Exclus': row['excluded']}
            for row in report['results'][label]['bins']]


def render_probability_reliability():
    """Render a lazy, local-only calibration check in the existing page."""
    box = st.expander('Les pourcentages annoncés se réalisent-ils ?',
                      icon=':material/fact_check:', key='hp_reliability_open', on_change='rerun')
    if not box.open:
        return
    with box:
        st.caption('Prévisions Clubs enregistrées avant match, toutes compétitions confondues. '
                   'Chaque match compte une fois par version : sa première prévision est conservée, '
                   'y compris lorsqu’elle attend encore son résultat.')
        path = Path(journal.DB_PATH)
        try:
            report = _load(str(path), _storage_signature(path), reliability.VERSION)
        except (journal.JournalError, OSError):
            st.warning('Le journal local ne peut pas être lu. Aucun taux n’a été calculé.')
            return
        excluded = sum(report['snapshot_exclusions'].values())
        if not report['versions']:
            st.info('Aucune probabilité Clubs par match exploitable n’est encore enregistrée avant le coup d’envoi.')
            if excluded:
                st.caption(f'{excluded} entrée(s) ou prévision(s) écartée(s) : provenance, distribution ou horaire inexploitable.')
            return
        versions = {row['key']: row for row in report['versions']}
        key = st.selectbox('Version des probabilités Clubs', list(versions), key='hp_reliability_version')
        selected = versions[key]
        with st.container(horizontal=True):
            st.metric('Matchs évalués', str(selected['evaluated']))
            st.metric('Résultats en attente', str(selected['pending']))
            st.metric('Matchs exclus', str(selected['excluded']))
        if not selected['evaluated']:
            st.info('Aucun résultat exploitable pour cette version. Les prévisions en attente ne comptent pas comme des échecs.')
        label = st.segmented_control('Résultat à vérifier', ['1st', 'Égal', '2nd'], default='1st',
                                     key='hp_reliability_outcome') or '1st'
        st.caption('Ce bilan utilise tous les pourcentages de la catégorie choisie, même lorsqu’elle '
                   'n’est pas favorite. Exemple : parmi les annonces de 50 à moins de 60 %, '
                   'le taux observé indique la fréquence réelle de cette catégorie.')
        table = _table_rows(selected, label)
        for row in table:
            for name in ('Moyenne annoncée (%)', 'Taux observé (%)', 'Écart observé − annoncé (points)'):
                value = row[name]
                row[name] = ('—' if value is None else f'{value:+.2f}' if name.startswith('Écart')
                             else f'{value:.2f}')
        st.dataframe(table, hide_index=True, width='stretch', key='hp_reliability_bins')
        st.caption('Tranches fermées à gauche et ouvertes à droite ; 100 % appartient à la dernière. '
                   'Un tiret indique l’absence de match évalué. Un écart positif signifie une fréquence '
                   'observée supérieure à la moyenne annoncée. Ces statistiques sont descriptives ; '
                   'les petits effectifs restent fragiles.')
        st.caption(f"{report['duplicates_removed']} répétition(s) retirée(s) entre groupes ; "
                   f'{excluded} entrée(s) ou prévision(s) incompatible(s) écartée(s). '
                   'Résultats issus du dernier rapprochement déjà enregistré de la première prévision ; '
                   'aucune actualisation API ni modification du journal à l’ouverture de ce bilan.')
