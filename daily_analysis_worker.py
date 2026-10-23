"""Bounded, resumable daily forecasts with immutable selection anchors.

The planner selects from local API-Football data. Every new forecast needs a
fresh ID-scoped provider preflight after its calculations. Opening a page only
reads ``load_status``/``load_selections``; ``run_now`` is an explicit action.
"""

import cloud_storage
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

import api_football as api
import api_match_workspace as workspace
import prediction_journal as journal
import prospective_models as models


VERSION = 'daily-analysis-worker-v1'
ZONE = ZoneInfo('Africa/Kinshasa')
STATE_NAME = 'daily_analysis_state.json'
STATUS_NAME = 'daily_analysis_status.json'


class DailyAnalysisError(ValueError):
    pass


def _root(root=None):
    return Path(api.DATA_DIR if root is None else root)


def _read(path, default):
    if not path.exists():
        return default
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError):
        raise DailyAnalysisError(f'Le fichier {path.name} est invalide ; préparation suspendue.') from None


@cloud_storage.persisted
def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent,
                                         prefix='.'+path.name, delete=False) as handle:
            name = handle.name
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if name is not None and Path(name).exists():
            Path(name).unlink()


def load_status(root=None):
    return _read(_root(root)/STATUS_NAME, {})


def _state(root):
    value = _read(root/STATE_NAME, dict(version=VERSION, days={}))
    if value.get('version') != VERSION or not isinstance(value.get('days'), dict):
        raise DailyAnalysisError('La version des sélections quotidiennes est incompatible.')
    return value


def load_selections(root=None):
    """Saved links also cover reused mono-league forecasts without rewriting them."""
    output = []
    for day, jobs in sorted(_state(_root(root))['days'].items()):
        for candidate_id, job in sorted(jobs.items()):
            if job.get('entry_id'):
                output.append(dict(entry_id=job['entry_id'], day=day,
                    selection=job['selection'], competitions=job['competitions'], candidate_id=candidate_id))
    return output


@contextmanager
def _lock(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root/'daily_analysis.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DailyAnalysisError('Une préparation quotidienne est déjà en cours.') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _clock(clock):
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DailyAnalysisError('L’horloge de préparation doit préciser son fuseau.')
    return value.astimezone(timezone.utc)


def _current(day, fixtures, raw, current):
    """Validate frozen identities and the whole local day, never labels/scores."""
    if current.astimezone(ZONE).date().isoformat() != day:
        return False, 'La journée de Kinshasa a changé ; reprise sur les matchs du jour.'
    by_id = {str(item.get('fixture', {}).get('id')): item for item in raw}
    for fixture in fixtures:
        try:
            stamp = journal._stamp(fixture['timestamp'])
            if stamp <= current:
                return False, 'Un coup d’envoi est passé ; aucune prévision rétrospective n’est ajoutée.'
            if stamp.astimezone(ZONE).date().isoformat() != day:
                return False, 'Le groupe contient un match hors de cette journée.'
            item = by_id[fixture['match_id']]
            check = workspace.fixture_from_raw(item)
            if api._status_short(item) != 'NS' or any(str(check[key]) != str(fixture[key])
                    for key in ('match_id', 'league', 'home_id', 'away_id', 'timestamp')):
                return False, 'Une affiche, ses clubs, son horaire ou son statut ont changé.'
        except (KeyError, TypeError, ValueError, AttributeError):
            return False, 'Une affiche n’est plus confirmée dans le relevé API-Football.'
    return True, ''


def _candidate(value, day, planned_at):
    if not isinstance(value, dict) or not isinstance(value.get('id'), str) or not value['id']:
        raise DailyAnalysisError('Le plan quotidien contient un identifiant invalide.')
    fixtures = journal._fixtures(value.get('fixtures', []), allow_mixed=True)
    if any(journal._stamp(row['timestamp']).astimezone(ZONE).date().isoformat() != day for row in fixtures):
        raise DailyAnalysisError('Le plan quotidien contient une affiche hors journée.')
    selection, competitions = value.get('selection'), value.get('competitions')
    if not isinstance(selection, dict):
        raise DailyAnalysisError('Les critères de sélection doivent être conservés.')
    if isinstance(competitions, list):
        competitions = {str(row['league']): {key: row[key] for key in ('competition', 'country') if key in row}
                        for row in competitions}
    if not isinstance(competitions, dict) or set(competitions) != {r['league'] for r in fixtures}:
        raise DailyAnalysisError('Les compétitions du plan doivent correspondre aux affiches.')
    # Make the anchor independent of mutable planner dictionaries.
    return json.loads(json.dumps(dict(fixtures=fixtures, selection=selection, competitions=competitions,
        planned_at=planned_at, state='planned', reason='', entry_id=None), ensure_ascii=False, allow_nan=False))


def _report(root, state, day, report, clock):
    jobs = [dict(candidate_id=identity, day=day, **job) for identity, job in state['days'].get(day, {}).items()]
    report.update(jobs=jobs, candidates=len(jobs),
                  remaining=sum(job['state'] not in ('recorded', 'expired', 'changed') for job in jobs),
                  finished_at=_clock(clock).isoformat())
    _write(root/STATE_NAME, state)
    _write(root/STATUS_NAME, report)
    return report


@cloud_storage.persisted
def run(root, clock, fetch=None, *, max_jobs=4):
    """Prepare at most ``max_jobs`` candidates using an already-budgeted fetch.

    ``fetch(**params)`` returns raw fixture dictionaries, e.g. a worker Gate's
    ``call``. Without it no forecast is written. All anchors are committed
    before the first calculation, and completed/expired anchors never move.
    A budget exception is re-raised after saving progress for the next cycle.
    """
    if type(max_jobs) is not int or not 1 <= max_jobs <= 100:
        raise DailyAnalysisError('Le nombre de calculs par passage doit être compris entre 1 et 100.')
    if not callable(clock) or fetch is not None and not callable(fetch):
        raise DailyAnalysisError('Horloge ou actualisation quotidienne invalide.')
    root = _root(root)
    current = _clock(clock)
    day = current.astimezone(ZONE).date().isoformat()
    report = dict(version=VERSION, source='API-Football', day=day, status='completed',
                  started_at=current.isoformat(), created=0, already_recorded=0, attempted=0,
                  diagnostics=[], jobs=[])
    with _lock(root):
        state = _state(root)
        jobs = state['days'].setdefault(day, {})
        try:
            raw = api.load_fixtures(root/'fixtures.json')
            commits = state.setdefault('plan_commits', {})
            if day not in commits and not jobs:
                import daily_analysis_engine as engine
                planned = engine.plan(raw, now=current, existing=())
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
            _write(root/STATE_NAME, state)  # durable BEFORE build or provider preflight
            history = None
            # Round-robin by last attempt: an insufficient first group cannot
            # permanently consume every CPU slot ahead of other candidates.
            queue = sorted(jobs.items(), key=lambda pair: (pair[1].get('last_attempt_at', ''),
                            pair[1]['fixtures'][0]['timestamp'], pair[0]))
            for identity, job in queue:
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
                if report['attempted'] >= max_jobs:
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
                    history = api.load_matches(root/'fixtures.json')[0]
                try:
                    frozen = models.build_mixed_methods(history, fixtures)
                except (ValueError, KeyError, TypeError, OSError):
                    job.update(state='insufficient', reason='L’historique ou les moteurs sont indisponibles pour ce groupe.')
                    continue
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
                raw = api.load_fixtures(root/'fixtures.json')
                eligible, reason = _current(day, fixtures, fresh, _clock(clock))
                if not eligible:
                    job.update(state='changed', reason=reason)
                    continue
                try:
                    entry = journal.record_mixed(fixtures, frozen['methods'], path=root/'predictions.sqlite3',
                        raw_fixtures=fresh, clock=clock, metadata=dict(selected_engine='automation',
                            unavailable=frozen.get('unavailable', {}), daily_policy=VERSION,
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


@cloud_storage.persisted
def run_now(root=None, *, max_jobs=4):
    """Explicit UI action sharing the worker's lock and durable API budgets.

    Refresh only today's calendar and ID-scoped preflights. No training or
    multi-season import is invoked by this action.
    """
    import automation_worker as automation
    root = _root(root)
    config = automation.load_config(root)
    clock = automation._time
    report = dict(requests=0, issues=[])
    with automation._lock(root):
        state = automation._read(root/'automation_state.json', {})
        gate = automation._Gate(root, state, config, None, clock, report)
        try:
            gate.client = api.ApiFootballClient(api.load_key())
            day = _clock(clock).astimezone(ZONE).date()
            raw = gate.call(date=day.isoformat(), timezone=ZONE.key)
            if any(workspace._stamp(row).astimezone(ZONE).date() != day for row in raw):
                raise DailyAnalysisError('Le calendrier reçu contient un match hors journée.')
            if _clock(clock).astimezone(ZONE).date() != day:
                raise DailyAnalysisError('La journée a changé pendant l’actualisation ; relancez la préparation.')
            api.save_fixtures(raw, root/'fixtures.json')
            workspace._mark_calendar_refresh(root/'fixtures.json', day, ZONE.key)
            result = run(root, clock, gate.call, max_jobs=max_jobs)
            result['requests'] = report['requests']
            _write(root/STATUS_NAME, result)
            return result
        finally:
            # Updating result states is local, including when quota interrupts.
            journal.reconcile(api.load_fixtures(root/'fixtures.json'), path=root/'predictions.sqlite3')
            gate.save()
