"""Complete, read-only probability history in a single native table."""
import table_views
import json
from math import isclose, isfinite

import streamlit as st

import prediction_journal as journal
from high_probability_history import VALUES, ZONE
from scenario_presentation import result_column


def trend_values(row, outcome):
    """Return percent and percentage points; a missing baseline stays missing."""
    baseline = row.get('baseline')
    if not isinstance(baseline, dict) or set(baseline) != set(VALUES):
        return None, None
    values = list(baseline.values())
    if (any(type(value) not in (int, float) or not isfinite(value) or not 0 <= value <= 1 for value in values)
            or not isclose(sum(values), 1, abs_tol=1e-9)):
        return None, None
    value = row['probabilities'][outcome]
    return baseline[outcome] * 100, (value - baseline[outcome]) * 100


def _local(value):
    return journal._stamp(value).astimezone(ZONE).strftime('%d/%m/%Y %H:%M') if value else 'Non conservé'


def _explanation(row):
    explanation = row.get('result_explanation') or {}
    detail = next((item for item in explanation.get('matches', [])
                   if str(item.get('match_id')) == str(row['fixture']['match_id'])), {})
    return (detail.get('label') or explanation.get('label'),
            detail.get('reason') or explanation.get('summary'))


def verdict(row, outcome, now):
    if row['status'] == 'settled' and row.get('actual') in VALUES:
        return ('Réalisé', 'green') if row['actual'] == outcome else ('Non réalisé', 'red')
    if row['status'] == 'void':
        return 'Annulé du suivi', 'gray'
    if row['status'] == 'pending' and journal._stamp(row['fixture']['timestamp']) > now:
        return 'En attente', 'blue'
    return 'Non évalué', 'orange'


def _calculations(row):
    values = []
    for saved in row.get('snapshots') or [row]:
        probabilities = ' · '.join(f'{value} : {saved["probabilities"][value] * 100:.2f} %' for value in VALUES)
        values.append(f'{_local(saved["created_at"])} · {probabilities} · '
                      f'Version : {saved.get("model_version") or "Non conservée"}')
    return '\n'.join(values)


def match_table_rows(rows, outcome, now):
    """Expose the saved percentages, results, reasons and provenance together."""
    table = []
    for row in rows:
        fixture = row['fixture']
        trend, gap = trend_values(row, outcome)
        label, _color = verdict(row, outcome, now)
        state, reason = _explanation(row)
        table.append({'Match': f'{fixture["home"]} — {fixture["away"]}',
                      'Compétition': row['competition'],
                      'Coup d’envoi (Kinshasa)': _local(fixture['timestamp']),
                      'Choix': [outcome], 'Probabilité (%)': row['probabilities'][outcome] * 100,
                      'Tendance (%)': trend, 'Écart (points)': gap,
                      **{f'{value} (%)': row['probabilities'][value] * 100 for value in VALUES},
                      'Favori du modèle': [row['favorite']],
                      'Score à la pause': row.get('ht') or 'Non reçu',
                      'Score à 90 minutes': row.get('ft') or 'Non reçu',
                      'Résultat réel': [row['actual']] if row.get('actual') in VALUES else [],
                      'Verdict': [label], 'Statut': state or label, 'Motif': reason or '—',
                      'Calcul avant match': _local(row['created_at']),
                      'Résultats historiques': row.get('training'),
                      'Journées historiques': row.get('history_days'),
                      'Coupure historique UTC': row.get('before') or 'Non conservée',
                      'Version du modèle': row.get('model_version') or 'Non conservée',
                      'Provenance': 'Classement archivé' if row['source_kind'] == 'ranking_archive' else 'Prévision déjà enregistrée',
                      'Calculs conservés': _calculations(row),
                      'Corrections des résultats': json.dumps(row['result_history'], ensure_ascii=False)
                          if row.get('result_history') else 'Aucune correction enregistrée'})
    return table


def render_match_table(rows, outcome, now):
    table_views.dataframe(match_table_rows(rows, outcome, now), hide_index=True, width='stretch',
                 height='content', row_height=64, key='hp_archive_table',
                 column_config={
                     'Match': st.column_config.TextColumn(width=300, pinned=True),
                     'Compétition': st.column_config.TextColumn(width=210),
                     'Coup d’envoi (Kinshasa)': st.column_config.TextColumn(width=165),
                     'Choix': result_column('Choix', width=75),
                     **{key: st.column_config.NumberColumn(format='%.1f', width=105)
                        for key in ('Probabilité (%)', '1st (%)', 'Égal (%)', '2nd (%)')},
                     'Tendance (%)': st.column_config.NumberColumn(format='%.1f', width=110,
                         help='Fréquence historique lissée de cette catégorie dans la compétition, conservée avant match. Une cellule vide signifie que la référence n’a pas été conservée.'),
                     'Écart (points)': st.column_config.NumberColumn(format='%+.1f', width=110,
                         help='Probabilité moins tendance, en points. Cet écart ne mesure pas la réussite du modèle.'),
                     'Favori du modèle': result_column('Favori du modèle'),
                     'Score à la pause': st.column_config.TextColumn(width=120),
                     'Score à 90 minutes': st.column_config.TextColumn(width=130),
                     'Résultat réel': result_column('Résultat réel'),
                     'Statut': st.column_config.TextColumn(width=220),
                     'Motif': st.column_config.TextColumn(width=500),
                     'Calcul avant match': st.column_config.TextColumn(width=165),
                     'Calculs conservés': st.column_config.TextColumn(width=500),
                     'Corrections des résultats': st.column_config.TextColumn(width=500),
                     'Verdict': st.column_config.MultiselectColumn(width=140,
                         options=['Réalisé', 'Non réalisé', 'En attente', 'Non évalué', 'Annulé du suivi'],
                         color=['#15803D', '#B91C1C', '#1D4ED8', '#B45309', '#64748B'])}, table_id='probability_history_presentation:render_match_table:dataframe:1')
