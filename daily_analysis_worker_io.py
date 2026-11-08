"""Versioned streaming I/O for the unchanged daily-analysis policy.

Only the Linux collector opts into this adapter. Its initial plan scans one
SQLite snapshot twice, keeping the selected calendar and sufficient historical
club profiles instead of the full raw population. Group construction and worker
control flow are an explicit static copy of the original policy; mathematical
helpers, models, storage, locking and settlement remain owned by their original
modules. Existing policy IDs and enrolled study source hashes are not rewritten.
JSON remains a compatibility path with the original full-load semantics.
"""
from collections import Counter, defaultdict
from datetime import date, datetime
from math import fsum
from zoneinfo import ZoneInfo
import json

import daily_analysis_engine as engine
import daily_analysis_worker as original
import fixture_store


IO_VERSION = 'daily-analysis-worker-streamed-io-v1'
VERSION = IO_VERSION
POLICY_VERSION = original.VERSION
api, workspace, journal, models = original.api, original.workspace, original.journal, original.models
cloud_storage, ZONE = original.cloud_storage, original.ZONE
STATE_NAME, STATUS_NAME = original.STATE_NAME, original.STATUS_NAME
DailyAnalysisError = original.DailyAnalysisError
_root, _clock, _lock, _state = original._root, original._clock, original._lock, original._state
_current, _candidate, _write, _report = original._current, original._candidate, original._write, original._report
load_status, load_selections = original.load_status, original.load_selections
_order, _identity, _id = engine._order, engine._identity, engine._id
_existing, _support = engine._existing, engine._support
SUPPORT_FORMULA, BUCKET_HOURS = engine.SUPPORT_FORMULA, engine.BUCKET_HOURS


def _canonical_rows(connection, diagnostics=None):
    """Yield valid canonical identities in original ordinal order.

    Canonical storage has one primary-key row per numeric fixture ID. Check
    that its raw identity still agrees with that key before relying on this
    uniqueness; reject an inconsistent store rather than silently deduplicating
    or retaining all raw payloads for comparison.
    """
    for stored in fixture_store._selected_rows(connection, 'match_id,raw_json', {}):
        item = json.loads(stored['raw_json'])
        counter = diagnostics if diagnostics is not None else dict(invalid_raw=0)
        if diagnostics is not None:
            diagnostics['raw_fixtures'] += 1
        values = engine._unique([item], counter)
        if not values:
            continue
        if api._numeric_id(item['fixture']['id'], 'du match') != stored['match_id']:
            raise engine.DailyAnalysisError('Une identité brute contredit la clé du stockage local.')
        yield item


def plan_store(path, now=None, existing=(), *, day=None):
    """Return the original complete plan and its initial calendar snapshot.

    Both passes use one SQLite read transaction, so a concurrent import cannot
    mix calendar and historical generations. All global diagnostics are kept,
    including invalid records and historical errors in unrelated competitions.
    """
    database = api.active_store(path)
    if database is None:
        raw = api.load_fixtures(path)
        planned = engine.plan(raw, now=now, existing=existing, day=day)
        wanted = {row['match_id'] for candidate in planned['candidates'] for row in candidate['fixtures']}
        return planned, [item for item in raw if str(item.get('fixture', {}).get('id')) in wanted]
    now = engine._now(now)
    zone = ZoneInfo(engine.TIMEZONE)
    today = now.astimezone(zone).date()
    if day is not None and (type(day) is not date or day < today):
        raise engine.DailyAnalysisError('Choisissez aujourd’hui ou une date à venir pour préparer une analyse.')
    day = (day or today).isoformat()
    diagnostics = dict(raw_fixtures=0, invalid_raw=0, calendar_matches=0,
                       upcoming_confirmed=0, eligible_matches=0, existing_skipped=0,
                       excluded=[], exclusions={}, history_issues=[])
    upcoming, calendar, excluded_counts = [], [], Counter()
    try:
        with fixture_store._connection(database) as connection:
            fixture_store._meta(connection)
            for item in _canonical_rows(connection, diagnostics):
                try:
                    stamp = workspace._stamp(item)
                except (ValueError, TypeError, AttributeError):
                    diagnostics['invalid_raw'] += 1
                    continue
                if stamp.astimezone(zone).date().isoformat() != day:
                    continue
                calendar.append(item)
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
            before = min(now.date().isoformat(),
                         min((row['timestamp'][:10] for row in upcoming), default=now.date().isoformat()))
            leagues = engine._history(_canonical_rows(connection), before, now, diagnostics)
    except fixture_store.FixtureStoreError as error:
        raise api.ApiFootballError(str(error)) from None
    diagnostics['upcoming_confirmed'] = len(upcoming)
    planned = _construct_plan(upcoming, leagues, before, day, zone, existing,
                              diagnostics, excluded_counts)
    return planned, calendar


def _construct_plan(upcoming, leagues, before, day, zone, existing, diagnostics, excluded_counts):
    # Static copy of daily_analysis_engine.plan's construction, kept byte-for-
    # byte in arithmetic/order operations and tested differentially below.
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
        selection = dict(policy=engine.VERSION, strategy=strategy, strategies=[strategy],
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
    return dict(version=engine.VERSION, source=engine.SOURCE, day=day, timezone=engine.TIMEZONE, before=before,
                candidates=candidates, diagnostics=diagnostics)


@cloud_storage.persisted
def run(root, clock, fetch=None, *, max_jobs=None, check_time=None, day=None):
    """Prepare the finite frozen queue using provider-checked fetches.

    ``fetch(**params)`` returns raw fixture dictionaries, e.g. a worker Gate's
    ``call``. Without it no forecast is written. All anchors are committed
    before the first calculation, and completed/expired anchors never move.
    An explicit positive ``max_jobs`` can limit a caller's batch. By default
    every candidate is attempted. ``check_time`` lets a host stop between
    calculations without losing the plan. Pauses save progress before raising.
    """
    if max_jobs is not None and (type(max_jobs) is not int or max_jobs < 1):
        raise DailyAnalysisError('Le nombre de calculs explicite doit être un entier positif.')
    if (not callable(clock) or fetch is not None and not callable(fetch) or
            check_time is not None and not callable(check_time)):
        raise DailyAnalysisError('Horloge ou actualisation quotidienne invalide.')
    root = _root(root)
    current = _clock(clock)
    today = current.astimezone(ZONE).date()
    if day is not None and (type(day) is not date or day < today):
        raise DailyAnalysisError('Choisissez aujourd’hui ou une date à venir pour préparer une analyse.')
    requested_day = day
    day = (day or today).isoformat()
    report = dict(version=POLICY_VERSION, source='API-Football', day=day, status='completed',
                  started_at=current.isoformat(), created=0, already_recorded=0, attempted=0,
                  diagnostics=[], jobs=[])
    with _lock(root):
        state = _state(root)
        jobs = state['days'].setdefault(day, {})
        try:
            # A frozen plan only needs its own calendar identities. Loading
            # every historical raw fixture on each resume defeats SQLite's
            # indexed reads and delays every future preflight.
            target_ids = {row['match_id'] for job in jobs.values() for row in job['fixtures']}
            raw = api.load_fixtures(root/'fixtures.json', ids=sorted(target_ids, key=int)) if jobs else []
            commits = state.setdefault('plan_commits', {})
            if day not in commits and not jobs:
                if check_time is not None:
                    check_time()
                planned, raw = plan_store(root/'fixtures.json', now=current, existing=(),
                                      **({'day': requested_day} if requested_day is not None else {}))
                if planned.get('day') != day or not isinstance(planned.get('candidates'), list):
                    raise DailyAnalysisError('Le plan ne correspond pas à la journée de Kinshasa.')
                report['diagnostics'] = planned.get('diagnostics', [])
                report['engine_version'] = planned.get('version')
                for value in planned['candidates']:
                    anchor = _candidate(value, day, current.isoformat())
                    jobs.setdefault(value['id'], anchor)
                if jobs:
                    commits[day] = dict(planned_at=current.isoformat(), engine_version=planned.get('version'),
                                        diagnostics=planned.get('diagnostics', []))
            else:
                commit = commits.get(day, {})
                report['engine_version'] = commit.get('engine_version')
                report['diagnostics'] = commit.get('diagnostics', [])
            report['plan_frozen'] = bool(jobs)
            target_ids = {row['match_id'] for job in jobs.values() for row in job['fixtures']}
            raw = [row for row in raw if str(row.get('fixture', {}).get('id')) in target_ids]
            target_leagues = {row['league'] for job in jobs.values() for row in job['fixtures']}
            _write(root/STATE_NAME, state)  # durable BEFORE build or provider preflight
            history = None
            # Round-robin by last attempt: an insufficient first group cannot
            # permanently consume every CPU slot ahead of other candidates.
            queue = sorted(jobs.items(), key=lambda pair: (pair[1].get('last_attempt_at', ''),
                            pair[1]['fixtures'][0]['timestamp'], pair[0]))
            for identity, job in queue:
                if check_time is not None:
                    check_time()
                fixtures = job['fixtures']
                existing = journal.find_mixed(fixtures, path=root/'predictions.sqlite3')
                if existing:
                    job.update(state='recorded', entry_id=existing['id'], reason='Première prévision déjà verrouillée.')
                    report['already_recorded'] += 1
                    continue
                if job['state'] in ('recorded', 'expired', 'changed'):
                    continue
                eligible, reason = _current(day, fixtures, raw, _clock(clock))
                if not eligible:
                    job.update(state='expired', reason=reason)
                    continue
                if max_jobs is not None and report['attempted'] >= max_jobs:
                    report['status'] = 'pending'
                    continue
                if fetch is None:
                    job.update(state='awaiting_preflight', reason='Une actualisation explicite est nécessaire avant enregistrement.')
                    report['status'] = 'awaiting_preflight'
                    continue
                report['attempted'] += 1
                job['last_attempt_at'] = _clock(clock).isoformat()
                _write(root/STATE_NAME, state)
                _write(root/STATUS_NAME, dict(report, status='running', candidates=len(jobs),
                    current_candidate_id=identity, remaining=sum(value['state'] not in
                        ('recorded', 'expired', 'changed') for value in jobs.values())))
                if history is None:
                    # A future calendar date must not turn into a future data
                    # cutoff. Keep the same complete UTC-day boundary as the
                    # planner, with the real run time as an upper bound.
                    history = [row for row in api.load_matches(root/'fixtures.json',
                               leagues=target_leagues, end=current.date().isoformat(),
                               statuses=api.FINISHED_STATUSES)[0]
                               if journal._stamp(row['timestamp']).date() < current.date()]
                if check_time is not None:
                    check_time()
                try:
                    frozen = models.build_mixed_methods(history, fixtures, decision_at=current)
                except (ValueError, KeyError, TypeError, OSError):
                    job.update(state='insufficient', reason='L’historique ou les moteurs sont indisponibles pour ce groupe.')
                    continue
                if check_time is not None:
                    check_time()
                if not frozen.get('methods'):
                    job.update(state='insufficient', reason='Aucune méthode commune disponible pour ce groupe.',
                               unavailable=frozen.get('unavailable', {}))
                    continue
                eligible, reason = _current(day, fixtures, raw, _clock(clock))
                if not eligible:
                    job.update(state='expired', reason=reason)
                    continue
                _write(root/STATE_NAME, state)
                try:
                    fresh = fetch(ids='-'.join(row['match_id'] for row in fixtures), timezone='UTC')
                except api.ApiFootballError:
                    job.update(state='awaiting_preflight', reason='La confirmation API est indisponible ; nouvelle tentative ultérieure.')
                    report['status'] = 'pending'
                    break
                try:
                    ids = [str(row['fixture']['id']) for row in fresh] if isinstance(fresh, list) else []
                except (KeyError, TypeError):
                    ids = []
                if len(ids) != len(fixtures) or set(ids) != {row['match_id'] for row in fixtures}:
                    job.update(state='awaiting_preflight', reason='La réponse API ne confirme pas exactement toutes les affiches.')
                    continue
                api.save_fixtures(fresh, root/'fixtures.json')
                raw = api.load_fixtures(root/'fixtures.json', ids=sorted(target_ids, key=int))
                eligible, reason = _current(day, fixtures, fresh, _clock(clock))
                if not eligible:
                    job.update(state='changed', reason=reason)
                    continue
                try:
                    entry = journal.record_mixed(fixtures, frozen['methods'], path=root/'predictions.sqlite3',
                        raw_fixtures=fresh, clock=clock, metadata=dict(selected_engine='automation',
                            unavailable=frozen.get('unavailable', {}), daily_policy=POLICY_VERSION,
                            selection=job['selection'], competitions=job['competitions'],
                            automation_day=day, automation_timezone=ZONE.key,
                            calendar_checked_at=_clock(clock).isoformat()))
                except (journal.JournalError, ValueError, OSError):
                    eligible, reason = _current(day, fixtures, fresh, _clock(clock))
                    job.update(state='awaiting_record' if eligible else 'changed',
                               reason='L’enregistrement est indisponible ; nouvelle tentative avant match.' if eligible else reason)
                    continue
                job.update(state='recorded', entry_id=entry['id'], methods=list(entry['methods']),
                           reason='Prévision verrouillée avant le premier coup d’envoi.')
                report['created'] += int(entry['created'])
                report['already_recorded'] += int(not entry['created'])
                _write(root/STATE_NAME, state)
        except cloud_storage.CloudStorageError:
            raise
        except Exception:
            # Gate quota pauses and unexpected failures must not erase anchors.
            report['status'] = 'paused'
            _report(root, state, day, report, clock)
            raise
        if any(job['state'] == 'insufficient' for job in jobs.values()) and report['status'] == 'completed':
            report['status'] = 'waiting_history'
        elif any(job['state'] not in ('recorded', 'expired', 'changed') for job in jobs.values()) and report['status'] == 'completed':
            report['status'] = 'pending'
        return _report(root, state, day, report, clock)
