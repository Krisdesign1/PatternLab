"""Simple guide to actual repeated scenarios per competition."""
import pandas as pd
import streamlit as st
from coupon_sizes import SIZES

from league_model import VALUES
import match_sources
from league_patterns import repeated_scenarios
from scenario_completion_panel import render_completions
from scenario_recurrence import recurrence_diagnostics
import scenario_tables
from scenario_presentation import render_match_results,result_column

MEANINGS = {'1st': 'Plus de buts en première mi-temps',
            'Égal': 'Autant de buts dans les deux mi-temps',
            '2nd': 'Plus de buts en seconde mi-temps'}


@st.cache_data(max_entries=8, show_spinner=False)
def prepare_patterns(coupons, signature, size=4, source='API-Football'):
    if source != 'API-Football':
        raise ValueError('Seules les données API-Football sont utilisées.')
    rows, audit = match_sources.load_matches([], source)
    return repeated_scenarios(rows, size), audit


def seq_text(sequence):
    return ' → '.join(sequence)


def match_table(matches):
    return [{'Place': i + 1, 'Date': r['timestamp'].replace('T', ' '),
             'Match': f"{r['home']} — {r['away']}", 'Score à la pause': r['ht'],
             'Score final': r['ft'], 'Résultat': r['value'], 'Identifiant du match': r['match_id']}
            for i, r in enumerate(matches)]


def ranking_table(result):
    total = len(result['blocks'])
    diagnostic = {row['sequence']: row for row in recurrence_diagnostics(result)}
    return [{'Rang': r['rank'], 'Scénario observé': seq_text(r['sequence']),
             'Apparitions': r['count'], 'Sur combien de groupes ?': total,
             'Attendu (positions indépendantes)': diagnostic[r['sequence']]['expected'],
             'Rapport observé / attendu': diagnostic[r['sequence']]['ratio'],
             'Écart en apparitions': diagnostic[r['sequence']]['excess'],
             'Groupes concernés': ', '.join(map(str, r['blocks']))}
            for r in result['ranking']]


def render_leagues(coupons, *, expert=True, default_source='API-Football'):
    if expert:
        st.subheader('Les scénarios qui se répètent dans chaque ligue')
        st.write('Un scénario = les résultats de **2 à 20 matchs successifs d’une même ligue**. '
                 'Choisis une ligue pour voir quelles séquences reviennent le plus souvent.')
    with st.expander('Comprendre 1st, Égal et 2nd'):
        for value in VALUES:
            color={'1st':'blue','Égal':'violet','2nd':'orange'}[value]
            st.markdown(f':{color}-badge[{value}] — {MEANINGS[value]}.')
        st.write('Exemple : 1–0 à la pause, puis 2–1 à la fin. Il y a eu 1 but avant la pause '
                 'et 2 après : le résultat est **2nd**. Égal ne signifie pas forcément un match nul.')
    source = 'API-Football'
    st.caption('Historique API-Football uniquement · aucune saisie manuelle n’entre dans cette analyse.')
    size = st.selectbox("Matchs par groupe d’analyse", SIZES, index=2, key="league_group_size")
    signature = match_sources.data_signature(source)
    try:
        patterns, audit = prepare_patterns(coupons, signature, size, source)
    except match_sources.SOURCE_ERRORS as error:
        st.error(str(error))
        return
    if not patterns:
        st.info('Aucun résultat HT/FT API-Football utilisable. Importez une saison depuis Données et réglages.')
        if audit['issues']:
            st.dataframe(audit['issues'], hide_index=True)
        return
    st.caption(f'Source {source} uniquement · horaires en UTC · scores à 90 minutes, hors prolongations et tirs au but.')
    if audit['issues']:
        st.warning(f"{len(audit['issues'])} match(s) exclus après vérification. Voir les détails en bas de page.")
    keys = sorted(patterns, key=lambda k: (-len(patterns[k]['blocks']),
                  patterns[k]['matches'][0]['country'], patterns[k]['matches'][0]['competition']))
    labels = {k: f"{patterns[k]['matches'][0]['country']} — {patterns[k]['matches'][0]['competition']}" for k in keys}
    selected = st.selectbox('Choisis une ligue ou une coupe', keys, format_func=labels.get,
                            key='league_detail', help='Tape le pays ou le nom de la compétition pour la retrouver.')
    result = patterns[selected]
    total = len(result['blocks'])
    pending = len(result['pending'])
    st.caption(f"{len(result['matches'])} matchs disponibles → {total} groupes complets de {size} matchs"
               + (f' · {pending} résultat(s) hors groupe complet.' if pending else '.'))
    if not total:
        st.info(f'Il faut encore {size - pending} match(s) dans cette ligue pour former le premier groupe de {size} matchs.')
    else:
        winners = [r for r in result['ranking'] if r['rank'] == 1]
        top_count = winners[0]['count']
        with st.container(border=True):
            if top_count == 1:
                st.markdown('### Aucun scénario ne s’est encore répété')
                st.write(f'Les **{total} groupes** ont chacun un scénario différent.' if total > 1
                         else 'Un seul groupe est disponible. Il faut d’autres groupes pour observer une répétition.')
            else:
                st.markdown('### Le scénario le plus répété' if len(winners) == 1
                            else f'### {len(winners)} scénarios arrivent en tête à égalité')
                for winner in winners:
                    scenario_tables.render_sequence(winner['sequence'])
                st.write(f'Observé **{top_count} fois sur {total} groupes**'
                         + (' pour chacun.' if len(winners) > 1 else '.'))
                st.caption(f'« {top_count} fois » compte toutes les apparitions, y compris la première.')
        st.markdown('### Classement des scénarios réellement observés')
        table = ranking_table(result)
        display=[dict(row,**{'Scénario observé':list(item['sequence'])})
                 for row,item in zip(table,result['ranking'])]
        st.dataframe(display,hide_index=True,width='stretch',column_config={
            'Scénario observé':result_column('Scénario observé',width='large')})
        st.caption('Un rang identique indique une égalité. La première valeur correspond au premier match '
                   'du groupe, la deuxième au deuxième match, etc. Ce classement décrit cet historique ; '
                   'il ne donne pas la probabilité du prochain groupe de matchs.')
        st.caption('Attendu : nombre d’apparitions expliqué par les fréquences de chaque position, '
                   'en supposant les positions indépendantes. Rapport 1 : autant qu’attendu ; '
                   'rapport supérieur à 1 : davantage observé. Un historique constant donne un rapport de 1. '
                   'Cette comparaison descriptive ne constitue pas un test de significativité ; '
                   'les motifs rares et la recherche de nombreuses combinaisons peuvent produire de grands rapports.')
        st.download_button('Télécharger ce classement', pd.DataFrame(table).to_csv(index=False).encode('utf-8-sig'),
                           'scenarios_observes_par_ligue.csv', 'text/csv', key='league_pattern_export')
        with st.expander(f'Vérifier les {size} matchs d’un groupe'):
            number = st.selectbox('Groupe à examiner', [b['number'] for b in result['blocks']],
                                  key=f'league_block_{selected}' if size == 4 else f'league_block_{selected}_{size}')
            block = result['blocks'][number - 1]
            st.markdown(f'**Groupe {number}**')
            scenario_tables.render_sequence(block['sequence'])
            render_match_results(match_table(block['matches']))
    if pending:
        with st.expander(f'Voir les {pending} match(s) du groupe incomplet'):
            st.caption('Ces matchs ne sont pas comptés dans le classement des scénarios.')
            render_match_results(match_table(result['pending']))
    with st.expander('Compléter un scénario commencé'):
        render_completions(result, selected if size == 4 else f'{selected}_{size}')
    if result['simultaneous']:
        st.caption('Certains matchs commencent à la même heure. Leur ordre est fixé par leur identifiant '
                   f'{source} croissant ; le classement dépend de cette convention.')
    with st.expander('Comment les groupes sont-ils formés ?'):
        st.write('Pour chaque ligue, on part du plus ancien match disponible et on trie par date et heure '
                 f'de début de la source {source}. À heure identique, on trie par identifiant {source} croissant. '
                 f'On prend les matchs 1 à {size}, puis {size+1} à {2*size}, et ainsi de suite. '
                 'Un match appartient à un seul groupe. Un groupe peut traverser plusieurs journées.')
        st.write('Seuls les matchs importés, vérifiés et encore présents dans cette source sont utilisés. '
                 'Des matchs peuvent manquer : ce relevé n’est pas forcément l’historique complet de la ligue. '
                 'Ajouter des matchs plus anciens ou supprimer un match peut modifier le découpage et le classement.')
        st.caption('Ces groupes sont construits uniquement à partir des résultats API-Football.')
    with st.expander('Comparer les ligues'):
        overview = []
        for key in keys:
            r = patterns[key]
            top = r['ranking'][0]['count'] if r['ranking'] else 0
            overview.append({'Ligue': labels[key], 'Groupes complets': len(r['blocks']),
                             'Résultats hors groupe complet': len(r['pending']),
                             'Maximum d’apparitions': top,
                             'Scénarios en tête': ' / '.join(seq_text(x['sequence']) for x in r['ranking']
                                                             if x['rank'] == 1) if top > 1 else 'Aucune répétition'})
        st.dataframe(overview, hide_index=True, width='stretch')
    with st.expander('Vérification des données'):
        st.write(f"**{audit['matched']} matchs sur {audit['expected']}** associés à leurs scores {source}.")
        if audit['issues']:
            st.dataframe(audit['issues'], hide_index=True)
