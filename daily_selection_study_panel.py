"""Read-only presentation of a frozen, retrospective selection-policy study.

Opening or filtering this page never trains, forecasts, imports results, or
chooses a new rule. All displayed performance comes from the saved report.
"""
import json

import streamlit as st

import daily_selection_study as study
from scenario_tables import METHOD_LABELS
from ux_theme import render_page_header


PHASE_LABELS = {'train': 'Apprentissage', 'calibration': 'Calibration',
                'selection': 'Choix de la règle', 'test': 'Test réservé'}
METHOD_NAMES = dict(METHOD_LABELS, league_baseline='Tendance de chaque compétition')
REASONS = {
    'fewer_than_100_complete_common_groups': 'Moins de 100 groupes complets communs.',
    'fewer_than_100_complete_groups': 'Moins de 100 groupes complets retenus.',
    'fewer_than_20_local_days': 'Moins de 20 journées locales.',
    'fewer_than_10_hits_or_misses_per_method': 'Moins de 10 réalisations ou non-réalisations par méthode.',
    'coverage_below_10_percent': 'Couverture inférieure à 10 %.',
    'calibration_gap_above_10_percentage_points': 'Écart entre confiance calibrée et réalisation supérieur à 10 points.',
}


def _percent(value):
    return None if value is None else 100 * value


def _display(value):
    return '—' if value is None else value


def _table(rows, *, key):
    """Use native numeric formatting while preserving missing observations."""
    if not rows:
        return
    config = {}
    for column in rows[0]:
        if '(%)' in column or '(points)' in column:
            config[column] = st.column_config.NumberColumn(format='%.2f', width=125)
        elif 'Erreur' in column or 'Brier' in column:
            config[column] = st.column_config.NumberColumn(format='%.4f', width=120)
    concise = {'Groupes évalués': ('Évalués', 85), 'Matchs distincts': ('Matchs distincts', 105),
               'Journées': ('Jours', 65), 'Exact en premier (%)': ('Exact 1er (%)', 105),
               'Exact dans les 3 (%)': ('Exact Top 3 (%)', 115),
               'Intervalle descriptif bas (%)': ('IC bas (%)', 95),
               'Intervalle descriptif haut (%)': ('IC haut (%)', 95),
               'Probabilité brute des 3 (%)': ('Top 3 brut (%)', 120),
               'Confiance calibrée du Top 3 (%)': ('Top 3 calibré (%)', 135)}
    for column, (label, width) in concise.items():
        if column in rows[0]:
            config[column] = st.column_config.NumberColumn(label, help=column,
                width=width, format='%.2f' if '(%)' in column else None)
    st.dataframe(rows, hide_index=True, width='stretch', column_config=config, key=key)


def _periods(report):
    protocol = report.get('protocol') or (report.get('inputs') or {}).get('protocol') or {}
    periods = protocol.get('periods') or {}
    rows = []
    for phase, label in PHASE_LABELS.items():
        period = periods.get(phase) or {}
        rows.append({'Période': label,
                     'Du': period.get('start', protocol.get(f'{phase}_start', '—')),
                     'Au, exclu': period.get('end_exclusive', protocol.get(f'{phase}_end_exclusive', '—')),
                     'Usage': {'train': 'Ajuster les modèles', 'calibration': 'Vérifier et ajuster la confiance',
                               'selection': 'Choisir méthode et seuil', 'test': 'Mesurer une règle déjà figée'}[phase]})
    _table(rows, key='selection_study_periods')


def _cohort_label(value):
    labels = {'mono': 'Même compétition', 'mono_five': 'Même compétition · cinq méthodes',
              'mono_static': 'Même compétition · trois méthodes statistiques',
              'mixed': 'Compétitions mélangées · Clubs et référence'}
    return labels.get(value, str(value))


def _scope_label(value):
    return {'match': 'Chaque match', 'scenario': 'Scénario complet',
            'top1': 'Scénario en premier', 'top3': 'Un des trois scénarios',
            'calibrated_top3': 'Confiance calibrée de réalisation du Top 3',
            'min_match_base': 'Minimum des meilleures probabilités brutes par match'}.get(value, str(value or '—'))


def _summary_row(label, summary):
    summary = summary or {}
    sample = summary.get('evaluated_sample') or summary.get('sample') or {}
    metrics = summary.get('metrics') or summary
    raw = metrics.get('raw_top3_event') or {}
    calibrated = metrics.get('calibrated_top3_event') or {}
    interval = metrics.get('top3_interval') or {}
    return {'Population': label,
            'Groupes évalués': metrics.get('evaluated_groups'),
            'Matchs distincts': sample.get('unique_matches'),
            'Journées': sample.get('days'),
            'Exact en premier (%)': _percent(metrics.get('top1_rate')),
            'Exact dans les 3 (%)': _percent(metrics.get('top3_rate')),
            'Intervalle descriptif bas (%)': _percent(interval.get('lower')),
            'Intervalle descriptif haut (%)': _percent(interval.get('upper')),
            'Probabilité brute des 3 (%)': _percent(raw.get('mean_probability')),
            'Confiance calibrée du Top 3 (%)': _percent(calibrated.get('mean_probability')),
            'Erreur par match ↓': metrics.get('loss_per_match')}


def _phase_table(label, phase, *, key):
    phase = phase or {}
    st.markdown(f'**{label}**')
    baseline = phase.get('baseline_on_selected') or {}
    baseline_method = (baseline.get('metrics') or {}).get('method', 'frequency')
    baseline_label = METHOD_NAMES.get(baseline_method, baseline_method)
    rows = [_summary_row('Règle sélectionnée', phase.get('selected')),
            _summary_row('Même méthode, sans filtre', phase.get('unfiltered')),
            _summary_row(f'{baseline_label}, groupes retenus', baseline)]
    _table(rows, key=key)
    common = phase.get('common') or {}
    selected = phase.get('selected') or {}
    coverage = selected.get('coverage')
    coverage_label = '—' if coverage is None else f'{100 * coverage:.2f} %'
    st.caption(f"Population commune : {common.get('groups', 0)} groupes · "
               f"Couverture retenue : {coverage_label} · "
               f"Journées sans proposition : {_display((selected.get('sample') or {}).get('zero_selection_days'))}.")
    st.caption(f"Groupes retenus avant connaissance des scores : {(selected.get('sample') or {}).get('groups', 0)} · "
               f"Résultats incomplets parmi eux : {selected.get('missing_outcomes', 0)}.")
    if not (selected.get('metrics') or {}).get('evaluated_groups'):
        st.info('Aucun groupe retenu dans cette période. Les taux de réussite restent indisponibles ; '
                'cette abstention n’est pas comptée comme un échec.')


def _rule_detail(row):
    rule = row.get('rule')
    if not rule:
        st.warning('Abstention : aucune règle ne satisfait les critères de sélection fixés à l’avance.')
        if row.get('status') == 'insufficient_calibration':
            st.caption('Effectifs de calibration insuffisants : il faut au moins 100 groupes complets communs, '
                       '20 journées, et 10 réalisations ainsi que 10 non-réalisations par méthode.')
        else:
            st.caption('Aucune combinaison de méthode et de seuil ne satisfait les effectifs, '
                       'la couverture et l’écart de calibration requis sur la période de sélection.')
        st.caption('Le meilleur résultat observé au test ne sert pas à remplacer cette décision.')
        return
    method = METHOD_NAMES.get(rule.get('method'), rule.get('method', '—'))
    threshold = _percent(rule.get('threshold'))
    st.markdown(f"**Règle figée : {method} · {_scope_label(rule.get('scope'))} · "
                f"seuil {_display(threshold)} %**")
    st.caption('Cette règle a été choisie avant de consulter ses résultats de test. '
               'Elle ne remplace aucun modèle ni aucune sélection en production.')
    if rule.get('no_filtering_evidence'):
        st.info('Le seuil choisi est nul : cette règle conserve tous les groupes admissibles. '
                'La sélection n’a pas établi l’utilité d’un filtre de confiance.')
    with st.expander('Règle et calibration conservées'):
        st.json(rule, expanded=False)


def _all_methods(row):
    test = row.get('test') or {}
    common = test.get('common') or {}
    methods = common.get('methods') or test.get('methods') or {}
    if not methods:
        return
    st.markdown('**Toutes les méthodes sur les mêmes groupes de test**')
    values = []
    for method, metrics in methods.items():
        result = _summary_row(METHOD_NAMES.get(method, method), metrics)
        result['Population'] = METHOD_NAMES.get(method, method)
        result.pop('Matchs distincts', None)
        result.pop('Journées', None)
        values.append(result)
    _table(values, key='selection_study_methods')
    st.caption('Ces résultats décrivent le test complet. Ils ne choisissent pas rétrospectivement '
               'une autre méthode ni un autre seuil.')


def _calibration(row):
    values = row.get('calibration') or {}
    if not values:
        return
    with st.expander('Fiabilité de la confiance et effectifs de calibration'):
        st.caption('La confiance calibrée concerne la réussite de l’événement retenu. Elle ne remplace '
                   'ni les probabilités 1st / Égal / 2nd ni la distribution complète des scénarios.')
        rows, bins = [], []
        for method, result in values.items():
            sample = result.get('sample') or {}
            metrics = result.get('descriptive_metrics') or {}
            for key, label in [('raw_top3_event', 'Brute'), ('calibrated_top3_event', 'Calibrée')]:
                event = metrics.get(key) or {}
                rows.append({'Méthode': METHOD_NAMES.get(method, method), 'Confiance': label,
                             'Groupes': sample.get('groups'), 'Journées': sample.get('days'),
                             'Estimation moyenne (%)': _percent(event.get('mean_probability')),
                             'Réalisation observée (%)': _percent(event.get('observed_rate')),
                             'Écart absolu (points)': _percent(event.get('absolute_gap')),
                             'Brier ↓': event.get('brier'), 'Erreur de l’événement ↓': event.get('event_logloss')})
        _table(rows, key='selection_study_calibration')
        method = st.selectbox('Méthode à examiner pour la calibration', list(values),
                              format_func=lambda name: METHOD_NAMES.get(name, name), key='selection_study_calibration_method')
        metrics = values[method].get('descriptive_metrics') or {}
        for key, label in [('raw_top3_event', 'Brute'), ('calibrated_top3_event', 'Calibrée')]:
            for item in (metrics.get(key) or {}).get('reliability_bins', []):
                bins.append({'Confiance': label, 'Tranche': f"{100*item['lower']:.0f}–{100*item['upper']:.0f} %",
                             'Groupes': item['groups'], 'Estimation moyenne (%)': _percent(item['mean_probability']),
                             'Réalisation observée (%)': _percent(item['observed_rate'])})
        _table(bins, key='selection_study_reliability')
        st.caption('Ces ajustements sont mesurés sur les données ayant servi à la calibration. '
                   'Leur fiabilité hors de cette période se juge sur la sélection puis sur le test réservé.')


def _availability(row):
    rows = []
    for phase, item in (row.get('availability') or {}).items():
        rows.append({'Période': PHASE_LABELS.get(phase, phase), 'Groupes construits': item.get('groups'),
                     'Groupes complets': item.get('complete_groups'), 'Journées': item.get('days'),
                     'Matchs distincts': item.get('unique_matches'), 'Communs à la famille': item.get('common_groups'),
                     'Communs et complets': item.get('common_complete_groups'),
                     'Méthode absente': item.get('excluded_missing_method'),
                     'Score incomplet': item.get('excluded_missing_outcome')})
    _table(rows, key='selection_study_availability')
    for attempt in (row.get('family') or {}).get('attempts', []):
        name = {'all_five': 'Cinq méthodes', 'statistical_three': 'Trois méthodes statistiques',
                'mixed_two': 'Clubs et référence'}.get(attempt.get('name'), attempt.get('name', '—'))
        reasons = ' '.join(REASONS.get(reason, reason) for reason in attempt.get('reasons', []))
        st.caption(f"{name} · {attempt.get('complete_groups', 0)} groupes complets de calibration · "
                   f"{attempt.get('days', 0)} journées · {'Admissible.' if attempt.get('eligible') else reasons}")


def _by_league(row):
    test = row.get('test') or {}
    values = test.get('by_league') or []
    if not values:
        return
    with st.expander('Résultats et couverture par compétition'):
        rows = []
        for value in values:
            metrics = value.get('metrics') or {}
            baseline = value.get('baseline_metrics') or {}
            rows.append({'Compétition API': f"Compétition {value['league']}", 'Groupes retenus': (value.get('sample') or {}).get('groups'),
                         'Groupes évalués': value.get('evaluated_groups'),
                         'Matchs distincts': (value.get('sample') or {}).get('unique_matches'),
                         'Exact dans les 3 (%)': _percent(metrics.get('top3_rate')),
                         'Référence sur ces groupes (%)': _percent(baseline.get('top3_rate'))})
        _table(rows, key='selection_study_leagues')
        st.caption('Un groupe entre compétitions figure dans chacune de ses compétitions ; ces lignes ne doivent pas être additionnées.')


def _report(report):
    st.info('Comparatif rétrospectif exploratoire : les données historiques ont déjà été examinées. '
            'La période réservée dans cette expérience ne constitue pas une nouvelle confirmation indépendante.')
    st.caption(f"Copie des données figée le {report.get('created_at', '—')} · "
               f"Rapport {report.get('run_id', '')[:12]} · Source unique : API-Football.")
    _periods(report)
    inventory = report.get('inventory') or {}
    leagues = {league for record in report.get('records', []) for league in record.get('leagues', [])}
    details = report.get('method_details') or []
    if isinstance(details, dict):
        details = details.values()
    trained = sum(bool(detail.get('catboost_available')) for detail in details)
    columns = st.columns(3)
    for column, label, value in zip(columns,
            ('Résultats HT/FT disponibles', 'Compétitions dans les groupes', 'CatBoost ajustés sur apprentissage'),
            (inventory.get('valid_past_results'), len(leagues), trained)):
        column.metric(label, _display(value))
    st.markdown('**La règle est choisie sur la période de sélection, puis mesurée sur le test.** '
                'Les tailles et les populations restent séparées. Aucun modèle n’est promu automatiquement.')
    statistics = report.get('statistics') or {}
    sizes = [row for row in statistics.get('by_size', [])
             if row.get('rule') or any((phase or {}).get('groups') for phase in (row.get('availability') or {}).values())]
    if not sizes:
        st.info('Aucune cohorte évaluable dans ce rapport. Les exclusions et la couverture restent consultables ci-dessous.')
    else:
        cohorts = list(dict.fromkeys(row.get('cohort', 'mono') for row in sizes))
        cohort = st.segmented_control('Population à consulter', cohorts, format_func=_cohort_label,
                                       default=cohorts[0], required=True, key='selection_study_cohort')
        scoped = [row for row in sizes if row.get('cohort', 'mono') == cohort]
        if cohort == 'mixed':
            st.caption('Groupes entre compétitions : comparaison distincte entre Clubs et la référence. '
                       'Les cinq méthodes ne sont pas toutes disponibles pour les composants d’un seul match.')
        family = scoped[0].get('family') or {}
        if family.get('methods') and cohort == 'mono':
            st.caption('La famille comparable est choisie sur la calibration : cinq méthodes si ses effectifs le permettent, '
                       'sinon trois méthodes statistiques. Chaque taille conserve sa propre famille.')
        overview = []
        for row in scoped:
            rule = row.get('rule') or {}
            test = row.get('test') or {}
            selected = test.get('selected') or {}
            common = test.get('common') or {}
            overview.append({'Matchs par groupe': row['size'],
                             'Famille': {'all_five': 'Cinq méthodes', 'statistical_three': 'Trois méthodes',
                                         'mixed_two': 'Clubs et référence'}.get((row.get('family') or {}).get('name'), 'Insuffisante'),
                             'Décision': METHOD_NAMES.get(rule.get('method'), rule.get('method')) if rule else 'Abstention',
                             'Seuil (%)': _percent(rule.get('threshold')),
                             'Groupes communs au test': common.get('groups'),
                             'Groupes retenus au test': (selected.get('sample') or {}).get('groups'),
                             'Couverture (%)': _percent(selected.get('coverage')),
                             'Exact dans les 3 (%)': _percent((selected.get('metrics') or {}).get('top3_rate'))})
        _table(overview, key='selection_study_overview')
        size = st.selectbox('Taille à examiner', sorted({row['size'] for row in scoped}),
                            format_func=lambda value: f'{value} matchs', key='selection_study_size')
        row = next(value for value in scoped if value['size'] == size)
        _rule_detail(row)
        if row.get('rule'):
            _phase_table('Résultats de la règle sur le test réservé', row.get('test'), key='selection_study_test')
        st.caption('Le scénario exact doit être réalisé entièrement et dans le bon ordre. '
                   'Les probabilités des trois choix sont additionnées sans être renormalisées à 100 %.')
        _all_methods(row)
        _calibration(row)
        _by_league(row)
        with st.expander('Période ayant servi au choix de la règle'):
            evaluation = (row.get('selection') or {}).get('evaluation')
            if evaluation:
                _phase_table('Sélection, avant le test', evaluation, key='selection_study_selection')
            else:
                st.caption('Aucune règle admissible à évaluer dans cette période.')
        with st.expander('Disponibilité des méthodes et exclusions de cette taille'):
            _availability(row)
    with st.expander('Couverture, protocole et limites du rapport'):
        st.caption('La présence actuelle d’une ancienne affiche ne prouve pas qu’elle était connue au même instant '
                   'avant match. Les groupes aux scores incomplets sont distingués des échecs. '
                   'Les groupes partageant leurs matchs ou leurs clubs ne sont pas des confirmations indépendantes.')
        st.json({'inventaire': report.get('inventory'), 'protocole': report.get('protocol'),
                 'incertitude': statistics.get('protocol'),
                 'limites': report.get('warnings', []) + statistics.get('warnings', [])}, expanded=False)
    st.caption('Les intervalles affichés sont descriptifs. Des matchs, clubs et journées communs ainsi que les règles '
               'multiples essayées limitent leur interprétation ; ils ne démontrent pas un gain prédictif confirmé.')
    st.download_button('Télécharger le comparatif figé', json.dumps(report, ensure_ascii=False, indent=2),
                       file_name='patternlab_comparatif_selection.json', mime='application/json',
                       icon=':material/download:', key='selection_study_download')


def render_selection_study():
    render_page_header('Comparatif de sélection',
                       'Vérifier une règle de sélection avant de l’utiliser dans les analyses du jour.',
                       eyebrow='APPRENTISSAGE · CALIBRATION · SÉLECTION · TEST RÉSERVÉ', icon=':material/science:')
    try:
        report = study.load_latest()
    except (study.StudyError, OSError) as error:
        st.error(str(error))
        return
    if report is None:
        st.info('Aucun comparatif figé disponible. Cette page ne lance aucun calcul ni aucune requête API.')
        st.code('python scripts/evaluate_selection_policy.py', language='bash')
        return
    _report(report)
