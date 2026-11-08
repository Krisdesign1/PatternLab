"""Read-only coherence checks for an already saved ordered match group."""
import table_views
from datetime import datetime
import json
from zoneinfo import ZoneInfo

import streamlit as st

from scenario_group_contract import inspect


STATUS_NAMES = {'valid': 'Contrôles cohérents', 'unverified': 'Preuves incomplètes',
                'invalid': 'Incohérences détectées'}
METHOD_NAMES = {'frequency': 'Répétitions', 'clubs': 'Clubs', 'combined': 'Combinaison',
                'catboost': 'CatBoost', 'context_neighbors': 'Contextes'}


def _time(value):
    if value is None:
        return 'Non conservée'
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            return str(value)
        return stamp.astimezone(ZoneInfo('Africa/Kinshasa')).strftime('%d/%m/%Y %H:%M:%S')
    except (AttributeError, TypeError, ValueError):
        return str(value)


def _method_status(value, name):
    issues = [row for row in value['issues']
              if row.get('method') == name or str(row.get('method', '')).startswith(name+' · ')]
    status = ('invalid' if any(row['severity'] == 'invalid' for row in issues)
              else 'unverified' if issues else 'valid')
    return STATUS_NAMES[status]


def render(entry, *, key='scenario_contract'):
    """Use one supplied immutable forecast; never read or rewrite its journal."""
    st.markdown('**Contrôle du groupe enregistré**')
    try:
        value = inspect(entry)
        if value.get('status') not in STATUS_NAMES:
            raise ValueError('Le statut du contrôle est invalide.')
        created = entry.get('created_at') if isinstance(entry, dict) else None
        summary = [{
            'État du contrôle': STATUS_NAMES[value['status']],
            'Matchs dans le groupe': value['size'],
            'Coupure historique commune (UTC, exclue)': value['common_cutoff'] or 'Non vérifiée',
            'Décision conservée (Kinshasa)': _time(value['decision_at']),
            'Enregistrement déclaré (Kinshasa)': _time(created),
        }]
        methods = [{
            'Méthode': METHOD_NAMES.get(row['name'], row['name']),
            'Version conservée': row['version'],
            'Contrôle de la méthode': _method_status(value, row['name']),
            'Premier scénario (%)': row['top1_probability']*100,
            'Trois premiers scénarios (%)': row['top3_probability']*100,
            'Autres scénarios (%)': row['remaining_probability']*100,
            'Hypothèses conservées': ' · '.join(row['assumptions']),
        } for row in value['methods']]
        issues = [{
            'État': STATUS_NAMES[row['severity']],
            'Méthode': METHOD_NAMES.get(row.get('method'), row.get('method')) or 'Groupe',
            'Point contrôlé': row['code'],
            'Motif': row['message'],
        } for row in value['issues']]
        encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as error:
        st.error(f'Contrôle du groupe impossible : {error}')
        return
    table_views.dataframe(summary, hide_index=True, width='stretch', key=key+'_summary', table_id='scenario_group_contract_panel:render:dataframe:1')
    if methods:
        table_views.dataframe(methods, hide_index=True, width='stretch', key=key+'_methods',
                     column_config={name: st.column_config.NumberColumn(format='%.8g')
                                    for name in methods[0] if name.endswith('(%)')}, table_id='scenario_group_contract_panel:render:dataframe:2')
    if issues:
        table_views.dataframe(issues, hide_index=True, width='stretch', key=key+'_issues', table_id='scenario_group_contract_panel:render:dataframe:3')
    st.caption('Les pourcentages restent absolus sur tous les scénarios possibles ; '
               'les trois premiers ne sont pas ramenés à 100 %. Un contrôle cohérent '
               'ne prouve ni la précision du modèle, ni la disponibilité historique '
               'du fournisseur, ni l’heure physique de sauvegarde avant match. '
               'Les anciennes prévisions restent conservées sans réécriture.')
    st.download_button('Télécharger le contrôle du groupe', encoded,
                       file_name='patternlab_controle_groupe_'+str(value.get('entry_id') or 'inconnu')[:12]+'.json',
                       mime='application/json', key=key+'_download')
