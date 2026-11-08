"""Shared native display of result blockages, without a reconciliation action."""
import table_views
import streamlit as st

from result_explanations import annotate_entries


def render_explanation(entry, *, key='result_explanation'):
    explanation = entry.get('result_explanation')
    if not isinstance(explanation, dict):
        explanation = annotate_entries([entry])[0]['result_explanation']
    blocking = explanation.get('blocking') or []
    if not blocking:
        return
    st.warning(explanation['summary'])
    table_views.dataframe([{'Match': f"{row.get('home', '—')} — {row.get('away', '—')}",
                   'État du match': row['label'], 'Statut API local': row.get('raw_status') or '—',
                   'Motif': row['reason']} for row in blocking],
                 hide_index=True, key=key, width='stretch', table_id='result_explanations_panel:render_explanation:dataframe:1')
    st.caption('Motifs issus des observations enregistrées et du dernier relevé local disponible. '
               'Cette consultation ne modifie ni les prévisions ni leurs verdicts. '
               'Un report, un forfait ou un score manquant ne compte pas comme une prédiction perdue.')
    if explanation.get('local_unavailable'):
        st.caption('Le relevé local est indisponible ; seuls les motifs déjà conservés ont pu être utilisés.')


def render_pending_overview(load_entries, *, key, root=None, now=None):
    """Lazy explanatory view, deliberately separate from a statistical cohort."""
    box = st.expander('Pourquoi certains résultats ne sont-ils pas évalués ?',
                      key=key, on_change='rerun')
    if not box.open:
        return
    with box:
        try:
            entries = [entry for entry in load_entries() if entry.get('status') != 'settled']
            entries = annotate_entries(entries, root=root, now=now)
        except (OSError, ValueError):
            st.warning('Le détail des résultats non évalués est indisponible. Les taux ne sont pas modifiés.')
            return
        matches = {}
        for entry in entries:
            for row in entry['result_explanation']['blocking']:
                identity = (row['match_id'], row['state'], row['reason'])
                matches.setdefault(identity, dict(row, groups=set()))['groups'].add(entry.get('id', ''))
        if not matches:
            st.info('Aucun résultat non évaluable dans ce périmètre du journal.')
            return
        table_views.dataframe([{'Match': f"{row['home']} — {row['away']}", 'État du match': row['label'],
                       'Motif': row['reason'], 'Groupes concernés': len(row['groups'])}
                      for row in matches.values()], hide_index=True, width='stretch', key=key+'_matches', table_id='result_explanations_panel:render_pending_overview:dataframe:1')
        st.caption('Explication actuelle du journal consulté, indépendante des filtres et des anciens rapports '
                   'statistiques. Les rencontres partagées sont regroupées pour cette liste de motifs. '
                   'Aucun score ni verdict n’est recalculé ; les résultats non évaluables restent hors des taux.')
