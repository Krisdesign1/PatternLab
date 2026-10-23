"""Read-only historical replay and prospective evidence for daily selections."""
import json

import streamlit as st
from cloud_ui import guarded

import api_football as api
import daily_analysis_worker as worker
import daily_evaluation_live as live
import daily_evaluation_store as store
import prediction_journal as journal
from scenario_tables import METHOD_LABELS
from ux_theme import render_page_header


def _percent(value):
    return None if value is None else 100*value


def _historical(report):
    st.info('Rejeu historique exploratoire : les anciennes affiches sont reconstruites à partir des données locales actuelles. '
            'Ce rapport ne remplace pas des prévisions réellement enregistrées avant match.')
    protocol = report.get('protocol') or {}
    inventory = report.get('inventory') or {}
    st.caption(f"Période : {protocol.get('start', '—')} → {protocol.get('end_exclusive', '—')} exclu · "
               'Profils et fréquences calculés uniquement avant la coupure historique de chaque journée.')
    if report.get('created_at'):
        st.caption(f"Copie des données figée le {report['created_at']} · Rapport {report.get('run_id','')[:12]}.")
    metrics = st.columns(3)
    for column, label, key in zip(metrics, ('Journées reconstruites', 'Résultats HT/FT disponibles', 'Rencontres admissibles'),
                                   ('calendar_days','valid_past_results','eligible_matches')):
        column.metric(label,inventory.get(key,0))
    st.markdown('**Ce rejeu ne suffit pas à démontrer un gain prédictif régulier de l’indice d’appui.** '
                'Il sépare le choix des groupes de la qualité du modèle Clubs, sans changer la politique en production.')
    st.markdown('**Regroupement par appui des données contre regroupement chronologique**')
    st.caption('Chaque comparaison conserve les mêmes rencontres, la même taille et la même coupure. '
               'Le reliquat commun est retiré avant les deux tris. Les groupes horaires complémentaires sont exclus de ce contraste contrôlé.')
    paired = report.get('paired_summary') or {}
    sizes = paired.get('by_size') or []
    if not sizes:
        st.info('Aucune cohorte comparable complète dans ce rapport.')
    else:
        paired_days = [row['day'] for row in report.get('paired_records', [])]
        if paired_days:
            st.caption(f'Cohortes complètes comparables : du {min(paired_days)} au {max(paired_days)}. '
                       'Les autres journées restent examinées ; leurs exclusions figurent dans le protocole.')
        st.dataframe([{'Matchs par groupe': row['size'], 'Groupes par politique': row['groups'],
                       'Journées': row['days'], 'Semaines': row['weeks'],
                       'Exact en premier · Appui (%)': _percent(row['support_top1_rate']),
                       'Exact en premier · Chronologique (%)': _percent(row['reference_top1_rate']),
                       'Exact dans les 3 · Appui (%)': _percent(row['support_top3_rate']),
                       'Exact dans les 3 · Chronologique (%)': _percent(row['reference_top3_rate']),
                       'Écart dans les 3 (points)': row['delta_top3_pp'],
                       'Intervalle exploratoire bas': (row.get('interval_top3_pp') or {}).get('lower'),
                       'Intervalle exploratoire haut': (row.get('interval_top3_pp') or {}).get('upper')}
                      for row in sizes], hide_index=True, width='stretch', key='daily_audit_comparison',
                     column_config={
                         'Matchs par groupe':st.column_config.NumberColumn('Matchs',width=65),
                         'Groupes par politique':st.column_config.NumberColumn('Groupes',width=80),
                         'Journées':st.column_config.NumberColumn('Jours',width=65),
                         'Semaines':st.column_config.NumberColumn('Semaines',width=80),
                         'Exact en premier · Appui (%)':st.column_config.NumberColumn('1er · Appui (%)',format='%.2f',width=115),
                         'Exact en premier · Chronologique (%)':st.column_config.NumberColumn('1er · Chrono (%)',format='%.2f',width=125),
                         'Exact dans les 3 · Appui (%)':st.column_config.NumberColumn('Top 3 · Appui (%)',format='%.2f',width=130),
                         'Exact dans les 3 · Chronologique (%)':st.column_config.NumberColumn('Top 3 · Chrono (%)',format='%.2f',width=140),
                         'Écart dans les 3 (points)':st.column_config.NumberColumn('Écart Top 3 (pts)',format='%+.2f',width=135),
                         'Intervalle exploratoire bas':st.column_config.NumberColumn('Intervalle bas',format='%+.2f',width=110),
                         'Intervalle exploratoire haut':st.column_config.NumberColumn('Intervalle haut',format='%+.2f',width=110)})
        st.caption('Un écart positif favorise le regroupement par appui ; un écart négatif favorise l’ordre chronologique. '
                   'Chaque taux exige le scénario complet dans le bon ordre. Les tailles restent séparées.')
        st.caption('Les intervalles sont descriptifs, avec rééchantillonnage par semaine locale. Les groupes peuvent partager '
                   'des matchs ; les 19 tailles sont explorées et aucune correction des comparaisons multiples n’est appliquée.')
        if all(row.get('nll_identity') for row in sizes):
            st.success('Contrôle vérifié : à matchs identiques, le regroupement conserve la même erreur probabiliste moyenne par match pour Clubs indépendants.')
        else:
            st.warning('Le contrôle d’identité des pertes probabilistes nécessite une vérification dans le rapport.')
        st.caption('Le regroupement peut modifier la réussite des scénarios complets. Il ne change pas les probabilités individuelles '
                   'du moteur Clubs indépendant. Ce contraste ne mesure pas les performances de CatBoost ou du modèle par contexte.')
        records = report.get('paired_records') or []
        if records:
            chosen = st.selectbox('Taille à examiner dans le temps', [row['size'] for row in sizes], key='daily_audit_size')
            cumulative = []
            counts = dict(groups=0, support=0, reference=0)
            for row in sorted((row for row in records if row['size'] == chosen), key=lambda row: row['day']):
                counts['groups'] += row['groups']
                counts['support'] += row['support_top3']
                counts['reference'] += row['reference_top3']
                cumulative.extend([{'Date': row['day'], 'Politique': label,
                                    'Scénario exact dans les 3 (%)': 100*counts[key]/counts['groups']}
                                   for key, label in [('support','Appui des données'),('reference','Chronologique')]])
            st.line_chart(cumulative, x='Date', y='Scénario exact dans les 3 (%)', color='Politique')
    production = report.get('production_by_size') or []
    with st.expander('Politique quotidienne complète : Clubs contre tendance de la compétition'):
        st.caption('Cette vue rejoue tous les groupes construits par la politique actuelle, y compris les compléments horaires. '
                   'La référence utilise les fréquences historiques propres à chaque compétition, sur les mêmes groupes évaluables. '
                   'Ces cohortes diffèrent du contraste contrôlé ci-dessus.')
        if production:
            st.dataframe([{'Matchs par groupe': row['size'], 'Groupes construits':row['groups'],
                           'Groupes évaluables':row['evaluated'], 'Moteur indisponible':row['unavailable'],
                           'Résultats manquants':row['missing_outcomes'],
                           'Exact dans les 3 · Clubs (%)':_percent(row['clubs_top3_rate']),
                           'Exact dans les 3 · Tendance (%)':_percent(row['baseline_top3_rate']),
                           'Erreur par match · Clubs ↓':row['clubs_loss_per_match'],
                           'Erreur par match · Tendance ↓':row['baseline_loss_per_match']}
                          for row in production],hide_index=True,width='stretch',key='daily_audit_production')
        st.caption('Les avantages éventuels de Clubs face à la tendance ne prouvent pas que le tri par appui choisit de meilleurs groupes.')
    with st.expander('L’appui des données correspond-il à de meilleures probabilités ?'):
        bins = report.get('support_bins') or []
        if bins:
            st.dataframe([{'Indice d’appui':f"{row['lower']}–{row['upper']}", 'Matchs évaluables':row['matches'],
                           'Résultats manquants':row['missing_outcomes'],
                           'Erreur Clubs par match ↓':row['clubs_loss'],
                           'Erreur tendance par match ↓':row['baseline_loss']}
                          for row in bins],hide_index=True,width='stretch',key='daily_audit_support_bins')
        st.caption('Chaque rencontre apparaît une seule fois dans ces tranches prédéfinies. Les populations de clubs, '
                   'compétitions et saisons diffèrent ; leurs écarts ne démontrent pas un effet causal de l’indice.')
    with st.expander('Protocole, exclusions et limites du rejeu'):
        st.markdown('Les anciens calendriers, statuts et scores ont pu être corrigés depuis leur date initiale. '
                    'La présence d’une affiche dans la base actuelle ne prouve pas qu’elle était disponible au même instant dans le passé. '
                    'Les groupes incomplets ou les moteurs indisponibles sont exclus de façon explicite, sans les compter comme des échecs.')
        st.markdown('Ce protocole reste exploratoire. Aucun réglage de production ni aucune ancienne prévision n’est remplacé à partir de ses résultats.')
        st.json({'protocole':protocol, 'inventaire':report.get('inventory'),
                 'exclusions_comparaison':report.get('controlled_attrition'),
                 'incertitude':paired.get('protocol'), 'limites':report.get('warnings',[])}, expanded=False)
    st.download_button('Télécharger le rapport du rejeu', json.dumps(report, ensure_ascii=False, indent=2),
                       file_name='patternlab_audit_selection_quotidienne.json', mime='application/json',
                       icon=':material/download:', key='daily_audit_download')


def _prospective():
    try:
        report = live.summarize(journal.list_entries(), worker.load_selections(api.DATA_DIR))
    except (ValueError, OSError) as error:
        st.error(str(error))
        return
    counts = report['counts']
    metrics = st.columns(3)
    for column, label, key in zip(metrics, ('Analyses enregistrées', 'En attente', 'Réalisées'), ('total','pending','settled')):
        column.metric(label, counts[key])
    st.caption(f"{report['unique_matches']} matchs distincts dans la sélection · {report['evaluated_unique_matches']} "
               'dans les groupes évalués. Les groupes partageant des rencontres ne sont pas des confirmations indépendantes.')
    if not report['by_method_size']:
        st.info('Aucune analyse quotidienne enregistrée avant match pour le moment.')
        return
    st.dataframe([{'Matchs par groupe': row['size'], 'Méthode': METHOD_LABELS.get(row['method'],row['method']),
                   'Version': row['version'], 'Génération': row['generation'][:12] or '—',
                   'Politique': row['policy'], 'Groupes évalués': row['evaluated'], 'En attente': row['pending'],
                   'Annulés ou à vérifier': row['excluded'], 'Évaluations manquantes': row['missing_evaluations'],
                   'Exact en premier (%)': _percent(row['top1_rate']), 'Exact dans les 3 (%)': _percent(row['top3_rate']),
                   'Erreur probabiliste par match ↓': row['loss_per_match']}
                  for row in report['by_method_size']], hide_index=True, width='stretch', key='daily_audit_prospective')
    st.caption(report['interpretation'])
    st.caption('Ces chiffres utilisent uniquement les évaluations déjà enregistrées. Ouvrir cette page ne récupère aucun score '
               'et ne recalcule aucune prévision. Les groupes en attente sont exclus des taux de réussite.')
    if counts['invalid']:
        st.warning(f"{counts['invalid']} groupe(s) ne satisfont pas les contrôles d’enregistrement avant match et sont exclus du bilan.")
    st.page_link('ui_pages/daily.py', label='Ouvrir les analyses et leurs résultats', icon=':material/stars:')


@st.fragment(run_every=60)
@guarded
def render_evaluation():
    render_page_header('Bilan de la sélection',
                       'Mesurer le regroupement des matchs, la qualité des probabilités et les résultats réellement observés.',
                       eyebrow='REJEU CHRONOLOGIQUE · COMPARAISON PAR TAILLE · SUIVI RÉEL', icon=':material/fact_check:')
    modes = {'historical':'Rejeu historique', 'prospective':'Suivi réel'}
    mode = st.segmented_control('Bilan à consulter', list(modes), format_func=modes.get,
                                default='historical', required=True, key='daily_audit_view')
    if mode == 'prospective':
        _prospective()
        return
    try:
        report = store.load_latest()
    except (ValueError, OSError) as error:
        st.error(str(error))
        return
    if report is None:
        st.info('Aucun rapport de rejeu conservé pour le moment. Le suivi des analyses réelles reste accessible.')
        st.code('python scripts/evaluate_daily_analyses.py', language='bash')
        return
    _historical(report)
