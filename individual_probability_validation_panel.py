"""Read-only, cached match probabilities in the existing evidence workflow."""
import table_views
from datetime import datetime, timezone
import json
from pathlib import Path

import streamlit as st

import api_football as api
import high_probability_archive as archive
import individual_probability_validation as validation
from probability_reliability_panel import _storage_signature


def _now():
    return datetime.now(timezone.utc)


@st.cache_data(ttl=60, max_entries=4, show_spinner=False)
def _read(root, archive_signature, protocol_signature, clock_minute):
    entries = archive.list_entries(root=root)
    plan = validation.load_protocol(root)
    now = _now()
    return dict(descriptive=validation.build(entries, now=now),
                prospective=validation.build(entries, protocol=plan, now=now) if plan else None,
                protocol=plan, compatibility=validation.compatibility(plan) if plan else None)


def _comparison(report):
    names = {'frequency': 'Tendance du championnat', 'clubs': 'Modèle Clubs actuel'}
    return [{'Méthode': names[name], 'Matchs communs évalués': row['matches'],
             'Favoris corrects': row['correct'],
             'Taux de réussite par match (%)': 100*row['accuracy'] if row['accuracy'] is not None else None,
             'Erreur probabiliste ↓': row['logloss'], 'Brier ↓': row['brier']}
            for name, row in report['metrics'].items()]


def render():
    st.subheader('Qualité des probabilités par match', icon=':material/analytics:')
    st.caption('Une rencontre compte une fois : sa première capture avant match, comparée à la '
               'tendance du championnat conservée au même instant. Les groupes et leurs paris ne '
               'multiplient pas les observations de ce bilan.')
    root = Path(api.DATA_DIR)
    try:
        data = _read(str(root), _storage_signature(archive.database_path(root)),
                     _storage_signature(validation.protocol_path(root)), _now().strftime('%Y-%m-%dT%H:%M'))
    except (ValueError, OSError, KeyError, TypeError):
        st.error('Le bilan individuel est indisponible. Aucun taux ni résultat de remplacement n’est calculé.')
        return
    plan = data['protocol']
    views = {'descriptive': 'Historique descriptif', 'prospective': 'Confirmation future'}
    mode = st.segmented_control('Période de validation individuelle', list(views), format_func=views.get,
        default='descriptive', required=True, key='individual_validation_scope')
    if mode == 'prospective' and plan is None:
        st.info('Aucun protocole futur figé. La consultation ne crée pas de campagne et ne reconstruit aucune prévision.')
        return
    report = data[mode]
    if mode == 'descriptive':
        st.info('Historique descriptif : ces journées ont déjà été consultées. Leurs résultats ne '
                'constituent pas une nouvelle confirmation indépendante.')
    else:
        start = validation._stamp(plan['start_at']).astimezone(validation.ZONE)
        st.caption(f"Protocole {plan['id'][:12]} · Début : {start:%d/%m/%Y à %H:%M} (Kinshasa). "
                   'Les captures antérieures restent hors de cette confirmation.')
        if not data['compatibility']['compatible']:
            st.warning('Le code ou les artefacts diffèrent du protocole figé. La comparaison ne peut '
                       'pas confirmer cette version ; aucune méthode n’est promue.')
        if _now() < start:
            st.info('Le suivi futur est préparé. Ses premières observations apparaîtront après le début '
                    'prévu et leur premier archivage avant match. Les taux resteront vides avant les résultats.')
    with st.container(horizontal=True):
        st.metric('Matchs distincts conservés', str(report['matches']))
        st.metric('Matchs évalués communs', str(report['evaluated']))
        st.metric('En attente', str(report['counts'].get('pending', 0)))
        st.metric('À vérifier ou annulés', str(sum(report['counts'].get(key, 0) for key in ('unresolved', 'void'))))
    table_views.dataframe(_comparison(report), hide_index=True, width='stretch', key='individual_validation_comparison', table_id='individual_probability_validation_panel:render:dataframe:1')
    st.caption('Erreur probabiliste et Brier : une valeur plus faible est préférable. Un taux absent '
               'reste vide. Les résultats manquants et les annulations ne comptent pas comme des erreurs.')
    if report['missing_baseline']:
        st.warning(f"{report['missing_baseline']} première(s) capture(s) sans référence comparable : "
                   'elles restent visibles dans les effectifs, hors des scores appariés ; aucune capture ultérieure ne les remplace.')
    if report['exclusions'].get('incompatible_model_code'):
        st.warning(f"{report['exclusions']['incompatible_model_code']} prévision(s) sans empreinte compatible "
                   'avec le protocole : elles restent hors de la confirmation future.')
    st.markdown('**Les pourcentages élevés sont-ils plus fiables ?**')
    table_views.dataframe([{'Seuil annoncé (%)': 100*row['threshold'], 'Matchs évalués': row['evaluated'],
        'Part des matchs évalués (%)': 100*row['coverage'] if row['coverage'] is not None else None,
        'Confiance moyenne annoncée (%)': 100*row['mean_confidence'] if row['mean_confidence'] is not None else None,
        'Réussite Clubs (%)': 100*row['metrics']['clubs']['accuracy'] if row['evaluated'] else None,
        'Réussite de la référence sur ces mêmes matchs (%)': 100*row['metrics']['frequency']['accuracy'] if row['evaluated'] else None}
        for row in report['thresholds']], hide_index=True, width='stretch', key='individual_validation_thresholds', table_id='individual_probability_validation_panel:render:dataframe:2')
    label = st.segmented_control('Calibration à examiner', validation.LABELS, default='1st',
                                 required=True, key='individual_validation_calibration')
    table_views.dataframe([{'Méthode': 'Clubs' if row['method'] == 'clubs' else 'Championnat',
        'Probabilité annoncée (%)': f"{100*row['lower']:.0f}–{100*row['upper']:.0f}",
        'Matchs évalués': row['matches'],
        'Moyenne annoncée (%)': 100*row['announced'] if row['announced'] is not None else None,
        'Fréquence observée (%)': 100*row['observed'] if row['observed'] is not None else None}
        for row in report['calibration'] if row['label'] == label], hide_index=True,
        width='stretch', key='individual_validation_calibration_table', table_id='individual_probability_validation_panel:render:dataframe:3')
    st.markdown('**Stabilité selon les journées**')
    if report['daily']:
        table_views.dataframe([{'Journée (Kinshasa)': row['day'], 'Matchs conservés': row['matches'],
            'Évalués communs': row['evaluated'], 'En attente': row['counts'].get('pending', 0),
            'À vérifier': row['counts'].get('unresolved', 0), 'Annulés': row['counts'].get('void', 0),
            'Taux Clubs (%)': 100*row['metrics']['clubs']['accuracy'] if row['evaluated'] else None,
            'Taux championnat (%)': 100*row['metrics']['frequency']['accuracy'] if row['evaluated'] else None,
            'Écart d’erreur Clubs − championnat ↓': row['delta_logloss']} for row in report['daily']],
            hide_index=True, width='stretch', key='individual_validation_days', table_id='individual_probability_validation_panel:render:dataframe:4')
    else:
        st.info('Aucune journée admissible dans cette période.')
    st.caption(f"{report['closed_weeks']} semaine(s) UTC complète(s) sans résultat en attente ni non résolu. "
               'Les semaines frontières sont exclues. Les clubs récurrents restent liés ; ces chiffres '
               'descriptifs n’autorisent aucun remplacement automatique du modèle.')
    if plan:
        gate = plan['enriched']
        st.markdown('**Candidat enrichi par les tirs et xG : non retenu sur validation historique.**')
        table_views.dataframe([{'Méthode sur la validation 2025': name,
                       'Erreur probabiliste ↓': gate['validation_logloss'][key]}
                      for key, name in (('frequency', 'Championnat'), ('clubs', 'Clubs'),
                                        ('scores', 'Correction scores'), ('rich', 'Correction tirs et xG'))],
                     hide_index=True, width='stretch', key='individual_validation_enriched', table_id='individual_probability_validation_panel:render:dataframe:5')
        st.caption('Les corrections scores et enrichie n’ont pas amélioré Clubs lors du choix prévu. '
                   'Le candidat rejeté n’est ni réentraîné ni activé pour embellir ce bilan.')
    st.caption('Lecture locale, sans appel API, entraînement, nouveau pari ni modification du journal. '
               'Le service existant conserve les prévisions avant match et rapproche leurs résultats. '
               'La couverture porte sur les matchs calculables archivés, pas sur tous les matchs de l’API.')
    st.download_button('Exporter ce bilan individuel', _export(data, mode),
        file_name=f'patternlab_validation_individuelle_{mode}.json', mime='application/json',
        on_click='ignore', key='individual_validation_export')


def _export(data, mode):
    return json.dumps(dict(report=data[mode], protocol=data['protocol'],
                           compatibility=data['compatibility']), ensure_ascii=False, indent=2, allow_nan=False)
