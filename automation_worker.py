"""Bounded API-only daily pipeline, independent from a Streamlit session.

All known competitions participate. Calendar collection and forecasts concern
today's Africa/Kinshasa fixtures only. Group anchors persist across updates;
no group is chosen on results, no forecast is backfilled, no model is fitted here.
"""
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

import api_football as api
import api_match_workspace as workspace
import prediction_journal as journal
import competition_readiness as readiness


VERSION = 'all-competitions-daily-blocks-v1'
DEFAULT_CONFIG = dict(enabled=True, sizes=[2, 4, 6, 8], interval_minutes=15,
                      daily_request_budget=500, cycle_request_budget=40,
                      history_seasons=[2022, 2023, 2024, 2025, 2026],
                      history_requests_per_cycle=2, models_enabled=True, model_jobs_per_cycle=1)
TIMEZONE = workspace.DEFAULT_TIMEZONE


class AutomationError(ValueError):
    pass


class _Pause(AutomationError):
    pass


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
    if not isinstance(value, dict) or set(value) - set(DEFAULT_CONFIG):
        raise AutomationError('Réglages d’automatisation invalides.')
    result = dict(DEFAULT_CONFIG, **value)
    if type(result['enabled']) is not bool or type(result['models_enabled']) is not bool:
        raise AutomationError('L’activation doit être un booléen.')
    for field, low, high in (('interval_minutes', 1, 1440), ('daily_request_budget', 1, 100000),
                             ('cycle_request_budget', 1, 1000), ('history_requests_per_cycle', 0, 2),
                             ('model_jobs_per_cycle', 0, 1)):
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
    """Reserve requests durably BEFORE network; bounded rolling minute, UTC day."""
    def __init__(self, root, state, config, client, clock, report):
        self.root, self.state, self.config, self.client, self.clock, self.report = root, state, config, client, clock, report

    def save(self):
        _write(self.root / 'automation_state.json', self.state)

    def call(self, **params):
        now = self.clock()
        day = now.date().isoformat()
        if self.state.get('budget_day') != day:
            self.state.update(budget_day=day, requests_today=0, quota={})
        quota = self.state.get('quota', {})
        recent = [v for v in self.state.get('minute_calls', []) if v > now.timestamp()-60]
        if (self.report['requests'] >= self.config['cycle_request_budget'] or
                self.state.get('requests_today', 0) >= self.config['daily_request_budget']):
            raise _Pause('Budget de requêtes atteint ; reprise au cycle suivant ou au prochain jour UTC.')
        if len(recent) >= 10:
            raise _Pause('Temporisation : dix requêtes au maximum sur une minute ; reprise au prochain cycle.')
        if self.state.get('cooldown_until', 0) > now.timestamp():
            raise _Pause('API temporairement en pause ; nouvelle tentative au prochain cycle.')
        if quota.get('requests_remaining_day', 100000) <= 5:
            raise _Pause('Réserve du quota API journalier atteinte ; reprise après remise à zéro UTC.')
        if (quota.get('requests_available_minute', 1) == 0 and
                now.timestamp() < self.state.get('quota_seen_at', 0)+max(60, quota.get('reset_seconds', 0))):
            raise _Pause('Quota API par minute épuisé ; reprise après temporisation.')
        self.state['minute_calls'] = recent + [now.timestamp()]
        self.state['requests_today'] = self.state.get('requests_today', 0)+1
        self.report['requests'] += 1
        self.save()
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


def _history(root, config, gate, report, info, clock):
    """Progressive, fair queue across ALL locally known league IDs and seasons."""
    imported = {(m['competition_code'], m['season']): m for m in api.load_season_imports(root / 'fixtures.json')}
    attempts = gate.state.setdefault('history_attempts', {})
    upcoming = {}
    for item in api.load_fixtures(root / 'fixtures.json'):
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
    pools = {False: [row for row in queue if not row[0]],
             True: [row for row in queue if row[0]]}
    selected = []
    prefer_refresh = gate.state.get('history_prefer_refresh', True)
    prefer_upcoming = gate.state.get('history_prefer_upcoming', True)
    for _ in range(config['history_requests_per_cycle']):
        kind = prefer_refresh if pools[prefer_refresh] else not prefer_refresh
        if not pools[kind]:
            break
        priority = [row for row in pools[kind] if row[-3] in upcoming]
        background = [row for row in pools[kind] if row[-3] not in upcoming]
        pool = (priority if prefer_upcoming else background) or background or priority
        if pool is priority:
            chosen = min(pool, key=lambda row: (upcoming[row[-3]], row[1], row[3], row[2]))
        else:
            chosen = min(pool)
        pools[kind].remove(chosen)
        selected.append(chosen)
        prefer_refresh = not kind
        prefer_upcoming = chosen[-3] not in upcoming
    for is_refresh, *_, code, year, key in selected:
        before = report['requests']
        try:
            raw = gate.call(league=code, season=year, timezone='UTC')
        except api.ApiFootballError:
            report['issues'].append(f'Historique {code}, saison {year} indisponible ; tentative différée de six heures.')
            continue
        finally:
            # A budget pause is not an attempted import and must not defer it.
            if report['requests'] > before:
                attempts[key] = clock().timestamp()
                gate.state['history_prefer_refresh'] = not is_refresh
                gate.state['history_prefer_upcoming'] = code not in upcoming
                gate.save()
        if any(api._season_identity(item) != (code, year) for item in raw):
            raise AutomationError('Une réponse historique ne correspond pas à la compétition et saison demandées.')
        api.save_fixtures(raw, root / 'fixtures.json', season_imports=[dict(
            competition_code=code, season=year, match_ids=[str(r['fixture']['id']) for r in raw])])
        report['history_imports'] += 1
        report['history_received'] += len(raw)
        report.setdefault('history_jobs', []).append(dict(league=code, season=year, received=len(raw),
            priority='upcoming' if code in upcoming else 'background'))


def _refresh_results(root, gate, clock, report):
    due = {}
    checks = gate.state.setdefault('result_checks', {})
    for entry in journal.list_entries(root / 'predictions.sqlite3'):
        if entry['status'] == 'void':
            continue
        for row in entry['fixtures']:
            kickoff = journal._stamp(row['timestamp'])
            if kickoff > clock():
                continue
            # Finished forecasts receive seven days of correction checks.
            if entry['status'] == 'settled' and kickoff < clock()-timedelta(days=7):
                continue
            interval = 21600 if entry['status'] == 'settled' else 900
            if checks.get(row['match_id'], 0) <= clock().timestamp()-interval:
                due[row['match_id']] = checks.get(row['match_id'], 0)
    ordered = sorted(due, key=lambda mid: (due[mid], int(mid)))
    for index in range(0, min(len(ordered), 40), 20):
        ids = ordered[index:index+20]
        raw = gate.call(ids='-'.join(ids), timezone='UTC')
        if {str(r['fixture']['id']) for r in raw} != set(ids):
            raise AutomationError('Réponse des résultats incomplète : aucun résultat de ce lot modifié.')
        api.save_fixtures(raw, root / 'fixtures.json')
        checks.update({mid: clock().timestamp() for mid in ids})
        gate.save()


def run_cycle(*, config=None, root=None, client=None, now=None, dry_run=False):
    root = _root(root)
    config = _config(config if config is not None else load_config(root))
    clock = (lambda: _time(now)) if now is not None else _time
    report = dict(version=VERSION, source='API-Football', status='completed', started_at=clock().isoformat(),
                  requests=0, created=0, already_recorded=0, history_imports=0, history_received=0,
                  coverage_competitions=0, groups_considered=0, settled=0, pending=0, issues=[], jobs=[])
    if dry_run:
        report.update(status='dry_run', coverage=coverage(root=root, config=config, now=clock()))
        report['coverage_competitions'] = len(report['coverage'])
        return report
    if not config['enabled']:
        report['status'] = 'disabled'
        return report
    try:
        with _lock(root):
            state = _read(root / 'automation_state.json', {})
            if state.get('version', VERSION) != VERSION:
                raise AutomationError('Version de la politique automatique incompatible.')
            state['version'] = VERSION
            gate = _Gate(root, state, config, client, clock, report)
            try:
                if gate.client is None:
                    gate.client = api.ApiFootballClient(api.load_key())
                today = _local_day(clock())
                _same_day(today, clock)
                raw = gate.call(date=today.isoformat(), timezone=TIMEZONE)
                if any(workspace._stamp(r).astimezone(ZoneInfo(TIMEZONE)).date() != today for r in raw):
                    raise AutomationError('Un calendrier contient des matchs hors de la date demandée.')
                api.save_fixtures(raw, root / 'fixtures.json')
                workspace._mark_calendar_refresh(root / 'fixtures.json', today, TIMEZONE)
                _same_day(today, clock)
                raw = api.load_fixtures(root / 'fixtures.json')
                info, rows, issues = _inventory(raw, clock())
                # Results and progressive imports get bounded slots before the
                # potentially much larger forecast queue. No league can starve them.
                _refresh_results(root, gate, clock, report)
                _history(root, config, gate, report, info, clock)
                _same_day(today, clock)
                try:
                    import daily_analysis_worker
                    report['daily_analyses'] = daily_analysis_worker.run(root, clock, gate.call)
                except _Pause:
                    raise
                except (ValueError, OSError, ImportError, KeyError, TypeError):
                    report['daily_analyses'] = dict(status='error',
                        reason='La préparation quotidienne est indisponible ; les autres groupes restent suivis.')
                raw = api.load_fixtures(root / 'fixtures.json')
                info, rows, issues = _inventory(raw, clock())
                day_info, _, _ = _inventory(workspace._day_rows(raw, today, TIMEZONE), clock())
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
                    _same_day(today, clock)
                    fixtures = _fixtures(group, raw_ids, clock())
                    job = dict(league=group['league'], competition=info[group['league']]['competition'],
                               day=group['day'], size=group['size'], state='ineligible', reason='')
                    report['jobs'].append(job)
                    report['groups_considered'] += 1
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
                        report['already_recorded'] += 1
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
                    job.update(state='recorded', id=entry['id'], methods=list(entry['methods']),
                               unavailable=frozen['unavailable'], reason='Prévision verrouillée avant match.')
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
                # Reconciliation is local and still runs if network or request budget stopped the cycle.
                try:
                    settled = journal.reconcile(api.load_fixtures(root / 'fixtures.json'), now=now,
                                                path=root / 'predictions.sqlite3')
                    report.update(settled=settled['settled'], pending=settled['pending'],
                                  unresolved=settled['unresolved'], quota=state.get('quota', {}),
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
