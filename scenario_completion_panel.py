"""Inspect observed completions; never infer an unseen suffix."""
import streamlit as st

from league_model import VALUES
from league_patterns import scenario_completions
import scenario_tables


def render_completions(result, league_key):
    size = result.get('size', 4)
    st.markdown('### Compléter un scénario commencé')
    st.write(f'Saisis les premiers résultats de ton groupe de {size} matchs. '
             'La recherche porte sur le **début des groupes complets de cette ligue**.')
    length = st.selectbox('Combien de résultats connais-tu déjà ?', list(range(1, size)),
                          key='completion_length' if size == 4 else f'completion_length_{size}')
    prefix = []
    for i in range(length):
        col = st
        prefix.append(col.selectbox(f'Résultat du match {i + 1}', VALUES, index=None,
                                    placeholder='Choisir…', key=f'completion_{league_key}_{length}_{i}'))
    if any(v is None for v in prefix):
        st.caption('Renseigne chaque résultat pour afficher les suites observées.')
        return
    found = scenario_completions(result, prefix)
    text = ' → '.join(prefix)
    scenario_tables.render_sequence(prefix,label='Début du scénario')
    st.caption(f'{size-length} résultat(s) encore inconnus dans ce groupe.')
    if not found['support']:
        st.info(f'**Aucun précédent trouvé** parmi les {found["total"]} groupes complets de cette ligue. '
                'Aucune suite n’est proposée. Cela ne signifie pas que ce scénario est impossible.')
        return
    st.write(f'Ce début apparaît dans **{found["support"]} groupes sur {found["total"]}**.')
    winners = [r for r in found['ranking'] if r['rank'] == 1]
    with st.container(border=True):
        st.markdown('**Suite la plus souvent observée**' if len(winners) == 1
                    else '**Plusieurs suites arrivent à égalité**')
        for winner in winners:
            scenario_tables.render_sequence(winner['suffix'])
            st.write(f"{winner['count']} fois sur {found['support']} groupes avec ce début.")
    if found['support'] == 1:
        st.caption('Un seul précédent : cette suite n’a pas encore été observée plusieurs fois avec ce début.')
    st.caption('Ce sont des apparitions passées, pas des chances de réussite pour le prochain groupe. '
               'Les groupes incomplets ne servent pas de précédents.')
    st.dataframe([{'Suite observée': ' → '.join(r['suffix']), 'Apparitions': r['count'],
                   'Sur les groupes avec ce début': found['support'],
                   'Scénario complet': text + ' → ' + ' → '.join(r['suffix']),
                   'Groupes à vérifier': ', '.join(map(str, r['blocks']))}
                  for r in found['ranking']], hide_index=True, width='stretch')
    st.caption(f'Pour retrouver les matchs, ouvre « Vérifier les {size} matchs d’un groupe » plus bas.')
