"""Pure, fixed-rule shortlist over an existing, score-free probability scan.

No model is fitted or called here. All probabilities are reused unchanged. The
ten recent matches mean the last ten *available* API-Football matches in the
same competition and venue role, with exact club IDs. They may span years.
Naive timestamps in normalized API history and scan fixtures denote UTC.
"""
from bisect import bisect_left
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date, datetime, time, timezone
from math import isclose, isfinite
from numbers import Real
import re
from zoneinfo import ZoneInfo
from halftime_domain import CLASS_ORDER as VALUES


# The selection thresholds are unchanged. A separate procedure version is
# necessary because its pinned model implementation now includes the common
# target contract. Never rewrite the old procedure or its first captures.
VERSION = 'argumented-selection-v2-contracts'
SOURCE = 'API-Football'
PROVIDER = 'api-football'
ZONE = ZoneInfo('Africa/Kinshasa')
CRITERIA = dict(
    version=VERSION, timezone=ZONE.key, fixture_status='NS', future_only=True,
    minimum_full_profile=10, recent_matches=10, minimum_recent_matching=5,
    recent_modal_required=True, model_baseline_agreement=True,
    exact_team_ids=True, same_league=True, same_role=True,
    history_cutoff='strictly_before_min_report_before_current_utc_day_fixture_utc_day',
    recent_window='last_available_without_calendar_limit',
    favourite_tie_order=list(VALUES),
)
_PROOF_FIELDS = ('choice', 'selection_probability', 'baseline_probability',
                 'delta_points', 'recent_home', 'recent_away', 'reasons',
                 'selection_version', 'criteria')
_SCORE_FIELDS = frozenset(('score', 'scores', 'ht', 'ft', 'value', 'goals',
                           'halftime', 'fulltime', 'outcome', 'result',
                           'home_score', 'away_score'))


class _Invalid(ValueError):
    def __init__(self, reason, detail):
        super().__init__(detail)
        self.reason = reason


def _now(value):
    value = datetime.now(timezone.utc) if value is None else value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('Une horloge avec fuseau horaire est nécessaire.')
    return value.astimezone(timezone.utc)


def _day(value, label):
    if not isinstance(value, str):
        raise ValueError(f'{label} absente ou invalide.')
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f'{label} absente ou invalide.') from None
    if parsed.isoformat() != value:
        raise ValueError(f'{label} absente ou invalide.')
    return parsed


def _identifier(value):
    try:
        valid = (not isinstance(value, bool) and isinstance(value, (int, str))
                 and str(value).isascii() and str(value).isdecimal() and int(value) > 0)
    except (ValueError, OverflowError):
        valid = False
    if not valid:
        raise _Invalid('invalid_identity', 'Identifiant API du match, de compétition ou de club absent ou invalide.')
    return str(int(value))


def _stamp(value):
    if not isinstance(value, str) or 'T' not in value:
        raise _Invalid('invalid_date', 'Date et heure UTC du match absentes ou invalides.')
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        # api.normalize_fixture and fixture_from_raw explicitly serialize UTC
        # without its offset. The source is checked before accepting this form.
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise _Invalid('invalid_date', 'Date et heure UTC du match absentes ou invalides.') from None


def _source(row):
    if row.get('provider') != PROVIDER:
        raise _Invalid('foreign_provider', 'Seuls les matchs explicitement identifiés API-Football sont acceptés.')
    if row.get('source') not in (None, '', SOURCE, PROVIDER,
                                  'https://www.api-football.com', 'https://www.api-football.com/'):
        raise _Invalid('foreign_source', 'La provenance du match ne correspond pas à API-Football.')


def _identity(row):
    _source(row)
    mid, league, home, away = (_identifier(row.get(key))
                               for key in ('match_id', 'league', 'home_id', 'away_id'))
    if home == away:
        raise _Invalid('invalid_identity', 'Les deux clubs doivent avoir des identifiants différents.')
    if row.get('competition_code') is not None and _identifier(row['competition_code']) != league:
        raise _Invalid('invalid_identity', 'Identifiants de compétition contradictoires.')
    if not all(isinstance(row.get(key), str) and row[key].strip() for key in ('home', 'away')):
        raise _Invalid('unconfirmed_fixture', 'Les noms des deux clubs doivent être confirmés.')
    stamp = _stamp(row.get('timestamp'))
    if 'day' in row and row['day'] != stamp.date().isoformat():
        raise _Invalid('invalid_date', 'La journée UTC déclarée contredit la date du match.')
    return mid, league, home, away, stamp


def _fixture(row, target_day, before, current):
    fixture = row.get('fixture')
    if not isinstance(fixture, dict):
        raise _Invalid('invalid_fixture', 'Affiche du match absente ou invalide.')
    mid, league, home, away, stamp = _identity(fixture)
    # Reject the presence of target results without reading their values.
    if _SCORE_FIELDS.intersection(fixture):
        raise _Invalid('fixture_contains_result', 'Une affiche avant match doit être dépourvue de scores et de résultat.')
    unknown = {'tbd', 'unknown', 'to be determined', 'à déterminer', 'a determiner', '?'}
    if any(fixture[key].strip().casefold() in unknown for key in ('home', 'away')):
        raise _Invalid('unconfirmed_fixture', 'Les deux clubs doivent être confirmés.')
    if stamp.astimezone(ZONE).date() != target_day:
        raise _Invalid('wrong_day', 'Le match ne fait pas partie de la journée sélectionnée à Kinshasa.')
    if (fixture.get('raw_status') != 'NS'
            or fixture.get('status', 'TIMED') != 'TIMED'
            or fixture.get('is_upcoming', True) is not True
            or fixture.get('is_historical', False) is not False
            or fixture.get('selectable', True) is not True):
        raise _Invalid('not_upcoming', 'Seul un match NS confirmé à venir est retenu.')
    if stamp <= current:
        raise _Invalid('kickoff_elapsed', 'Le coup d’envoi est passé ou a déjà commencé.')
    if before > min(current.date(), stamp.date()):
        raise _Invalid('unsafe_scan_cutoff', 'La coupure du calcul dépasse la journée UTC autorisée avant match.')
    if 'before' in row and row['before'] != before.isoformat():
        raise _Invalid('inconsistent_scan_cutoff', 'La coupure de cette probabilité diffère de celle du rapport.')
    return mid, league, home, away, stamp


def _probabilities(value):
    if (not isinstance(value, dict) or set(value) != set(VALUES)
            or any(isinstance(p, bool) or not isinstance(p, Real) or not isfinite(p)
                   or not 0 <= p <= 1 for p in value.values())
            or not isclose(sum(value.values()), 1, rel_tol=0, abs_tol=1e-9)):
        raise _Invalid('invalid_probabilities', 'Chaque distribution doit contenir 1st, Égal et 2nd, avec des probabilités finies dont la somme vaut 1.')
    return max(VALUES, key=value.get)


def _profiles(row):
    counts = []
    for key in ('home_profile', 'away_profile'):
        profile = row.get(key)
        if not isinstance(profile, dict) or type(profile.get('matches')) is not int or profile['matches'] < 0:
            raise _Invalid('invalid_profile', 'Nombre de précédents du profil complet absent ou invalide.')
        counts.append(profile['matches'])
    if min(counts) < CRITERIA['minimum_full_profile']:
        raise _Invalid('insufficient_full_profile',
                       f'Profils complets : domicile {counts[0]}, extérieur {counts[1]} ; au moins 10 chacun sont nécessaires.')
    if row.get('personalized', True) is not True:
        raise _Invalid('not_personalized', 'Les probabilités doivent exploiter les profils des deux clubs.')
    training, days = row.get('training'), row.get('history_days')
    if (type(training) is not int or training < max(counts)
            or type(days) is not int or not 1 <= days <= training):
        raise _Invalid('invalid_training_metadata', 'Les tailles des profils, de l’historique et des journées historiques sont incohérentes.')


def _score(value):
    text = value.strip() if isinstance(value, str) else ''
    if text.startswith('(') and text.endswith(')'):
        text = text[1:-1].strip()
    pair = re.fullmatch(r'([0-9]+)\s*-\s*([0-9]+)', text)
    if not pair:
        raise _Invalid('invalid_score', 'Scores HT et FT complets, entiers et positifs ou nuls requis.')
    try:
        return tuple(map(int, pair.groups()))
    except ValueError:
        raise _Invalid('invalid_score', 'Scores HT et FT hors plage prise en charge.') from None


def _history_row(row):
    mid, league, home, away, stamp = _identity(row)
    if (row.get('raw_status', 'FT') not in ('FT', 'AET', 'PEN')
            or row.get('status', 'FINISHED') not in ('FT', 'AET', 'PEN', 'FINISHED')
            or row.get('is_upcoming', False) is not False
            or row.get('is_historical', True) is not True):
        raise _Invalid('unfinished_history', 'Seuls des résultats historiques terminés sont acceptés.')
    ht, ft = _score(row.get('ht')), _score(row.get('ft'))
    if any(first > full for first, full in zip(ht, ft)):
        raise _Invalid('invalid_score', 'Un score HT ne peut pas dépasser le score FT du même club.')
    first, second = sum(ht), sum(ft)-sum(ht)
    observed = '1st' if first > second else '2nd' if second > first else 'Égal'
    if row.get('value') != observed:
        raise _Invalid('inconsistent_outcome', 'La catégorie historique est absente ou contredit les scores HT/FT.')
    checked = deepcopy(row)
    checked.update(match_id=mid, league=league, home_id=home, away_id=away,
                   timestamp=stamp.replace(tzinfo=None).isoformat(timespec='seconds'),
                   day=stamp.date().isoformat(), ht='-'.join(map(str, ht)),
                   ft='-'.join(map(str, ft)), value=observed)
    if 'competition_code' in checked:
        checked['competition_code'] = league
    return checked, stamp


def _history_index(history, boundary, diagnostics):
    """Validate and index the history once; never estimate probabilities."""
    unique, conflicts = {}, set()
    issues = diagnostics['invalid_history']
    for item in history:
        mid = None
        try:
            if not isinstance(item, dict):
                raise _Invalid('invalid_history', 'Ligne historique absente ou invalide.')
            _source(item)
            mid = _identifier(item.get('match_id'))
            # Scope history before inspecting results or resolving identities:
            # a later/current-day record must not alter an earlier shortlist.
            if _stamp(item.get('timestamp')).date() >= boundary:
                diagnostics['outside_history_cutoff_count'] += 1
                continue
        except _Invalid as error:
            issues.append(dict(match_id=mid, reason=error.reason, detail=str(error)))
            continue
        if mid in unique:
            if unique[mid] == item:
                diagnostics['history_duplicate_count'] += 1
            else:
                conflicts.add(mid)
        else:
            unique[mid] = item
    groups = defaultdict(list)
    for mid, item in unique.items():
        if mid in conflicts:
            issues.append(dict(match_id=mid, reason='conflicting_history_duplicate',
                               detail='Plusieurs relevés API contradictoires : cet identifiant historique est entièrement écarté.'))
            continue
        try:
            checked, stamp = _history_row(item)
        except _Invalid as error:
            issues.append(dict(match_id=mid, reason=error.reason, detail=str(error)))
            continue
        diagnostics['history_count'] += 1
        for role in ('home', 'away'):
            groups[(checked['league'], checked[f'{role}_id'], role)].append((stamp, int(mid), checked))
    indexed = {}
    for key, members in groups.items():
        members.sort(key=lambda member: (member[0], member[1]))
        indexed[key] = ([member[0] for member in members], [member[2] for member in members])
    issues.sort(key=lambda issue: (int(issue['match_id']) if issue['match_id'] is not None else -1,
                                   issue['reason'], issue['detail']))
    diagnostics['invalid_history_count'] = len(issues)
    diagnostics['history_group_count'] = len(indexed)
    return indexed


def _recent(index, league, team, role, cutoff, choice):
    stamps, rows = index.get((league, team, role), ([], []))
    stop = bisect_left(stamps, datetime.combine(cutoff, time.min, timezone.utc))
    recent = deepcopy(rows[max(0, stop-CRITERIA['recent_matches']):stop])
    counts = {value: sum(row['value'] == value for row in recent) for value in VALUES}
    return dict(matches=len(recent), counts=counts, matching=counts[choice],
                first_at=recent[0]['timestamp'] if recent else None,
                last_at=recent[-1]['timestamp'] if recent else None,
                ids=[row['match_id'] for row in recent], rows=recent)


def _qualify(row, index, day, before, current):
    mid, league, home, away, stamp = _fixture(row, day, before, current)
    choice = _probabilities(row.get('probabilities'))
    baseline_choice = _probabilities(row.get('baseline'))
    _profiles(row)
    if choice != baseline_choice:
        raise _Invalid('model_baseline_disagreement',
                       f'Favori du modèle : {choice} ; favori de la référence de compétition : {baseline_choice}.')
    cutoff = min(before, current.date(), stamp.date())
    recent_home = _recent(index, league, home, 'home', cutoff, choice)
    recent_away = _recent(index, league, away, 'away', cutoff, choice)
    for role, recent in (('home', recent_home), ('away', recent_away)):
        label = 'à domicile' if role == 'home' else 'à l’extérieur'
        if recent['matches'] != CRITERIA['recent_matches']:
            raise _Invalid(f'insufficient_recent_{role}',
                           f"Seulement {recent['matches']}/10 précédents disponibles {label}, dans la même compétition et avec le même identifiant de club.")
        if (recent['matching'] < CRITERIA['minimum_recent_matching']
                or recent['matching'] != max(recent['counts'].values())):
            raise _Invalid(f'recent_{role}_not_confirmed',
                           f"{choice} apparaît {recent['matching']}/10 fois {label} ; il doit atteindre au moins 5/10 et être une catégorie la plus fréquente (ex æquo admis).")
    selected = deepcopy(row)
    probability, baseline = row['probabilities'][choice], row['baseline'][choice]
    selected.update(choice=choice, selection_probability=probability,
                    baseline_probability=baseline, delta_points=(probability-baseline)*100,
                    recent_home=recent_home, recent_away=recent_away,
                    reasons=[
                        f'{choice} est le favori du modèle et de la référence de compétition.',
                        f"Profils complets : {row['home_profile']['matches']} précédents à domicile et {row['away_profile']['matches']} à l’extérieur.",
                        f"Dix derniers disponibles à domicile : {recent_home['matching']}/10 en {choice}, catégorie la plus fréquente (ex æquo admis).",
                        f"Dix derniers disponibles à l’extérieur : {recent_away['matching']}/10 en {choice}, catégorie la plus fréquente (ex æquo admis).",
                        f'Tous les précédents précèdent strictement le {cutoff.isoformat()} UTC ; les dix derniers disponibles peuvent couvrir plusieurs années.',
                    ], selection_version=VERSION, criteria=deepcopy(CRITERIA))
    return selected


def build(report, history, now=None):
    """Return every qualifying fixture, preserving its existing scan fields.

    Invalid top-level reports raise ValueError. Invalid or nonqualifying rows
    are explained in ``excluded``; invalid histories are audited and excluded
    from every count. Identical IDs deduplicate; conflicting IDs never compete
    for the version most favourable to selection. Inputs are never mutated.
    """
    current = _now(now)
    if (not isinstance(report, dict) or report.get('source') != SOURCE
            or not isinstance(report.get('rows'), list)
            or not isinstance(report.get('excluded', []), list)
            or any(not isinstance(item, dict) for item in report.get('excluded', []))):
        raise ValueError('Un rapport local de probabilités API-Football valide est nécessaire.')
    if not isinstance(history, (list, tuple)):
        raise ValueError('L’historique normalisé API-Football doit être une liste de matchs.')
    day = _day(report.get('day'), 'Journée de sélection')
    before = _day(report.get('before'), 'Coupure historique UTC')
    excluded = deepcopy(report.get('excluded', []))
    diagnostics = dict(input_count=len(report['rows']), selected_count=0,
                       scan_excluded_count=len(excluded), fixture_duplicate_count=0,
                       history_input_count=len(history), history_count=0,
                       history_duplicate_count=0, history_group_count=0,
                       invalid_history_count=0, invalid_history=[], outside_history_cutoff_count=0)
    index = _history_index(history, min(before, current.date()), diagnostics)
    unique, conflicts = {}, set()
    for row in report['rows']:
        mid = None
        try:
            if not isinstance(row, dict) or not isinstance(row.get('fixture'), dict):
                raise _Invalid('invalid_fixture', 'Affiche du match absente ou invalide.')
            mid = _identifier(row['fixture'].get('match_id'))
        except _Invalid as error:
            excluded.append(dict(match_id=mid, reason=error.reason, detail=str(error)))
            continue
        if mid in unique:
            if unique[mid] == row:
                diagnostics['fixture_duplicate_count'] += 1
            else:
                conflicts.add(mid)
        else:
            unique[mid] = row
    selected = []
    for mid, row in unique.items():
        try:
            if mid in conflicts:
                raise _Invalid('conflicting_fixture_duplicate', 'Plusieurs calculs ou affiches contradictoires pour cet identifiant : le match est écarté.')
            selected.append(_qualify(row, index, day, before, current))
        except _Invalid as error:
            excluded.append(dict(match_id=mid, reason=error.reason, detail=str(error),
                                 fixture=deepcopy(row['fixture'])))
    selected.sort(key=lambda row: (-row['selection_probability'], _stamp(row['fixture']['timestamp']),
                                    int(row['fixture']['match_id'])))
    diagnostics['selected_count'] = len(selected)
    diagnostics['excluded_counts'] = dict(Counter(item.get('reason', 'unknown') for item in excluded))
    return dict(version=VERSION, source=SOURCE, timezone=ZONE.key,
                day=day.isoformat(), before=before.isoformat(),
                generated_at=current.isoformat(timespec='seconds'), criteria=deepcopy(CRITERIA),
                history_fingerprint=report.get('history_fingerprint'),
                rows=selected, excluded=excluded, diagnostics=diagnostics)


def validate_row(row, *, day, before, now=None):
    """Validate a current-version frozen selection proof, without any I/O.

    This verifies the supplied ten-match evidence and all selection rules. It
    cannot prove that an omitted later match was absent from the original full
    history; only ``build`` over that full history establishes that fact.
    Historical stores should keep their frozen version and criteria when the
    live engine advances to a later rule version.
    """
    if not isinstance(row, dict) or row.get('selection_version') != VERSION or row.get('criteria') != CRITERIA:
        raise ValueError('Version ou critères de sélection figés absents ou invalides.')
    history = []
    for key in ('recent_home', 'recent_away'):
        recent = row.get(key)
        if not isinstance(recent, dict) or not isinstance(recent.get('rows'), list):
            raise ValueError('Les dix précédents justificatifs sont absents ou invalides.')
        history.extend(recent['rows'])
    rebuilt = build(dict(source=SOURCE, day=day, before=before, rows=[row]), history, now)
    if not rebuilt['rows']:
        detail = rebuilt['excluded'][0]['detail'] if rebuilt['excluded'] else 'Sélection invalide.'
        raise ValueError(detail)
    expected = rebuilt['rows'][0]
    if any(key not in row or row[key] != expected[key] for key in _PROOF_FIELDS):
        raise ValueError('La preuve figée ne correspond pas aux probabilités et aux dix précédents fournis.')
    return deepcopy(expected)
