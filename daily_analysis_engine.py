"""Deterministic daily candidate groups ranked by data support, without forecasts.

This policy constructs a bounded set of groups; it does not enumerate all
subsets or claim to identify the most accurate predictions.  For every nominal
size 2..20, the support-ranked pool is partitioned into disjoint full blocks.
Additional chronological four-hour buckets (blocks of at most 20) give time
diversity.  A final pair covers an otherwise isolated participant.  Groups are
deduplicated, and no global top-N limit is applied.

After sorting, construction and scoring cost O(19*N); the initial sort costs
O(N*log(N)).  Historical profiles are counted once.  No API, artifact, journal,
prediction, training or persistent-store access occurs in this module.
"""
from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import fsum
from zoneinfo import ZoneInfo

import api_football as api
import api_match_workspace as workspace
from match_predictor import MIN_DAYS, MIN_LEAGUE, MIN_TEAM, SHRINK


VERSION = 'daily-data-support-partitions-v1'
SOURCE = 'API-Football'
TIMEZONE = 'Africa/Kinshasa'
BUCKET_HOURS = 4
SUPPORT_FORMULA = (
    '100 * mean(0.50 * min(home_role,away_role)/(min(home_role,away_role)+8) '
    '+ 0.25 * league_matches/(league_matches+30) '
    '+ 0.25 * league_days/(league_days+3))'
)


class DailyAnalysisError(ValueError):
    """The local input or the requested daily clock is inconsistent."""


def _now(value):
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DailyAnalysisError('Une date avec fuseau horaire est nécessaire.')
    return value.astimezone(timezone.utc)


def _order(fixture):
    return fixture['timestamp'], int(fixture['match_id'])


def _identity(fixtures):
    return tuple(sorted(((str(row['match_id']), str(row['league'])) for row in fixtures),
                        key=lambda item: (int(item[0]), int(item[1]))))


def _id(identity):
    payload = json.dumps([VERSION, SOURCE, identity], separators=(',', ':'), ensure_ascii=False)
    return sha256(payload.encode()).hexdigest()


def _existing(items):
    ids, identities = set(), set()
    for item in items:
        if isinstance(item, str):
            ids.add(item)
        elif isinstance(item, dict):
            if isinstance(item.get('id'), str):
                ids.add(item['id'])
            fixtures = item.get('fixtures')
            if isinstance(fixtures, list) and fixtures:
                try:
                    identities.add(_identity(fixtures))
                except (ValueError, TypeError, KeyError):
                    raise DailyAnalysisError('Un groupe déjà enregistré contient des identifiants invalides.') from None
        else:
            raise DailyAnalysisError('La liste des groupes déjà enregistrés est invalide.')
    return ids, identities


def _unique(raw, diagnostics):
    unique = {}
    for item in raw:
        try:
            if not isinstance(item, dict) or item.get('provider') not in (None, 'api-football', SOURCE):
                raise ValueError()
            mid = api._numeric_id(item['fixture']['id'], 'du match')
            api._numeric_id(item['league']['id'], 'de compétition')
        except (KeyError, ValueError, TypeError):
            diagnostics['invalid_raw'] += 1
            continue
        if mid in unique and unique[mid] != item:
            raise DailyAnalysisError('Un identifiant API-Football apparaît avec des données contradictoires.')
        unique[mid] = item
    return list(unique.values())


def _history(raw, before, now, diagnostics):
    leagues = defaultdict(lambda: dict(matches=0, days=set(), home=Counter(), away=Counter()))
    issues = Counter()
    for item in raw:
        if api._status_short(item) not in api.FINISHED_STATUSES:
            continue
        try:
            stamp = workspace._stamp(item)
            # Ignore target-day/future outcomes before interpreting their scores.
            if stamp.date().isoformat() >= before or stamp >= now:
                continue
            row = api.normalize_fixture(item)
        except (KeyError, TypeError, ValueError, AttributeError):
            issues[str(item['league']['id'])] += 1
            continue
        value = leagues[row['league']]
        value['matches'] += 1
        value['days'].add(row['timestamp'][:10])
        # Names deliberately match the current clubs model's role profiles.
        value['home'][row['home']] += 1
        value['away'][row['away']] += 1
    diagnostics['history_issues'] = [dict(league=league, matches=count)
                                      for league, count in sorted(issues.items(), key=lambda item: int(item[0]))]
    return leagues


def _support(fixture, leagues):
    league = leagues.get(fixture['league'])
    n = league['matches'] if league else 0
    days = len(league['days']) if league else 0
    home = league['home'][fixture['home']] if league else 0
    away = league['away'][fixture['away']] if league else 0
    minimum_role = min(home, away)
    score = 100 * (.50 * minimum_role / (minimum_role + SHRINK)
                   + .25 * n / (n + MIN_LEAGUE)
                   + .25 * days / (days + MIN_DAYS))
    reasons = []
    if n < MIN_LEAGUE:
        reasons.append(f'{n}/{MIN_LEAGUE} résultats de compétition')
    if days < MIN_DAYS:
        reasons.append(f'{days}/{MIN_DAYS} jours historiques UTC')
    if home < MIN_TEAM or away < MIN_TEAM:
        reasons.append(f'{home}/{MIN_TEAM} précédents domicile et {away}/{MIN_TEAM} précédents extérieur')
    return dict(eligible=not reasons, history_matches=n, history_days=days,
                home_role_matches=home, away_role_matches=away,
                club_role_matches_min=minimum_role, data_support_index=score,
                reason=' ; '.join(reasons))


def plan(raw, now=None, existing=()):
    """Return all constructed daily candidates, excluding already frozen groups.

    ``raw`` contains API-Football raw fixtures, including local historical FT
    results. ``now`` is timezone-aware. ``existing`` accepts previous candidates,
    journal entries containing fixtures, or candidate IDs. Equal fixture sets
    are skipped even when an existing journal ID uses a different namespace.

    Admission requires 30 historical league results on three UTC days and three
    home/away-role precedents for both participants.  This is stricter than the
    clubs model's league-baseline fallback. A common conservative UTC cutoff,
    before the earliest confirmed upcoming fixture of the day, is used for all
    support counts. Model methods are chosen and frozen later by the coordinator.
    The coordinator must persist the first nonempty daily plan. Regenerating
    after kickoffs would create different combinations of the remaining games;
    ``existing`` excludes equal groups, but is not a replacement for that lock.
    """
    now = _now(now)
    zone = ZoneInfo(TIMEZONE)
    day = now.astimezone(zone).date().isoformat()
    raw = list(raw)
    diagnostics = dict(raw_fixtures=len(raw), invalid_raw=0, calendar_matches=0,
                       upcoming_confirmed=0, eligible_matches=0, existing_skipped=0,
                       excluded=[], exclusions={}, history_issues=[])
    raw = _unique(raw, diagnostics)
    upcoming, excluded_counts = [], Counter()
    for item in raw:
        try:
            stamp = workspace._stamp(item)
        except (ValueError, TypeError, AttributeError):
            diagnostics['invalid_raw'] += 1
            continue
        if stamp.astimezone(zone).date().isoformat() != day:
            continue
        diagnostics['calendar_matches'] += 1
        if api._status_short(item) != 'NS':
            excluded_counts['status_not_NS'] += 1
            continue
        if stamp <= now:
            excluded_counts['kickoff_passed'] += 1
            continue
        try:
            upcoming.append(workspace.fixture_from_raw(item))
        except (ValueError, TypeError, AttributeError):
            excluded_counts['unconfirmed_teams'] += 1
    upcoming.sort(key=_order)
    before = min((row['timestamp'][:10] for row in upcoming), default=now.date().isoformat())
    leagues = _history(raw, before, now, diagnostics)
    diagnostics['upcoming_confirmed'] = len(upcoming)
    support, eligible = {}, []
    for fixture in upcoming:
        state = _support(fixture, leagues)
        support[fixture['match_id']] = state
        if state['eligible']:
            eligible.append(fixture)
        else:
            excluded_counts['insufficient_profiles'] += 1
            diagnostics['excluded'].append(dict(match_id=fixture['match_id'], league=fixture['league'],
                home=fixture['home'], away=fixture['away'], reason=state['reason'], support=state))
    diagnostics['eligible_matches'] = len(eligible)
    diagnostics['exclusions'] = dict(sorted(excluded_counts.items()))
    existing_ids, existing_sets = _existing(existing)
    constructed = {}

    def add(group, strategy, nominal_size=None, bucket=None):
        if not 2 <= len(group) <= 20:
            return
        ordered = sorted(group, key=_order)
        identity = _identity(ordered)
        if identity in constructed:
            selection = constructed[identity]['selection']
            if strategy not in selection['strategies']:
                selection['strategies'].append(strategy)
            if nominal_size is not None and nominal_size not in selection['nominal_sizes']:
                selection['nominal_sizes'].append(nominal_size)
            return
        profiles = [support[row['match_id']] for row in ordered]
        score = fsum(row['data_support_index'] for row in profiles) / len(profiles)
        minimum_matches = min(row['history_matches'] for row in profiles)
        minimum_days = min(row['history_days'] for row in profiles)
        minimum_roles = min(row['club_role_matches_min'] for row in profiles)
        competitions = {row['league']: dict(league=row['league'],
            competition=row.get('competition') or row['league'], country=row.get('country') or 'International')
            for row in ordered}
        components = [f'Au moins {minimum_matches} résultats historiques par compétition.',
                      f'Au moins {minimum_days} jours historiques UTC.',
                      f'Au moins {minimum_roles} précédents dans le rôle de chaque club.',
                      'Indice d’appui des données ; précision de ces groupes non validée.']
        selection = dict(policy=VERSION, strategy=strategy, strategies=[strategy],
            nominal_sizes=[nominal_size] if nominal_size is not None else [],
            data_support_index=score, data_support_scale=100, formula=SUPPORT_FORMULA,
            before=before, components=components,
            support=dict(history_matches_min=minimum_matches, history_days_min=minimum_days,
                club_role_matches_min=minimum_roles, eligible_matches=len(ordered),
                data_support_min=min(row['data_support_index'] for row in profiles)),
            reason=' '.join(components), temporal_evidence=dict(available=False,
                scope='daily_group_construction', reason='Cette politique de sélection n’a pas de test temporel dédié.'))
        if bucket is not None:
            selection['time_bucket'] = bucket
        constructed[identity] = dict(id=_id(identity), size=len(ordered), fixtures=ordered,
            competitions=[competitions[key] for key in sorted(competitions, key=int)],
            mixed=len(competitions) > 1, selection=selection)

    ranked = sorted(eligible, key=lambda row: (-support[row['match_id']]['data_support_index'], *_order(row)))
    nominal_sizes = list(range(2, min(20, len(ranked)) + 1))
    for size in nominal_sizes:
        for index in range(0, len(ranked) - size + 1, size):
            add(ranked[index:index + size], 'support_partitions', nominal_size=size)
    buckets = defaultdict(list)
    for row in eligible:
        hour = workspace._stamp({'fixture': {'date': row['timestamp'] + 'Z'}}).astimezone(zone).hour
        buckets[hour // BUCKET_HOURS].append(row)
    for bucket, values in sorted(buckets.items()):
        for index in range(0, len(values), 20):
            add(values[index:index + 20], 'time_bucket', bucket=dict(
                start_hour=bucket * BUCKET_HOURS, end_hour=(bucket + 1) * BUCKET_HOURS))
    # Odd, prime-sized pools can leave the last ranked fixture in every remainder.
    covered = {row['match_id'] for group in constructed.values() for row in group['fixtures']}
    for fixture in eligible:
        if fixture['match_id'] in covered or len(eligible) < 2:
            continue
        stamp = datetime.fromisoformat(fixture['timestamp'])
        partner = min((row for row in eligible if row['match_id'] != fixture['match_id']),
            key=lambda row: (abs((datetime.fromisoformat(row['timestamp']) - stamp).total_seconds()), *_order(row)))
        add([fixture, partner], 'coverage_pair')
        covered.update((fixture['match_id'], partner['match_id']))

    candidates = []
    for identity, candidate in constructed.items():
        if candidate['id'] in existing_ids or identity in existing_sets:
            diagnostics['existing_skipped'] += 1
        else:
            candidates.append(candidate)
    candidates.sort(key=lambda row: (-row['selection']['data_support_index'],
        -row['selection']['support']['data_support_min'], row['fixtures'][0]['timestamp'], row['id']))
    diagnostics.update(constructed_groups=len(constructed), candidate_count=len(candidates),
        nominal_sizes=nominal_sizes, exhaustive=False,
        construction='Disjoint support partitions by nominal size, four-hour buckets, deduplication and isolated-match coverage.',
        ranking='Mean data-support index per match; not a probability, accuracy estimate or exhaustive optimum.')
    return dict(version=VERSION, source=SOURCE, day=day, timezone=TIMEZONE, before=before,
                candidates=candidates, diagnostics=diagnostics)
