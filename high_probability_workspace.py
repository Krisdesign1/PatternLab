"""Local probability search and explicit, immutable pre-match registration.

The list always uses the same Clubs model. Group snapshots reuse exactly the
displayed probabilities; no model selection, training or history import occurs.
"""

import cloud_storage
from collections import Counter, OrderedDict
from copy import deepcopy
from datetime import date, datetime, time, timezone
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
from threading import RLock
from zoneinfo import ZoneInfo

import api_football as api
import api_match_workspace as calendar
import high_probability_engine as engine
import prediction_journal as journal
import prospective_models as models
from match_predictor import VERSION as CLUB_VERSION


VERSION = 'high-probability-v3'
ZONE = ZoneInfo('Africa/Kinshasa')
_ARCHIVED_SCANS = OrderedDict()
_ARCHIVE_LOCK = RLock()


def _now(value=None):
    value = datetime.now(timezone.utc) if value is None else value
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('Une horloge avec fuseau horaire est nécessaire.')
    return value.astimezone(timezone.utc)


def _root(root):
    return Path(api.DATA_DIR if root is None else root)


@lru_cache(maxsize=4)
def _cached_targets(path, modified, size, target_day):
    return calendar.local_day_matches(date.fromisoformat(target_day), ZONE.key, path=path)


def _day(value, current):
    value = current.astimezone(ZONE).date() if value is None else value
    if type(value) is not date:
        raise ValueError('La date des matchs doit être une date de calendrier.')
    return value


@lru_cache(maxsize=4)
def _cached_scan(path, modified, size, period_start, target_day, history_before):
    # History has an exclusive UTC-day cutoff. Within that UTC/Kinshasa
    # calendar period, unchanged data yield exactly the same probabilities.
    # Include all NS targets from the period's start, then expire their
    # kickoffs on every read instead of rescanning the history every minute.
    path = Path(path)
    read_started=datetime.now(timezone.utc)
    source_signature=api.data_signature(path)
    current = datetime.fromisoformat(period_start)
    day = date.fromisoformat(target_day)
    if api.active_store(path) is not None:
        # Load targets inside this measured read window. The outer cache can
        # contain R1 even when the source already moved to R2 before this call.
        raw = calendar.local_day_matches(day,ZONE.key,path=path)
        leagues = set()
        for item in raw:
            try:
                leagues.add(calendar.fixture_from_raw(item)['league'])
            except ValueError:
                continue
        history, audit = api.load_matches(path, leagues=leagues,
            end=history_before, statuses=api.FINISHED_STATUSES)
        report = engine.scan(raw, now=current, day=day, history=history, history_issues=audit['issues'])
    else:
        report = engine.scan(api.load_fixtures(path), now=current, day=day)
    signature_end=api.data_signature(path)
    read_finished=datetime.now(timezone.utc)
    from probability_time_evidence import read_receipt
    report['source_read_receipt']=read_receipt(read_started,read_finished,source_signature,
        signature_end,report['before'],report['rows'])
    report['generated_at'] = read_finished.isoformat()
    return report


def load_scan(root=None, now=None, *, day=None):
    """Read locally; reuse probabilities until the file or either day changes."""
    current = _now(now)
    target_day = _day(day, current)
    path = _root(root) / 'fixtures.json'
    local_day = current.astimezone(ZONE).date()
    period_start = max(datetime.combine(local_day, time.min, ZONE).astimezone(timezone.utc),
                       datetime.combine(current.date(), time.min, timezone.utc)).isoformat()
    _, revision, identity = api.data_signature(path)
    targets = _cached_targets(str(path), revision, identity, target_day.isoformat())
    future_stamps = []
    for raw in targets:
        try:
            stamp = calendar._stamp(raw)
            calendar.fixture_from_raw(raw)
            if api._status_short(raw) == 'NS' and stamp > current:
                future_stamps.append(stamp)
        except ValueError:
            continue
    cutoff = min(current.date(), min(future_stamps, default=current).date()).isoformat()
    report = deepcopy(_cached_scan(str(path), revision, identity, period_start,
                                  target_day.isoformat(), cutoff))
    # A cached period must never keep a match whose kickoff has just passed.
    kept, expired = [], []
    for row in report['rows']:
        (kept if journal._stamp(row['fixture']['timestamp']) > current else expired).append(row)
    report['rows'] = kept
    expired_count = len(expired)
    for item in report['excluded']:
        fixture = item.get('fixture')
        if fixture and journal._stamp(fixture['timestamp']) <= current:
            expired_count += 1
            item.update(reason='kickoff_elapsed', detail='Horaire dépassé sans confirmation d’un match à venir.')
    report['excluded'].extend(dict(match_id=row['fixture']['match_id'], reason='kickoff_passed',
                                  detail='Le coup d’envoi est passé.', fixture=row['fixture']) for row in expired)
    report['diagnostics']['eligible_count'] = len(kept)
    report['diagnostics']['upcoming_count'] -= expired_count
    report['diagnostics']['excluded_counts'] = dict(Counter(item['reason'] for item in report['excluded']))
    report['checked_at'] = current.isoformat()
    report['calendar_updated_at'] = calendar._calendar_updates(path).get((report['day'], ZONE.key))
    return report


def archive_scan(report=None, root=None, clock=None, *, day=None):
    """Automatically retain the full locally calculated ranking before kickoff.

    Kept separate from load_scan's read-only contract. No probability threshold,
    search, selected category or group membership limits this archive.
    """
    import high_probability_archive as archive
    from probability_implementation import code_hashes
    clock = (lambda: datetime.now(timezone.utc)) if clock is None else clock
    current = _now(clock())
    if report is None:
        report = load_scan(root=root, now=current, day=day)
    if not report:
        return dict(created=0, existing=0, skipped=0)
    root = _root(root)
    fingerprint = _digest(dict(source=report.get('source'), version=report.get('version'),
        day=report.get('day'), before=report.get('before'),
        history=report.get('history_fingerprint'), rows=report.get('rows'),
        source_read_receipt=report.get('source_read_receipt'),model_code_sha256=code_hashes()))
    path = archive.database_path(root)
    def signature():
        values = []
        for name in (path, Path(str(path)+'-wal')):
            try:
                stat = name.stat()
                values.append((stat.st_ino, stat.st_size, stat.st_mtime_ns))
            except FileNotFoundError:
                values.append(None)
        return tuple(values)
    key = str(root.resolve())
    with _ARCHIVE_LOCK:
        previous = _ARCHIVED_SCANS.get(key)
        if previous and previous['fingerprint'] == fingerprint and previous['signature'] == signature():
            return dict(created=0, existing=previous['count'], skipped=0)
        result = archive.capture(report, root=root, clock=clock)
        if not result['skipped']:
            _ARCHIVED_SCANS[key] = dict(fingerprint=fingerprint, signature=signature(),
                                      count=result['created']+result['existing'])
            _ARCHIVED_SCANS.move_to_end(key)
            while len(_ARCHIVED_SCANS) > 8:
                _ARCHIVED_SCANS.popitem(last=False)
        return result


@cloud_storage.persisted
def refresh_archive_results(match_ids, root=None, fetch=None):
    """Explicit provider refresh for archived individual fixtures, in batches."""
    import high_probability_archive as archive
    root = _root(root)
    saved = {entry['fixture']['match_id'] for entry in archive.list_entries(root=root)}
    identities = sorted({str(mid) for mid in match_ids}, key=int)
    if not identities or not set(identities) <= saved:
        raise ValueError('Choisissez des matchs présents dans l’archive des probabilités.')
    key = ''
    if fetch is None:
        client, key = calendar._client()
        fetch = lambda **params: calendar._response_fixtures(client.fixtures(**params))
    try:
        for index in range(0, len(identities), 20):
            batch = identities[index:index+20]
            fresh = fetch(ids='-'.join(batch), timezone='UTC')
            if (not isinstance(fresh, list) or len(fresh) != len(batch) or
                    {str(row.get('fixture', {}).get('id')) for row in fresh} != set(batch)):
                raise ValueError('Réponse API incomplète : les résultats de ce lot ne sont pas modifiés.')
            api.save_fixtures(fresh, root / 'fixtures.json')
        return archive.reconcile(root=root)
    except api.ApiFootballError as error:
        raise api.ApiFootballError(calendar._safe(error, key)) from None


@cloud_storage.persisted
def refresh_scan(root=None, now=None, *, day=None):
    """One explicit calendar request, no training or multi-season imports."""
    current = _now(now)
    target_day = _day(day, current)
    calendar.refresh_day(target_day, timezone_name=ZONE.key, path=_root(root)/'fixtures.json')
    after = _now(now)
    if day is None and after.astimezone(ZONE).date() != target_day:
        raise ValueError('La journée a changé pendant l’actualisation ; relancez la recherche.')
    _cached_scan.cache_clear()
    _cached_targets.cache_clear()
    return load_scan(root=root, now=after, **({'day': target_day} if day is not None else {}))


def _digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def prepare_group(report, match_ids, now=None, *, outcome=None, minimum=0, choices=None):
    """Freeze the selected, displayed Clubs distributions without recalculating."""
    current = _now(now)
    day = date.fromisoformat(report.get('day', '')).isoformat()
    if report.get('source') != 'API-Football' or day < current.astimezone(ZONE).date().isoformat():
        raise ValueError('Relancez la recherche sur une date à venir à Kinshasa.')
    ids = [str(value) for value in match_ids]
    if len(ids) != len(set(ids)) or not 2 <= len(ids) <= 20:
        raise ValueError('Choisissez entre 2 et 20 matchs distincts.')
    if choices is not None:
        if (outcome is not None or not isinstance(choices, dict)
                or any(not isinstance(key, str) for key in choices)
                or set(choices) != set(ids) or any(value not in ('1st', 'Égal', '2nd') for value in choices.values())):
            raise ValueError('Choisissez exactement un résultat 1st, Égal ou 2nd pour chaque match du groupe.')
    available = {row['fixture']['match_id']: row for row in engine.filter_rows(
        report, minimum=0 if choices is not None else minimum, outcome=outcome)}
    # Validate a custom threshold even when each selected fixture has its own outcome.
    if choices is not None:
        engine.filter_rows(report, minimum=minimum)
    if any(identity not in available for identity in ids):
        raise ValueError('Une affiche ne figure plus dans les résultats de la recherche.')
    chosen = sorted((deepcopy(available[identity]) for identity in ids),
                    key=lambda row: (row['fixture']['timestamp'], int(row['fixture']['match_id'])))
    if choices is not None:
        for row in chosen:
            row['search_outcome'] = choices[row['fixture']['match_id']]
            row['search_probability'] = row['probabilities'][row['search_outcome']]
            if row['search_probability'] < minimum:
                raise ValueError('Un résultat choisi est inférieur au seuil demandé.')
    fixtures = journal._fixtures([row['fixture'] for row in chosen], allow_mixed=True)
    if any(journal._stamp(row['timestamp']).astimezone(ZONE).date().isoformat() != day for row in fixtures):
        raise ValueError('Les rencontres doivent appartenir à la date de la recherche.')
    journal._check_time(fixtures, current, True)
    history_before = report['before']
    if (not isinstance(history_before, str) or date.fromisoformat(history_before).isoformat() != history_before):
        raise ValueError('La coupure historique doit être une journée UTC canonique.')
    if history_before > fixtures[0]['timestamp'][:10] or history_before > current.date().isoformat():
        raise ValueError('La coupure historique doit précéder les rencontres à prévoir.')
    context = dict(models.group_context(fixtures, decision_at=current), training_before=history_before)
    grouped = {}
    for index, row in enumerate(chosen):
        if row.get('personalized') is not True:
            raise ValueError('Les profils des deux clubs doivent être exploitables.')
        if row.get('before') != history_before:
            raise ValueError('Les probabilités sélectionnées doivent utiliser la coupure historique de la recherche.')
        grouped.setdefault(fixtures[index]['league'], []).append((index, row))
    leagues = sorted(grouped, key=int)
    components, competitions = [], {}
    for league in leagues:
        members = grouped[league]
        details = [{key: row[key] for key in ('probabilities', 'baseline', 'home_profile', 'away_profile',
                   'personalized', 'training', 'before') if key in row} for _, row in members]
        probabilities = [row['probabilities'] for _, row in members]
        fingerprints = {row['history_fingerprint'] for _, row in members if row.get('history_fingerprint')}
        if len(fingerprints) > 1:
            raise ValueError('Les profils de cette compétition utilisent des historiques différents.')
        child_context = models._context([fixtures[index] for index, _ in members], history_before, current)
        provenance = dict(source='API-Football', league=league, **child_context,
                          history_cutoff_exclusive=history_before,
                          history_fingerprint=next(iter(fingerprints), _digest(details)),
                          training_matches=members[0][1]['training'], training_groups=0)
        distribution = models._distribution('independent', len(members), distributions=probabilities)
        method = models._method(CLUB_VERSION, distribution, provenance,
                                _component=len(members) == 1, club_estimates=details)
        components.append(dict(league=league, indices=[index for index, _ in members], method=method))
        fixture = members[0][1]['fixture']
        competitions[league] = dict(competition=fixture.get('competition') or league,
                                    country=fixture.get('country') or 'International')
    provenance = dict(source='API-Football', provider='api-football', league='mixed', leagues=leagues,
                      group_kind='mixed', **context, history_cutoff_exclusive=history_before,
                      fixtures=fixtures, assumes_block_independence=True,
                      correction_scope='within_each_league_only', selection_policy=VERSION)
    distribution = dict(schema=models.DISTRIBUTION_VERSION, type='block_product', size=len(fixtures),
                        components=components)
    method = models._method(models.MIXED_VERSION, distribution, provenance,
                            generation=_digest([component['method'] for component in components]))
    chances = [row['search_probability'] for row in chosen]
    sequence = [row['search_outcome'] for row in chosen]
    requested = (dict(sequence=sequence, probability=models.probability_for_sequence(method, sequence))
                 if choices is not None or outcome is not None else None)
    search_kind = 'mixed' if choices is not None else outcome
    selection = dict(policy=VERSION, match_day=day,
                     ranking_metric=('custom_outcome_clubs_probability' if choices is not None else
                                     'outcome_clubs_probability' if outcome is not None else 'maximum_clubs_probability'),
                     searched_outcome=outcome, requested_scenario=requested,
                     minimum_match_probability=min(chances), maximum_match_probability=max(chances),
                     search=dict(outcome=search_kind, minimum=minimum),
                     before=history_before, match_probabilities=[dict(match_id=row['fixture']['match_id'],
                         probabilities=row['probabilities'], choice=row['choice'],
                         max_probability=row['max_probability'], search_outcome=row['search_outcome'],
                         search_probability=row['search_probability'], baseline=row.get('baseline'),
                         training=row.get('training'), history_days=row.get('history_days'),
                         home_profile=row.get('home_profile'), away_profile=row.get('away_profile'),
                         delta_points=(100*(row['search_probability']-row['baseline'][row['search_outcome']])
                                       if row['search_outcome'] and row.get('baseline') else None)) for row in chosen],
                     reason=('Choix personnalisés pour chaque rencontre, à partir des probabilités Clubs, '
                             if choices is not None else
                             'Matchs choisis selon la probabilité Clubs de « ' + outcome + ' », '
                             if outcome is not None else
                             'Matchs choisis dans le classement des probabilités Clubs décroissantes, ') +
                            'toutes compétitions confondues. Les probabilités du groupe sont calculées '
                            'sous une hypothèse d’indépendance des rencontres.')
    return dict(source='API-Football', day=day, fixtures=fixtures, methods={'clubs': method},
                metadata=dict(selected_engine='clubs', daily_policy=VERSION, selection=selection,
                              competitions=competitions), requested_scenario=requested,
                created_at=current.isoformat())


@cloud_storage.persisted
def record_group(prepared, root=None, clock=None, fetch=None):
    """Fresh provider preflight followed by the existing write-once journal."""
    root = _root(root)
    clock = (lambda: datetime.now(timezone.utc)) if clock is None else clock
    fixtures = journal._fixtures(prepared['fixtures'], allow_mixed=True)
    existing = journal.find_mixed(fixtures, path=root/'predictions.sqlite3')
    requested = prepared.get('metadata', {}).get('selection', {}).get('requested_scenario')
    if existing and requested is None:
        return dict(existing, created=False)
    if requested is not None:
        models._sequence(requested.get('sequence'), len(fixtures))
    if existing:
        old = {row['match_id']: row for row in existing['fixtures']}
        if any(any(str(row[field]) != str(old[row['match_id']][field])
                   for field in ('league', 'home_id', 'away_id', 'home', 'away', 'timestamp')) for row in fixtures):
            raise ValueError('Les affiches ont changé depuis la première prévision figée de ce groupe.')
    current = _now(clock())
    day = date.fromisoformat(prepared.get('day', '')).isoformat()
    if (prepared.get('source') != 'API-Football' or day < current.astimezone(ZONE).date().isoformat()
            or any(journal._stamp(row['timestamp']).astimezone(ZONE).date().isoformat() != day for row in fixtures)):
        raise ValueError('Cette analyse ne concerne plus une date à venir valide.')
    journal._check_time(fixtures, current, True)
    for payload in prepared['methods'].values():
        models.validate_method(payload, len(fixtures))
    key = ''
    try:
        if fetch is None:
            client, key = calendar._client()
            fresh = calendar._response_fixtures(client.fixtures(
                ids='-'.join(row['match_id'] for row in fixtures), timezone='UTC'))
        else:
            fresh = fetch(ids='-'.join(row['match_id'] for row in fixtures), timezone='UTC')
        if (not isinstance(fresh, list) or len(fresh) != len(fixtures)
                or any(not isinstance(row, dict) or not isinstance(row.get('fixture'), dict) for row in fresh)
                or {str(row['fixture'].get('id')) for row in fresh} != {row['match_id'] for row in fixtures}):
            raise ValueError('API-Football ne confirme pas exactement les matchs de cette analyse.')
        # Validate identities/status before persisting the provider response.
        by_id = {str(row['fixture']['id']): row for row in fresh}
        for fixture in fixtures:
            raw = by_id[fixture['match_id']]
            confirmed = calendar.fixture_from_raw(raw)
            if api._status_short(raw) != 'NS' or any(
                    (str(confirmed[field]).strip() if field in ('home', 'away') else str(confirmed[field])) != str(fixture[field])
                    for field in ('league', 'home_id', 'away_id', 'home', 'away', 'timestamp')):
                raise ValueError('Une affiche, son statut ou son horaire a changé : actualisez la recherche.')
        journal._check_time(fixtures, _now(clock()), True)
        api.save_fixtures(fresh, root/'fixtures.json')
        metadata = deepcopy(prepared['metadata'])
        metadata['calendar_checked_at'] = _now(clock()).isoformat()
        entry = (dict(existing, created=False) if existing else
                 journal.record_mixed(fixtures, prepared['methods'], path=root/'predictions.sqlite3',
                                      metadata=metadata, raw_fixtures=fresh, clock=clock))
        if requested is not None:
            import probability_scenario_store as intents
            intent = intents.record(entry, prepared['metadata']['selection'],
                                    path=root/'probability_scenarios.sqlite3', clock=clock)
            return dict(entry, forecast_created=entry['created'], created=intent['created'], scenario_intent=intent)
        return entry
    except api.ApiFootballError as error:
        raise api.ApiFootballError(calendar._safe(error, key)) from None
