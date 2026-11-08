"""Versioned scoped-I/O execution of the original daily scheduling policy.

The frozen automation_worker.py remains the reference. Operational provenance
is recorded separately for this explicitly qualified execution revision.

API-only daily pipeline, independent from a Streamlit session.

All known competitions participate. Calendar collection and forecasts concern
today's Africa/Kinshasa fixtures only. Group anchors persist across updates;
no group is chosen on results and no forecast is backfilled. Isolated daily
candidates advance between provider tasks without replacing serving models.
"""

import cloud_storage
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import math
import os
import sqlite3
from pathlib import Path
import tempfile
import time
from zoneinfo import ZoneInfo

import api_football as api
import api_match_workspace as workspace
import prediction_journal as journal
import competition_readiness as readiness
import automation_health as health
import automation_worker as reference_worker
import worker_fixture_io as worker_io

EXECUTION_IO_VERSION = "worker-scoped-io-v1"


VERSION = 'all-competitions-daily-blocks-v1'
DEFAULT_CONFIG = dict(enabled=True, sizes=[2, 4, 6, 8], interval_minutes=15,
                      history_seasons=[2022, 2023, 2024, 2025, 2026],
                      history_enabled=True, models_enabled=True, model_jobs_per_cycle=1)
TIMEZONE = workspace.DEFAULT_TIMEZONE
# Scheduling cadence, not a budget: every history item is still processed.
# One provider request / one forecast calculation remains indivisible.
DAY_REFRESH_SECONDS = 60
DAY_REFRESH_POLICY = 'civil-or-monotonic-refresh-v2'


# Downstream hooks import these scheduler exceptions from the reference module.
AutomationError = reference_worker.AutomationError
_Pause = reference_worker._Pause


def _root(root=None):
    return Path(root) if root is not None else api.DATA_DIR


def _read(path, default):
    if not path.exists():
        return default
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError):
        raise AutomationError(f'Fichier {path.name} invalide : traitement suspendu.') from None


@cloud_storage.persisted
def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.' + path.name, delete=False) as handle:
            name = handle.name
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if name is not None and Path(name).exists():
            Path(name).unlink()


def _config(value):
    legacy = {'daily_request_budget', 'cycle_request_budget', 'history_requests_per_cycle'}
    if not isinstance(value, dict) or set(value) - set(DEFAULT_CONFIG) - legacy:
        raise AutomationError('Réglages d’automatisation invalides.')
    # Read old saved settings without allowing their former caps to survive.
    # A previous explicit history stop remains a stop; positive counts become
    # ordinary enablement, with no maximum number of imports per passage.
    for field in legacy & set(value):
        minimum = 0 if field == 'history_requests_per_cycle' else 1
        if type(value[field]) is not int or value[field] < minimum:
            raise AutomationError(f'Valeur invalide : {field}.')
    result = dict(DEFAULT_CONFIG, **{key: val for key, val in value.items() if key not in legacy})
    if 'history_enabled' not in value and 'history_requests_per_cycle' in value:
        result['history_enabled'] = value['history_requests_per_cycle'] > 0
    if any(type(result[field]) is not bool for field in ('enabled', 'models_enabled', 'history_enabled')):
        raise AutomationError('L’activation doit être un booléen.')
    for field, low, high in (('interval_minutes', 1, 1440), ('model_jobs_per_cycle', 0, 1)):
        if type(result[field]) is not int or not low <= result[field] <= high:
            raise AutomationError(f'Valeur invalide : {field}.')
    for field, low, high in (('sizes', 2, 20), ('history_seasons', 1900, datetime.now(timezone.utc).year)):
        values = result[field]
        if (not isinstance(values, list) or not values or
                any(type(v) is not int or not low <= v <= high for v in values)):
            raise AutomationError(f'Valeurs invalides : {field}.')
        result[field] = sorted(set(values))
    return result


def load_config(root=None):
    return _config(_read(_root(root) / 'automation_config.json', {}))


@cloud_storage.persisted
def save_config(config, root=None):
    value = _config(config)
    _write(_root(root) / 'automation_config.json', value)
    return value


def load_status(root=None):
    return _read(_root(root) / 'automation_status.json', {})


def _time(now=None):
    if now is None:
        return datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise AutomationError('Une date avec fuseau horaire est nécessaire.')
    return now.astimezone(timezone.utc)


def _local_day(now):
    return now.astimezone(ZoneInfo(TIMEZONE)).date()


def _same_day(day, clock):
    if _local_day(clock()) != day:
        raise _Pause('La journée de Kinshasa a changé pendant le cycle ; reprise au prochain passage pour les matchs du jour.')


@contextmanager
def _lock(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'automation.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise _Pause('Un cycle est déjà en cours.') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _inventory(raw, now):
    info, rows, issues = {}, defaultdict(list), Counter()
    for item in raw:
        try:
            league = api._numeric_id(item['league']['id'], 'de compétition')
            info[league] = dict(league=league, competition=item['league'].get('name') or league,
                                country=item['league'].get('country') or 'International')
        except (KeyError, TypeError, ValueError):
            continue
        if api._status_short(item) in api.FINISHED_STATUSES:
            try:
                row = api.normalize_fixture(item)
                # Today's outcomes cannot help today's pre-match selections.
                if row['timestamp'][:10] < now.date().isoformat():
                    rows[league].append(row)
            except (ValueError, TypeError):
                issues[league] += 1
    for values in rows.values():
        values.sort(key=lambda row: (row['timestamp'], int(row['match_id'])))
    return info, rows, issues


def _plan(raw, days, sizes, plans):
    buckets, raw_ids = defaultdict(list), {}
    for item in raw:
        try:
            mid = api._numeric_id(item['fixture']['id'], 'du match')
            league = api._numeric_id(item['league']['id'], 'de compétition')
            stamp = workspace._stamp(item)
            day = stamp.astimezone(ZoneInfo(TIMEZONE)).date().isoformat()
        except (KeyError, TypeError, ValueError):
            continue
        raw_ids[mid] = item
        if day in days:
            buckets[(day, league)].append((stamp, int(mid), mid))
    groups = []
    for (day, league), values in sorted(buckets.items()):
        ordered = [item[2] for item in sorted(values)]
        for size in sizes:
            key = f'{day}/{league}/{size}'
            blocks = plans.setdefault(key, [])
            assigned = {mid for block in blocks for mid in block}
            available = [mid for mid in ordered if mid not in assigned]
            blocks.extend(available[i:i+size] for i in range(0, len(available)-size+1, size))
            groups.extend(dict(day=day, league=league, size=size, ids=block) for block in blocks)
    return groups, raw_ids


def _fixtures(group, raw_ids, now):
    if group['day'] != _local_day(now).isoformat():
        return None
    rows = []
    for mid in group['ids']:
        item = raw_ids.get(mid)
        try:
            if (api._status_short(item) != 'NS' or workspace._stamp(item) <= now or
                    workspace._stamp(item).astimezone(ZoneInfo(TIMEZONE)).date().isoformat() != group['day']):
                raise ValueError()
            fixture = workspace.fixture_from_raw(item)
            if fixture['league'] != group['league']:
                raise ValueError()
            rows.append(fixture)
        except (ValueError, TypeError, AttributeError):
            return None
    return sorted(rows, key=lambda row: (row['timestamp'], int(row['match_id'])))


def coverage(root=None, config=None, now=None):
    """Today's local fixtures, with all prior history for those competitions."""
    root, config, now = _root(root), _config(config if config is not None else load_config(root)), _time(now)
    raw = api.load_fixtures(root / 'fixtures.json')
    info, rows, issues = _inventory(raw, now)
    today = _local_day(now)
    day_raw = workspace._day_rows(raw, today, TIMEZONE)
    day_info, _, _ = _inventory(day_raw, now)
    plans = _read(root / 'automation_state.json', {}).get('plans', {})
    groups, raw_ids = _plan(raw, [today.isoformat()], config['sizes'], plans)
    upcoming = defaultdict(list)
    for group in groups:
        fixtures = _fixtures(group, raw_ids, now)
        if fixtures:
            upcoming[group['league']].append((group['size'], fixtures[0]['timestamp'][:10]))
    counts = Counter(str(r.get('league', {}).get('id')) for r in day_raw)
    output = []
    for code in sorted(day_info, key=int):
        # Today's outcomes remain excluded from the historical population.
        cutoffs = {before for _, before in upcoming[code]} or {now.date().isoformat()}
        assessments = {before: {r['size']: r for r in readiness.assess_sizes(
            rows[code], code, config['sizes'], before)} for before in cutoffs}
        size_rows = []
        ready = total = 0
        for size in config['sizes']:
            applicable = [before for length, before in upcoming[code] if length == size]
            before = min(applicable) if applicable else min(cutoffs)
            detail = dict(assessments[before][size])
            n = len(applicable)
            r = sum(assessments[day][size]['eligible'] for day in applicable)
            detail.update(eligible_groups=n, ready_groups=r, pending_groups=n-r)
            size_rows.append(detail)
            ready += r
            total += n
        state = 'no_upcoming' if total == 0 else 'ready' if ready == total else 'partial' if ready else 'insufficient'
        label = {'ready':'Prête pour Clubs + répétitions', 'partial':'Prête pour certaines tailles',
                 'insufficient':'Historique statistique à compléter', 'no_upcoming':'Sans groupe à venir'}[state]
        waiting = [r for r in size_rows if r['pending_groups']]
        reason = (f'{ready}/{total} groupes à venir disposent de l’historique statistique requis.' if total else
                  'Aucun bloc complet de matchs à venir pour les tailles choisies aujourd’hui à Kinshasa.')
        if waiting:
            reason += ' ' + min(waiting, key=lambda r:r['missing_matches'])['reason']
        output.append(dict(day_info[code], history_matches=len(rows[code]), issues=issues[code], fixtures=counts[code],
                           eligible_groups=total, ready_groups=ready, pending_groups=total-ready,
                           ready_sizes=[r['size'] for r in size_rows if r['ready_groups']],
                           missing_matches=min((r['missing_matches'] for r in waiting), default=0) if total else None,
                           readiness=state, readiness_label=label, reason=reason, per_size=size_rows))
    return output


class _Gate:
    """Record calls before network; honor provider quotas and the host deadline."""
    def __init__(self, root, state, config, client, clock, report, *, deadline=None):
        self.root, self.state, self.config, self.client, self.clock, self.report = root, state, config, client, clock, report
        self.deadline = deadline
        self.forecast_jobs = {}
        self.created_ids = set()
        self.reported_existing_ids = set()

    def save(self):
        _write(self.root / 'automation_state.json', self.state)

    def check_time(self):
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise _Pause('Le passage se termine pour sauvegarder les données avant l’arrêt de l’hébergeur ; '
                         'le traitement reprendra au prochain cycle.')

    def call(self, **params):
        self.check_time()
        now = self.clock()
        day = now.date().isoformat()
        if self.state.get('budget_day') != day:
            self.state.update(budget_day=day, requests_today=0, quota={})
        quota = self.state.get('quota', {})
        recent = [v for v in self.state.get('minute_calls', []) if v > now.timestamp()-60]
        if self.state.get('cooldown_until', 0) > now.timestamp():
            raise _Pause('API temporairement en pause ; nouvelle tentative au prochain cycle.')
        if quota.get('requests_remaining_day', 1) <= 0:
            raise _Pause('Le quota journalier communiqué par API-Football est épuisé ; reprise après remise à zéro UTC.')
        if (quota.get('requests_available_minute', 1) == 0 and
                now.timestamp() < self.state.get('quota_seen_at', 0)+max(60, quota.get('reset_seconds', 0))):
            raise _Pause('Quota API par minute épuisé ; reprise après temporisation.')
        self.state['minute_calls'] = recent + [now.timestamp()]
        self.state['requests_today'] = self.state.get('requests_today', 0)+1
        self.report['requests'] += 1
        self.save()
        # The reservation must reach durable storage before the provider call.
        # This also covers injected clients that bypass ApiFootballClient.get.
        cloud_storage.assert_lease()
        try:
            return workspace._response_fixtures(self.client.fixtures(**params))
        except api.ApiFootballError as error:
            if any(word in str(error).casefold() for word in ('quota', '429', 'connexion')):
                self.state['cooldown_until'] = now.timestamp()+max(900, workspace._quota(self.client).get('reset_seconds', 0))
            raise
        finally:
            self.state['quota'] = workspace._quota(self.client)
            self.state['quota_seen_at'] = now.timestamp()
            self.save()


def _history(root, config, gate, report, info, clock, *, after_batch=None):
    """Progressive, fair queue across ALL locally known league IDs and seasons."""
    imported = {(m['competition_code'], m['season']): m for m in api.load_season_imports(root / 'fixtures.json')}
    attempts = gate.state.setdefault('history_attempts', {})
    upcoming = {}
    for item in worker_io.iter_fixtures(root / 'fixtures.json'):
        try:
            fixture = workspace.fixture_from_raw(item)
            stamp = workspace._stamp(item)
            if api._status_short(item) == 'NS' and clock() < stamp <= clock()+timedelta(days=7):
                code = fixture['league']
                upcoming[code] = min(stamp, upcoming.get(code, stamp))
        except (ValueError, KeyError, TypeError):
            continue
    queue = []
    for code in sorted(info, key=int):
        for year in reversed(config['history_seasons']):
            marker = imported.get((code, year))
            # Completed old seasons are immutable inputs; current season refreshed daily.
            stale = (marker is not None and year >= clock().year-1 and
                     journal._stamp(marker['imported_at']) < clock()-timedelta(days=1))
            if marker is not None and not stale:
                continue
            key = f'{code}/{year}'
            if attempts.get(key, 0) > clock().timestamp()-21600:
                continue
            queue.append((marker is not None, attempts.get(key, 0), int(code), -year, code, year, key))
    report['history_pending'] = len(queue)
    if not config['history_enabled']:
        return
    pools = {False: [row for row in queue if not row[0]],
             True: [row for row in queue if row[0]]}
    prefer_refresh = gate.state.get('history_prefer_refresh', True)
    prefer_upcoming = gate.state.get('history_prefer_upcoming', True)
    while pools[False] or pools[True]:
        gate.check_time()
        kind = prefer_refresh if pools[prefer_refresh] else not prefer_refresh
        priority = [row for row in pools[kind] if row[-3] in upcoming]
        background = [row for row in pools[kind] if row[-3] not in upcoming]
        pool = (priority if prefer_upcoming else background) or background or priority
        if pool is priority:
            chosen = min(pool, key=lambda row: (upcoming[row[-3]], row[1], row[3], row[2]))
        else:
            chosen = min(pool)
        pools[kind].remove(chosen)
        report['history_pending'] = len(pools[False])+len(pools[True])
        prefer_refresh = not kind
        prefer_upcoming = chosen[-3] not in upcoming
        is_refresh, *_, code, year, key = chosen
        before = report['requests']
        try:
            raw = gate.call(league=code, season=year, timezone='UTC')
        except api.ApiFootballError:
            report['issues'].append(f'Historique {code}, saison {year} indisponible ; tentative différée de six heures.')
            # The finally below commits the failed attempt before yielding.
            raw = None
        finally:
            # A provider or host pause before the call is not an attempted import.
            if report['requests'] > before:
                attempts[key] = clock().timestamp()
                gate.state['history_prefer_refresh'] = not is_refresh
                gate.state['history_prefer_upcoming'] = code not in upcoming
                gate.save()
        if raw is None:
            if after_batch is not None:
                after_batch()
            continue
        if any(api._season_identity(item) != (code, year) for item in raw):
            raise AutomationError('Une réponse historique ne correspond pas à la compétition et saison demandées.')
        api.save_fixtures(raw, root / 'fixtures.json', season_imports=[dict(
            competition_code=code, season=year, match_ids=[str(r['fixture']['id']) for r in raw])])
        report['history_imports'] += 1
        report['history_received'] += len(raw)
        report.setdefault('history_jobs', []).append(dict(league=code, season=year, received=len(raw),
            priority='upcoming' if code in upcoming else 'background'))
        report['history_pending'] = len(pools[False])+len(pools[True])
        if after_batch is not None:
            after_batch()


def _refresh_results(root, gate, clock, report, *, after_batch=None, max_batches=None):
    """Refresh due IDs, optionally yielding after a scheduler-sized quantum.

    Checks are durable per ID, so subsequent passages resume the unattempted
    work. ``due``/``remaining`` describe the latest passage; received, missing
    and batch counters accumulate within the caller's report.
    """
    if max_batches is not None and (type(max_batches) is not int or max_batches < 1):
        raise AutomationError('Le nombre de lots de résultats doit être un entier positif.')
    due = {}
    checks = gate.state.setdefault('result_checks', {})
    missing_checks = gate.state.setdefault('result_missing', {})
    import candidate_tracking
    import high_probability_archive
    sources = [
        ('journal', lambda: journal.list_entries(root / 'predictions.sqlite3')),
        ('candidates', lambda: journal.list_entries(candidate_tracking.database_path(root))),
        ('probabilities', lambda: [dict(status=entry['status'], fixtures=[entry['fixture']]) for entry in
            high_probability_archive.latest(high_probability_archive.list_entries(root=root))])]
    if not cloud_storage.configured():
        import daily_learning_tracking
        import argumented_selection_store
        sources.append(('daily_learning',lambda:[dict(status=entry['status'],fixtures=[entry['fixture']])
            for entry in daily_learning_tracking.entries(root=root)]))
        sources.append(('argumented_selection',lambda:[dict(status=entry['status'],fixtures=[entry['fixture']])
            for entry in argumented_selection_store.entries(root=root)]))
        import causal_confirmation_worker
        sources.append(('causal_confirmation',lambda:causal_confirmation_worker.refresh_entries(root=root)))
    entries = []
    availability = report.setdefault('result_refresh_sources', {})
    for name, load in sources:
        try:
            entries.extend(load())
            availability[name] = 'available'
        except _Pause:
            raise
        except (ValueError, OSError, sqlite3.Error, KeyError, TypeError):
            availability[name] = 'unavailable'
            report['status'] = 'error'
            report.setdefault('issues', []).append(
                f'Source {name} indisponible pour la récupération des résultats ; les autres sources restent traitées.')
    # An administrative group outcome does not close its other fixtures.
    # Only a confirmed administrative fixture can be omitted individually.
    void_ids = {row['match_id'] for entry in entries if entry['status'] == 'void' for row in entry['fixtures']}
    void_raw = {}
    if void_ids:
        try:
            void_raw = {str(item['fixture']['id']): item for item in api.load_fixtures(
                root / 'fixtures.json', ids=sorted(void_ids, key=int))}
        except _Pause:
            raise
        except (ValueError, OSError, sqlite3.Error, KeyError, TypeError):
            report.setdefault('issues', []).append(
                'États individuels des groupes annulés indisponibles ; leurs rencontres restent à vérifier.')
    for entry in entries:
        for row in entry['fixtures']:
            kickoff = journal._stamp(row['timestamp'])
            if kickoff > clock():
                continue
            finished = entry['status'] == 'settled'
            if entry['status'] == 'void':
                item = void_raw.get(row['match_id'])
                if item is not None:
                    from fixture_import_policy import quality_error
                    if not quality_error(item) and api._status_short(item) in ('CANC', 'ABD', 'AWD', 'WO'):
                        continue
                    if api._status_short(item) in api.FINISHED_STATUSES:
                        try:
                            api.normalize_fixture(item)
                            finished = True
                        except (ValueError, KeyError, TypeError):
                            pass
            # Finished forecasts receive seven days of correction checks.
            if finished and kickoff < clock()-timedelta(days=7):
                continue
            interval = 21600 if finished else 900
            if checks.get(row['match_id'], 0) <= clock().timestamp()-interval:
                priority = (int(finished), checks.get(row['match_id'], 0))
                # A shared ID still needed by an unevaluated forecast must not
                # inherit the lower priority of another, already settled one.
                due[row['match_id']] = min(due.get(row['match_id'], priority), priority)
    ordered = sorted(due, key=lambda mid: (due[mid], int(mid)))
    refresh = report.setdefault('result_refresh', dict(received=0, missing=0, batches=0))
    refresh.update(due=len(ordered), remaining=len(ordered),
                   status='pending' if ordered else 'partial' if refresh['missing'] else 'completed')
    for batch, index in enumerate(range(0, len(ordered), 20)):
        if max_batches is not None and batch >= max_batches:
            break
        ids = ordered[index:index+20]
        raw = gate.call(ids='-'.join(ids), timezone='UTC')
        # A partial response is not a result for an omitted fixture. Verify
        # every returned identity before persisting any subset of this lot.
        try:
            if not isinstance(raw, list):
                raise ValueError()
            received = [api._numeric_id(item['fixture']['id'], 'du match') for item in raw]
            if len(received) != len(set(received)) or set(received)-set(ids):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise AutomationError('Réponse des résultats invalide : identifiant étranger, répété ou absent ; aucun résultat de ce lot modifié.') from None
        if raw:
            api.save_fixtures(raw, root / 'fixtures.json')
        attempted_at = clock().timestamp()
        absent = set(ids)-set(received)
        for mid in ids:
            if mid in absent:
                previous = missing_checks.get(mid, {})
                missing_checks[mid] = dict(first_attempt_at=previous.get('first_attempt_at', attempted_at),
                    last_attempt_at=attempted_at, attempts=previous.get('attempts', 0)+1,
                    reason='not_returned_by_api')
            else:
                missing_checks.pop(mid, None)
        # Persist attempts even for absent IDs, so they cannot repeatedly
        # monopolize the first lot. They remain eligible for a later retry.
        checks.update({mid: attempted_at for mid in ids})
        gate.save()
        refresh['received'] += len(received)
        refresh['missing'] += len(absent)
        refresh['batches'] += 1
        refresh['remaining'] = len(ordered)-index-len(ids)
        refresh['status'] = ('pending' if refresh['remaining'] else
                             'partial' if refresh['missing'] else 'completed')
        if absent:
            report.setdefault('issues', []).append(
                f'{len(absent)} match(s) absent(s) de la réponse des résultats : aucune issue inventée, '
                'nouvelle tentative ultérieure ; les autres lots restent traités.')
        if after_batch is not None:
            after_batch()


def _candidate_observation(root,report,when=None):
    if cloud_storage.configured():
        return
    try:
        import candidate_followup_report as observations
        # Production observations always use their actual UTC capture time.
        deterministic=when if Path(root).resolve()!=Path(api.DATA_DIR).resolve() else None
        report['candidate_observation']=observations.capture(root,now=deterministic)
    except (ValueError,OSError,sqlite3.Error,KeyError,TypeError):
        report['candidate_observation']=dict(status='unavailable',
            reason='Le relevé daté du suivi est indisponible ; les prévisions et leurs règles restent conservées.')


def _forecast_checkpoint(gate):
    """Yield to result collection between indivisible prediction calculations."""
    gate.check_time()
    checkpoint = getattr(gate, 'results_checkpoint', None)
    if checkpoint is not None:
        checkpoint()
    if isinstance(gate,_Gate):
        _track_causal_confirmation(gate.root,gate,gate.report,gate.clock)


def _forecast_fetch(gate, **params):
    _forecast_checkpoint(gate)
    return gate.call(**params)


def _track_candidates(root, gate, report, clock):
    if not cloud_storage.configured():
        import candidate_tracking_io as candidate_tracking
        try:
            report['candidate_tracking'] = candidate_tracking.run_cycle(
                root, clock=clock, refresh=lambda **params: _forecast_fetch(gate, **params),
                check_time=lambda: _forecast_checkpoint(gate))
        except _Pause:
            raise
        except (ValueError, OSError, KeyError, TypeError):
            report['candidate_tracking'] = dict(status='error', reason='Le suivi du candidat nécessite une vérification ; les prévisions habituelles restent séparées.')
        _candidate_observation(root,report,clock())


def _track_daily_learning(root,gate,report,clock):
    if cloud_storage.configured():return
    try:
        import daily_learning_worker
        report['daily_learning_tracking']=daily_learning_worker.capture_cycle(
            root,clock=clock,refresh=lambda **params:_forecast_fetch(gate,**params),
            check_time=lambda:_forecast_checkpoint(gate))
    except _Pause:
        raise
    except (ValueError,OSError,sqlite3.Error,KeyError,TypeError):
        report['daily_learning_tracking']=dict(status='unavailable',created=0,
            reason='Comparaison individuelle indisponible ; aucune prévision reconstruite.')


def _track_causal_confirmation(root,gate,report,clock):
    if cloud_storage.configured():return
    guarded=isinstance(gate,_Gate)
    if guarded:
        if getattr(gate,'causal_confirmation_active',False) is True:return
        if time.monotonic()<getattr(gate,'causal_confirmation_due',0):return
        gate.causal_confirmation_active=True
        gate.causal_confirmation_due=time.monotonic()+30
    try:
        import causal_confirmation_worker
        report['causal_confirmation']=causal_confirmation_worker.capture_cycle(
            root,clock=clock,refresh=lambda **params:_forecast_fetch(gate,**params),
            check_time=lambda:_forecast_checkpoint(gate))
    except _Pause:
        raise
    except (ValueError,OSError,sqlite3.Error,KeyError,TypeError):
        report['causal_confirmation']=dict(status='unavailable',created=0,
            reason='Confirmation du candidat causal indisponible ; aucune prévision antidatée.')
    finally:
        if guarded:gate.causal_confirmation_active=False


def _track_argumented_selection(root,gate,report,clock):
    if cloud_storage.configured():return
    try:
        import argumented_selection_workspace
        if argumented_selection_workspace.is_enabled(root):
            report['argumented_selection']=argumented_selection_workspace.capture_cycle(
                root,clock=clock,refresh=gate.call,check_time=gate.check_time)
    except _Pause:
        raise
    except (ValueError,OSError,sqlite3.Error,KeyError,TypeError):
        report['argumented_selection']=dict(status='unavailable',created=0,
            reason='Sélection argumentée indisponible ; aucune prévision reconstruite.')


def _prepare_daily_learning(root,config,gate,report):
    """Advance a local fit between provider tasks, even in a long API cycle."""
    if cloud_storage.configured() or not config.get('models_enabled',True):return
    gate.check_time()
    try:
        import daily_learning_worker
        if daily_learning_worker.is_enabled(root):
            report['daily_learning_training']=daily_learning_worker.run_training(root,config=config)
    except (ValueError,OSError,RuntimeError,sqlite3.Error):
        report['daily_learning_training']=dict(status='error',prepared=0,
            reason='Préparation quotidienne interrompue ; modèle courant conservé.')
    gate.check_time()


def _forecast(root, config, gate, report, clock, today, now):
    """Process the finite forecast queue using the latest local history."""
    state = gate.state
    _same_day(today, clock)
    # Give the served daily queue its turn before secondary inference/training.
    # Its cooperative checkpoints still keep result collection ahead of writes.
    try:
        import daily_analysis_worker_io as daily_analysis_worker
        report['daily_analyses'] = daily_analysis_worker.run(root, clock, gate.call,
                                                            check_time=lambda: _forecast_checkpoint(gate))
    except _Pause:
        raise
    except (ValueError, OSError, ImportError, KeyError, TypeError):
        report['daily_analyses'] = dict(status='error',
            reason='La préparation quotidienne est indisponible ; les autres groupes restent suivis.')
    _archive_probabilities(root, clock, report)
    _track_causal_confirmation(root,gate,report,clock)
    _track_argumented_selection(root,gate,report,clock)
    if config['models_enabled']:
        _prepare_daily_learning(root,config,gate,report)
        _track_daily_learning(root,gate,report,clock)
    _track_candidates(root,gate,report,clock)
    # Preserve persisted plan IDs even after provider rescheduling; their
    # ineligibility and report ordering remain governed by the original code.
    raw, day_info, info, rows, issues = worker_io.forecast_inventory(
        root / 'fixtures.json', today, config['sizes'], state.get('plans', {}),
        clock(), timezone_name=TIMEZONE)
    report['coverage_competitions'] = len(day_info)
    report['data_issues'] = sum(issues.values())
    plans = state.setdefault('plans', {})
    threshold = (today-timedelta(days=21)).isoformat()
    state['plans'] = plans = {key: val for key, val in plans.items() if key[:10] >= threshold}
    groups, raw_ids = _plan(raw, [today.isoformat()], config['sizes'], plans)
    gate.save()
    entries = {e['id']: e for e in journal.list_entries(root / 'predictions.sqlite3')}
    # Prioritize earliest kickoff across leagues; unavailable histories consume no requests.
    groups.sort(key=lambda g: (min((workspace._stamp(raw_ids[mid]) for mid in g['ids'] if mid in raw_ids),
                                  default=clock()), int(g['league']), g['size']))
    import prospective_models as models
    for group in groups:
        _forecast_checkpoint(gate)
        _same_day(today, clock)
        fixtures = _fixtures(group, raw_ids, clock())
        job_key = (group['day'], group['league'], group['size'], tuple(group['ids']))
        job = gate.forecast_jobs.get(job_key)
        if job is None:
            job = {}
            gate.forecast_jobs[job_key] = job
            report['jobs'].append(job)
            report['groups_considered'] += 1
        job.clear()
        job.update(league=group['league'], competition=info[group['league']]['competition'],
                   day=group['day'], size=group['size'], match_ids=list(group['ids']),
                   state='ineligible', reason='')
        if fixtures is None:
            job['reason'] = 'Coup d’envoi passé, clubs non confirmés ou statut incompatible.'
            continue
        try:
            key = journal._group_key(journal._fixtures(fixtures))
        except journal.JournalError as error:
            job['reason'] = str(error)
            continue
        if key in entries:
            job.update(state='recorded', id=key, reason='Première prévision déjà verrouillée.')
            if key not in gate.created_ids and key not in gate.reported_existing_ids:
                report['already_recorded'] += 1
                gate.reported_existing_ids.add(key)
            continue
        try:
            frozen = models.build_methods(rows[group['league']], fixtures)
        except (models.SnapshotError, ValueError, KeyError, TypeError):
            job.update(state='insufficient', reason='Données du groupe incompatibles avec les méthodes disponibles.')
            continue
        if not frozen['methods']:
            job.update(state='insufficient', reason='Historique ou modèles insuffisants.',
                       unavailable=frozen['unavailable'])
            continue
        # Preflight is AFTER all calculations. No cached NS status can authorize a write.
        _forecast_checkpoint(gate)
        _same_day(today, clock)
        fresh = gate.call(ids='-'.join(r['match_id'] for r in fixtures), timezone='UTC')
        _same_day(today, clock)
        if {str(r['fixture']['id']) for r in fresh} != set(group['ids']):
            job.update(state='changed', reason='API-Football ne confirme pas toutes les affiches.')
            continue
        api.save_fixtures(fresh, root / 'fixtures.json')
        try:
            entry = journal.record(fixtures, frozen['methods'], now=now,
                path=root / 'predictions.sqlite3', raw_fixtures=fresh, metadata=dict(
                    selected_engine='automation', unavailable=frozen['unavailable'],
                    calendar_checked_at=clock().isoformat(), competition=job['competition'],
                    country=info[group['league']]['country'], automation_policy=VERSION,
                    automation_day=group['day'], automation_timezone=TIMEZONE))
        except journal.JournalError as error:
            job.update(state='changed', reason=str(error))
            continue
        entries[entry['id']] = entry
        report['created'] += int(entry['created'])
        if entry['created']:
            gate.created_ids.add(entry['id'])
        job.update(state='recorded', id=entry['id'], methods=list(entry['methods']),
                   unavailable=frozen['unavailable'], reason='Prévision verrouillée avant match.')


def _calendar(root, gate, clock, today, *, archive=True):
    _same_day(today, clock)
    raw = gate.call(date=today.isoformat(), timezone=TIMEZONE)
    if any(workspace._stamp(r).astimezone(ZoneInfo(TIMEZONE)).date() != today for r in raw):
        raise AutomationError('Un calendrier contient des matchs hors de la date demandée.')
    api.save_fixtures(raw, root / 'fixtures.json')
    workspace._mark_calendar_refresh(root / 'fixtures.json', today, TIMEZONE)
    _same_day(today, clock)
    return _archive_probabilities(root, clock) if archive else dict(status='deferred')


def _archive_probabilities(root, clock, report=None):
    """Keep single-match probabilities ahead of long group/history queues."""
    import high_probability_workspace
    try:
        result = high_probability_workspace.archive_scan(root=root, clock=clock)
    except (ValueError, OSError, sqlite3.Error):
        result = dict(status='unavailable')
        if report is not None:
            report['issues'].append('Archivage des probabilités individuelles indisponible pour ce passage.')
    if report is not None:
        report['probability_archive'] = result
    return result


def _reconcile(root, report, now):
    if not cloud_storage.configured():
        try:
            import causal_confirmation_worker
            timing=None if Path(root).resolve()==Path(api.DATA_DIR).resolve() else now
            report['causal_confirmation_results']=causal_confirmation_worker.reconcile(root,now=timing)
        except (ValueError,OSError,sqlite3.Error,KeyError,TypeError):
            report['causal_confirmation_results']=dict(status='unavailable',
                reason='Résultats de confirmation indisponibles ; aucun échec déduit.')
        try:
            import argumented_selection_store
            report['argumented_selection_results']=argumented_selection_store.reconcile(root,now=now)
        except (ValueError,OSError,sqlite3.Error,KeyError,TypeError):
            report['argumented_selection_results']=dict(status='unavailable',
                reason='Résultats de la sélection argumentée indisponibles ; aucun échec déduit.')
        try:
            import daily_learning_worker
            report['daily_learning_results']=daily_learning_worker.reconcile(root,now=now)
        except (ValueError,OSError,sqlite3.Error,KeyError,TypeError):
            report['daily_learning_results']=dict(status='unavailable',
                reason='Résultats des candidats indisponibles ; aucun échec déduit.')
    try:
        settled = journal.reconcile(now=now, path=root / 'predictions.sqlite3',
                                    fixtures_path=root / 'fixtures.json')
        report['journal_results'] = dict(settled, status='available')
        report.update(settled=settled['settled'], pending=settled['pending'], unresolved=settled['unresolved'])
    except _Pause:
        raise
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError):
        report['journal_results'] = dict(status='unavailable')
        for name in ('settled', 'pending', 'unresolved'):
            report.pop(name, None)
        report['status'] = 'error'
        report.setdefault('issues', []).append(
            'Rapprochement du journal principal indisponible ; les autres historiques restent traités.')
    import high_probability_archive
    try:
        report['probability_archive_results'] = high_probability_archive.reconcile(root=root, now=now)
    except _Pause:
        raise
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError):
        report['probability_archive_results'] = dict(status='unavailable')
        report['status'] = 'error'
        report.setdefault('issues', []).append('Rapprochement des probabilités individuelles indisponible.')
    if not cloud_storage.configured():
        import candidate_tracking_io as candidate_tracking
        try:
            report['candidate_results'] = candidate_tracking.reconcile(root, now=now)
        except _Pause:
            raise
        except (ValueError, OSError, sqlite3.Error, KeyError, TypeError):
            report['candidate_results'] = dict(status='unavailable')
            report['status'] = 'error'
            report.setdefault('issues', []).append('Rapprochement du suivi des candidats indisponible.')
        _candidate_observation(root,report,now)


class _DayRefresh:
    """Cooperative fresh-day passes between durable historical season imports.

    No queue is truncated. Resume markers remain the existing imports/attempts;
    no timer or pending in-memory queue is required to survive a restart.
    Civil UTC and monotonic deadlines are independent: either can make a pass
    due. This catches a suspension without relying on platform sleep semantics,
    while a backward civil-clock correction cannot postpone monotonic work.
    Work still starts only at a cooperative checkpoint, never in a timer thread.
    """
    def __init__(self, root, config, gate, report, clock, today, now, pulse):
        self.root, self.config, self.gate, self.report = root, config, gate, report
        self.clock, self.today, self.now, self.pulse = clock, today, now, pulse
        self.defer_calendar()
        self.defer_results()
        self.servicing_results = False
        self.last_imports = report['history_imports']

    def defer_calendar(self):
        self.due = time.monotonic()+DAY_REFRESH_SECONDS
        self.due_wall = self.clock()+timedelta(seconds=DAY_REFRESH_SECONDS)

    def defer_results(self):
        self.results_due = time.monotonic()+DAY_REFRESH_SECONDS
        self.results_due_wall = self.clock()+timedelta(seconds=DAY_REFRESH_SECONDS)

    def _calendar_due(self):
        return time.monotonic() >= self.due or self.clock() >= self.due_wall

    def _results_due(self):
        return time.monotonic() >= self.results_due or self.clock() >= self.results_due_wall

    def mark(self, field):
        if self.gate.state.get('day_service',{}).get('day') != self.today.isoformat():
            self.gate.state['day_service'] = {}
        latest = self.gate.state['day_service']
        latest.update(day=self.today.isoformat(), **{field:self.clock().isoformat()})
        self.report['day_service'] = dict(latest)
        self.gate.save()
        phase = {'calendar_at':'calendar','results_at':'results','forecasts_at':'forecasts',
                 'reconciled_at':'reconcile'}[field]
        if not cloud_storage.configured() and not health.record_refresh(self.root, phase=phase, when=latest[field],
                cycle_started_at=self.report['started_at'], day=self.today.isoformat(), counters=self.report):
            self.report['monitoring_warning'] = health.MONITORING_WARNING

    def during_results(self):
        self.gate.check_time()
        _same_day(self.today, self.clock)
        self.mark('results_at')
        # Finish result batches before entering any expensive forecast queue.
        # A calendar refresh is cheap and does not start group inference.
        refreshed_calendar = self._calendar_due()
        if refreshed_calendar:
            self.pulse.progress('calendar')
            _calendar(self.root, self.gate, self.clock, self.today, archive=False)
            self.mark('calendar_at')
        _reconcile(self.root, self.report, self.clock())
        self.mark('reconciled_at')
        self.defer_results()
        if refreshed_calendar:
            self.defer_calendar()
        self.pulse.progress('results')

    def check_results(self):
        """Lightweight result work; never recurse into a forecast calculation."""
        if self.servicing_results or not self._results_due():
            return
        self.servicing_results = True
        try:
            # An ID backlog can contain thousands of fixtures. A single
            # calendar request catches up today's scores after sleep without
            # waiting for every older ID to receive its turn in that queue.
            _same_day(self.today, self.clock)
            refreshed_calendar = self._calendar_due()
            if refreshed_calendar:
                self.pulse.progress('calendar')
                _calendar(self.root, self.gate, self.clock, self.today, archive=False)
                self.mark('calendar_at')
            self.pulse.progress('results')
            _refresh_results(self.root, self.gate, self.clock, self.report, max_batches=1)
            self.mark('results_at')
            self.pulse.progress('reconcile')
            _reconcile(self.root, self.report, self.clock())
            self.mark('reconciled_at')
            self.report['result_priority_passes'] = self.report.get('result_priority_passes', 0)+1
            # A slow result/reconciliation pass must leave a full interval for
            # the interrupted forecast, rather than being due again on return.
            self.defer_results()
            if refreshed_calendar:
                self.defer_calendar()
            _write(self.root/'automation_status.json', dict(self.report, status='running',
                                                           last_updated_at=self.clock().isoformat()))
        finally:
            self.servicing_results = False
            self.pulse.progress('forecasts')

    def __call__(self, *, final=False, refresh_results=True):
        self.gate.check_time()
        _same_day(self.today, self.clock)
        if not self._calendar_due() and not (final and self.report['history_imports'] > self.last_imports):
            return
        self.gate.state['next_phase'] = 'results'
        self.gate.save()
        self.pulse.progress('calendar')
        _calendar(self.root, self.gate, self.clock, self.today, archive=False)
        self.mark('calendar_at')
        self.defer_calendar()
        if refresh_results:
            self.pulse.progress('results')
            self.gate.state['next_phase'] = 'forecasts'
            self.gate.save()
            _refresh_results(self.root, self.gate, self.clock, self.report,
                             after_batch=self.during_results, max_batches=1)
            self.mark('results_at')
        self.pulse.progress('reconcile')
        _reconcile(self.root, self.report, self.now)
        self.mark('reconciled_at')
        if refresh_results:
            self.defer_results()
        self.pulse.progress('forecasts')
        _same_day(self.today, self.clock)
        # Leave history the next turn if the host interrupts this finite pass.
        self.gate.state['next_phase'] = 'history'
        self.gate.save()
        _forecast(self.root, self.config, self.gate, self.report, self.clock, self.today, self.now)
        self.mark('forecasts_at')
        self.gate.state['next_phase'] = 'results'
        self.gate.save()
        self.report['day_refreshes'] = self.report.get('day_refreshes',0)+1
        self.report['last_daily_refresh_at'] = self.clock().isoformat()
        _write(self.root/'automation_status.json', dict(self.report,status='running',
                                                       last_updated_at=self.clock().isoformat()))
        self.last_imports = self.report['history_imports']
        self.defer_calendar()
        self.pulse.progress('history')


@cloud_storage.persisted
def run_cycle(*, config=None, root=None, client=None, now=None, dry_run=False, max_duration_seconds=None,
              origin='direct', execution_io=None):
    if max_duration_seconds is not None and (
            type(max_duration_seconds) not in (int, float) or
            not math.isfinite(max_duration_seconds) or max_duration_seconds <= 0):
        raise AutomationError('La durée disponible doit être un nombre positif et fini.')
    deadline = None if max_duration_seconds is None else time.monotonic()+max_duration_seconds
    root = _root(root)
    config = _config(config if config is not None else load_config(root))
    clock = (lambda: _time(now)) if now is not None else _time
    report = dict(version=VERSION, source='API-Football', status='completed', started_at=clock().isoformat(),
                  refresh_policy=DAY_REFRESH_POLICY,
                  requests=0, created=0, already_recorded=0, history_imports=0, history_received=0,
                  coverage_competitions=0, groups_considered=0, settled=0, pending=0, issues=[], jobs=[])
    report['execution_io'] = dict(execution_io or {'version': EXECUTION_IO_VERSION})
    if dry_run:
        report.update(status='dry_run', coverage=coverage(root=root, config=config, now=clock()))
        report['coverage_competitions'] = len(report['coverage'])
        return report
    if not config['enabled']:
        report['status'] = 'disabled'
        return report
    if origin not in health.ORIGINS:
        raise AutomationError('Origine du cycle invalide.')
    with health.monitor(root, report, interval_minutes=config['interval_minutes'], clock=clock,
                        origin=origin, enabled=not cloud_storage.configured()) as pulse:
        try:
            return _execute_cycle(root, config, client, clock, report, deadline, now, pulse)
        except Exception:
            # A late write/checkpoint/unlock failure must not leave the JSON
            # claiming success while the execution ledger records an error.
            report.update(status='error',finished_at=clock().isoformat())
            report['issues'].append('Une erreur de fin de passage a été détectée ; la fin normale du cycle n’est pas confirmée.')
            try:
                _write(root/'automation_status.json',report)
            except Exception:
                report['monitoring_warning'] = health.MONITORING_WARNING
            raise


def _execute_cycle(root, config, client, clock, report, deadline, now, pulse):
    """Original pipeline, independent of the local diagnostics ledger."""
    try:
        with _lock(root):
            state = _read(root / 'automation_state.json', {})
            if state.get('version', VERSION) != VERSION:
                raise AutomationError('Version de la politique automatique incompatible.')
            state['version'] = VERSION
            gate = _Gate(root, state, config, client, clock, report, deadline=deadline)
            try:
                if gate.client is None:
                    gate.client = api.ApiFootballClient(api.load_key())
                today = _local_day(clock())
                pulse.progress('calendar')
                _calendar(root, gate, clock, today, archive=False)
                day_refresh = _DayRefresh(root, config, gate, report, clock, today, now, pulse)
                day_refresh.mark('calendar_at')
                # Results cannot wait behind a multi-hour group preparation.
                # An immediately resumed, host-limited passage keeps its fair
                # phase turn after having just attempted this priority pass.
                state.setdefault('next_phase', 'results')
                try:
                    attempted = journal._stamp(state['result_priority_attempted_at'])
                except (KeyError, ValueError, TypeError):
                    attempted = None
                age = (clock()-attempted).total_seconds() if attempted else None
                if age is None or age < 0 or age >= DAY_REFRESH_SECONDS:
                    state['result_priority_attempted_at'] = clock().isoformat()
                    if state['next_phase'] == 'results':
                        state['next_phase'] = 'forecasts'
                    gate.save()
                    pulse.progress('results')
                    _refresh_results(root, gate, clock, report,
                                     after_batch=day_refresh.during_results, max_batches=1)
                    day_refresh.mark('results_at')
                    _reconcile(root, report, clock())
                    day_refresh.mark('reconciled_at')
                    day_refresh.defer_results()
                gate.results_checkpoint = day_refresh.check_results
                # The pinned candidate has a short declared capture window.
                # Give it an inference turn before unrelated training/group jobs.
                _track_causal_confirmation(root,gate,report,clock)
                # A shadow comparison can itself take several minutes. Give
                # the served daily queue one calculation first; its complete
                # queue still resumes in the normal forecast phase below.
                pulse.progress('forecasts')
                try:
                    import daily_analysis_worker_io as daily_analysis_worker
                    report['daily_analysis_priority'] = daily_analysis_worker.run(
                        root, clock, gate.call, max_jobs=1,
                        check_time=lambda: _forecast_checkpoint(gate))
                except _Pause:
                    raise
                except (ValueError, OSError, ImportError, KeyError, TypeError):
                    report['daily_analysis_priority'] = dict(status='error',
                        reason='Premier tour quotidien indisponible ; reprise dans la phase des prévisions.')
                # The forecast phase already captures the shadow study after
                # its full daily queue. Only a resumed background phase needs
                # this early capture; otherwise it would delay that queue.
                if state.get('next_phase') != 'forecasts':
                    pulse.progress('forecasts')
                    _track_candidates(root,gate,report,clock)
                phases = ('results', 'forecasts', 'history')
                next_phase = state.get('next_phase', 'results')
                if next_phase not in phases:
                    raise AutomationError('La phase de reprise automatique est invalide.')
                start = phases.index(next_phase)
                # Each finite queue hands the next turn off BEFORE its work.
                # A long results queue therefore cannot consume every passage;
                # a host/provider pause resumes with the following phase.
                for phase in phases[start:]+phases[:start]:
                    pulse.progress(phase)
                    gate.check_time()
                    _same_day(today, clock)
                    state['next_phase'] = phases[(phases.index(phase)+1) % len(phases)]
                    gate.save()
                    if phase == 'results':
                        _refresh_results(root, gate, clock, report,
                                         after_batch=day_refresh.during_results, max_batches=1)
                        day_refresh.mark('results_at')
                        day_refresh.defer_results()
                    elif phase == 'forecasts':
                        _forecast(root, config, gate, report, clock, today, now)
                        day_refresh.mark('forecasts_at')
                    else:
                        # Enumerate all leagues without keeping all raw rows
                        # alive throughout the long historical import queue.
                        info, _, _ = worker_io.inventory(root / 'fixtures.json', clock(), keep_leagues=())
                        _history(root, config, gate, report, info, clock, after_batch=day_refresh)
                        day_refresh(final=True)
                state['next_phase'] = 'results'
                gate.save()
            except _Pause as error:
                report.update(status='paused')
                report['issues'].append(str(error))
            except (api.ApiFootballError, AutomationError, journal.JournalError) as error:
                report.update(status='error')
                # Provider exceptions are designed for display; redact again
                # before persisting in case the HTTP response echoed a token.
                message = str(error)
                try:
                    secret = api.load_key()
                    if secret:
                        message = message.replace(secret, '[clé masquée]')
                except api.ApiFootballError:
                    message = 'Connexion API-Football ou configuration indisponible.'
                report['issues'].append(message[:600])
            except (ValueError, KeyError, TypeError, OSError):
                report.update(status='error')
                report['issues'].append('Cycle interrompu : API, données ou stockage indisponibles. Reprise au prochain passage.')
            finally:
                # Local reconciliation also runs after a provider or host pause.
                try:
                    pulse.progress('reconcile')
                    _reconcile(root, report, now)
                    if 'day_refresh' in locals():
                        day_refresh.mark('reconciled_at')
                    report.update(quota=state.get('quota', {}),
                                  requests_today=state.get('requests_today', 0))
                except (api.ApiFootballError, journal.JournalError, ValueError, OSError):
                    report['status'] = 'error'
                    report['issues'].append('La réconciliation locale nécessite une vérification des données.')
                report['finished_at'] = clock().isoformat()
                gate.save()
                _write(root / 'automation_status.json', report)
    except _Pause:
        report.update(status='busy', issues=['Un cycle est déjà en cours.'])
    return report
