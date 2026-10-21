"""Descriptive paired statistics for the daily grouping policy replay.

The evaluator owns causal features, common fixture multisets, complete paired
slots and outcome attrition. This module accepts their daily counts only. With
the same independent Clubs probabilities on the same full fixture multiset,
per-match logloss is invariant to partitioning; its check is a diagnostic,
never evidence that a support score improves match prediction.
"""
from collections import defaultdict
from datetime import date, timedelta
from math import fsum, isclose, isfinite
import random


VERSION = 'daily-evaluation-statistics-v1'
SEED = 20261021
BOOTSTRAP_SAMPLES = 1000
CONFIDENCE = .95
MIN_WEEKS = 4
NLL_TOLERANCE = 1e-9


def _count(value, *, positive=False):
    if type(value) is not int or value < (1 if positive else 0):
        raise ValueError('Les effectifs appariés doivent être des entiers valides.')
    return value


def _loss(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('Les pertes appariées doivent être finies et positives ou nulles.')
    try:
        value = float(value)
    except (OverflowError, ValueError):
        raise ValueError('Les pertes appariées doivent être finies et positives ou nulles.') from None
    if not isfinite(value) or value < 0:
        raise ValueError('Les pertes appariées doivent être finies et positives ou nulles.')
    return value


def _record(value):
    if not isinstance(value, dict):
        raise ValueError('Un relevé journalier apparié doit être un objet.')
    try:
        day = date.fromisoformat(value['day'])
        if day.isoformat() != value['day']:
            raise ValueError()
        size, groups = _count(value['size'], positive=True), _count(value['groups'], positive=True)
        if not 2 <= size <= 20:
            raise ValueError()
        if value['member_sets_equal'] is not True:
            raise ValueError('Les deux politiques doivent conserver exactement le même multiensemble de matchs.')
        counts = {name: _count(value[name]) for name in (
            'support_top1', 'reference_top1', 'support_top3', 'reference_top3')}
        if (any(count > groups for count in counts.values())
                or counts['support_top1'] > counts['support_top3']
                or counts['reference_top1'] > counts['reference_top3']):
            raise ValueError('Les réussites doivent correspondre aux mêmes groupes et conserver top1 ≤ top3.')
        losses = {name: _loss(value[name]) for name in ('support_loss_sum', 'reference_loss_sum')}
    except (KeyError, TypeError, OverflowError):
        raise ValueError('Un relevé apparié est incomplet ou invalide.') from None
    return dict(day=day, size=size, groups=groups, **counts, **losses)


def _week(day):
    return day-timedelta(days=day.weekday())


def _percentile(values, probability):
    position = (len(values)-1)*probability
    lower = int(position)
    upper = min(lower+1, len(values)-1)
    return values[lower]+(values[upper]-values[lower])*(position-lower)


def _interval(weeks):
    """Resample entire weeks, retaining their group counts in both sums."""
    if len(weeks) < MIN_WEEKS:
        return None, 'insufficient', (f'{len(weeks)} semaine(s) disponible(s) ; au moins {MIN_WEEKS} '
            'sont requises pour cet intervalle descriptif. Ce minimum ne garantit pas une puissance suffisante.')
    ratios = [delta/groups for groups, delta in weeks]
    if isclose(min(ratios), max(ratios), rel_tol=1e-12, abs_tol=1e-12):
        return None, 'no_variation', 'L’écart ne varie pas entre les semaines observées ; aucun intervalle ponctuel n’est présenté comme une précision acquise.'
    generator = random.Random(SEED)
    draws = []
    for _ in range(BOOTSTRAP_SAMPLES):
        chosen = generator.choices(weeks, k=len(weeks))
        draws.append(100*sum(delta for _, delta in chosen)/sum(groups for groups, _ in chosen))
    draws.sort()
    tail = (1-CONFIDENCE)/2
    lower, upper = _percentile(draws, tail), _percentile(draws, 1-tail)
    if isclose(lower, upper, rel_tol=1e-12, abs_tol=1e-12):
        return None, 'no_variation', 'L’intervalle rééchantillonné se réduit à un point ; aucune précision ni équivalence n’en est déduite.'
    return dict(lower=lower, upper=upper), 'descriptive', ('Intervalle exploratoire par rééchantillonnage des semaines, '
        'pondéré par les nombres de groupes ; il ne confirme pas un gain prédictif.')


def summarize_paired(records):
    """Summarize common daily cohorts, separately for every actual group size.

    Each day/size occurs once. ``groups`` is the number of complete paired slots
    on both sides, and losses are sums of their joint scenario negative logs.
    The full membership equality is asserted by the evaluator, not inferred
    from aggregate counts. Missing outcomes/methods must remove the entire
    day/size in that evaluator, never just whichever groups scored badly.

    Records carry local Kinshasa dates rather than kickoff timestamps: clusters
    are their Monday–Sunday calendar weeks. All overlapping groups of a day
    stay in one block. Independence/comparability across weeks is not assured.
    No I/O, fitting, historical data loading, or model selection takes place.
    """
    by_size, identities = defaultdict(list), set()
    for value in records:
        row = _record(value)
        identity = row['day'], row['size']
        if identity in identities:
            raise ValueError('Une journée et une taille ne peuvent être comptées deux fois.')
        identities.add(identity)
        by_size[row['size']].append(row)
    output, failures = [], []
    for size, rows in sorted(by_size.items()):
        rows.sort(key=lambda row: row['day'])
        groups = sum(row['groups'] for row in rows)
        matches = groups*size
        totals = {name: sum(row[name] for row in rows) for name in (
            'support_top1', 'reference_top1', 'support_top3', 'reference_top3')}
        losses = {name: fsum(row[name] for row in rows)/matches
                  for name in ('support_loss_sum', 'reference_loss_sum')}
        differences, checks = [], []
        weeks = defaultdict(lambda:[0, 0])
        for row in rows:
            support, reference = (row[name]/(size*row['groups']) for name in
                                  ('support_loss_sum', 'reference_loss_sum'))
            differences.append(abs(support-reference))
            checks.append(abs(support-reference) <= NLL_TOLERANCE*max(1, abs(support), abs(reference)))
            block = weeks[_week(row['day'])]
            block[0] += row['groups']
            block[1] += row['support_top3']-row['reference_top3']
        interval, status, reason = _interval([tuple(weeks[week]) for week in sorted(weeks)])
        invariant = all(checks)
        if not invariant:
            failures.append(size)
        output.append(dict(size=size, groups=groups, days=len(rows), weeks=len(weeks), matches=matches,
            **{name+'_rate':total/groups for name,total in totals.items()},
            delta_top1_pp=100*(totals['support_top1']-totals['reference_top1'])/groups,
            delta_top3_pp=100*(totals['support_top3']-totals['reference_top3'])/groups,
            interval_top3_pp=interval,
            support_loss_per_match=losses['support_loss_sum'],
            reference_loss_per_match=losses['reference_loss_sum'],
            nll_identity=invariant, nll_max_abs_diff_per_match=max(differences),
            uncertainty_status=status, reason=reason))
    warnings = [
        'Rejeu rétrospectif exploratoire : les horaires, statuts et révisions de résultats historiques ne sont pas des instantanés disponibles avant match.',
        'L’indice mesure l’appui des données, pas une probabilité de réussite. La politique construit tous ses groupes ; elle ne sélectionne pas un top N à valider.',
        'Avec les mêmes probabilités Clubs indépendantes et le même multiensemble de matchs, la logloss par match est invariante au regroupement.',
        'Les tailles sont séparées. Jusqu’à 19 comparaisons, ainsi que les consultations répétées, ne font l’objet d’aucune correction de multiplicité.',
        'Les semaines peuvent rester dépendantes ou non comparables ; quatre semaines constituent un minimum opérationnel, sans garantie de puissance ou de couverture.',
        'Les écarts top 1 et top 3 décrivent la construction des groupes ; aucun gain stable, classement optimal ou promotion de modèle n’est établi.',
    ]
    if failures:
        warnings.insert(0, 'Contrôle d’invariance de la logloss à vérifier pour les tailles : '+', '.join(map(str,failures))+'.')
    return dict(version=VERSION, protocol=dict(
        evaluation_kind='exploratory_retrospective_replay',
        baseline='chronological_partitions', selected='data_support_partitions',
        forecast='independent_clubs_same_probabilities', primary='logloss_per_match_identity_check',
        secondary=['exact_top1_by_size', 'exact_top3_by_size'],
        block='calendar_week_of_kinshasa_day', confidence=CONFIDENCE,
        bootstrap_samples=BOOTSTRAP_SAMPLES, seed=SEED, min_weeks=MIN_WEEKS,
        weighting='ratio_of_summed_paired_top3_differences_to_summed_group_counts',
        nll_tolerance=NLL_TOLERANCE, member_sets_equality='asserted_by_evaluator',
        verdict='descriptive_only_no_model_promotion'), by_size=output, warnings=warnings)
