"""Daily, local candidate preparation and future individual comparisons.

The provider worker owns fresh-status calls. This module never publishes a model
to the application and never fits while a Streamlit page is rendered.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import api_football as api
import automation_worker as automation
import cloud_storage
import fixture_store


VERSION = 'daily-learning-worker-v1'


def _root(root=None):
    return Path(api.DATA_DIR if root is None else root).resolve()


def _now(value=None):
    current=datetime.now(timezone.utc) if value is None else value
    if not isinstance(current,datetime) or current.tzinfo is None:
        raise ValueError('Une heure UTC avec fuseau est nécessaire.')
    return current.astimezone(timezone.utc)


def is_enabled(root=None):
    value=automation._read(_root(root)/'daily_learning/enabled.json',{})
    return value.get('enabled') is True


def enable(root=None, *, enabled=True):
    """Explicit activation, separate from importing or visiting a page."""
    if type(enabled) is not bool:raise ValueError('Activation booléenne requise.')
    if cloud_storage.configured():raise ValueError('Stockage local requis.')
    cloud_storage._assert_writable()
    automation._write(_root(root)/'daily_learning/enabled.json',dict(enabled=enabled))


@contextmanager
def _lock(root):
    root.mkdir(parents=True,exist_ok=True)
    with (root/'worker.lock').open('a') as handle:
        try:
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:yield True
        finally:fcntl.flock(handle,fcntl.LOCK_UN)


def _inventory(root, cutoff, now):
    """Read indexed league counts and priorities; no complete raw-file load."""
    path=fixture_store.active_path(root/'fixtures.json')
    if path is None:
        rows,_=api.load_matches(root/'fixtures.json',end=cutoff)
        counts={}
        for row in rows:
            counts[row['league']]=counts.get(row['league'],0)+1
        raw=api.load_fixtures(root/'fixtures.json')
        priority={}
        for item in raw:
            if api._status_short(item)!='NS':continue
            try:
                stamp=datetime.fromisoformat(item['fixture']['date'].replace('Z','+00:00'))
                league=str(item['league']['id'])
                if stamp>now:priority[league]=min(stamp.isoformat(),priority.get(league,stamp.isoformat()))
            except (KeyError,TypeError,ValueError):continue
        signatures={code:sha256(json.dumps([r for r in rows if r['league']==code],sort_keys=True).encode()).hexdigest()
                    for code in counts}
        return counts,priority,signatures
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as con:
        con.execute('PRAGMA query_only=ON');con.execute('BEGIN')
        counts={};hashes={}
        for code,normalized in con.execute('SELECT league,normalized_json FROM fixtures '
                'WHERE normalized_json IS NOT NULL AND kickoff_utc<? ORDER BY league,match_id',(cutoff,)):
            counts[code]=counts.get(code,0)+1
            hashes.setdefault(code,sha256()).update(normalized.encode()+b'\n')
        priority=dict(con.execute("SELECT league,MIN(kickoff_utc) FROM fixtures WHERE status='NS' "
            'AND kickoff_utc>? GROUP BY league',(now.replace(tzinfo=None).isoformat(),)))
    return counts,priority,{code:value.hexdigest() for code,value in hashes.items()}


def run_training(root=None, *, config=None, now=None, dry_run=False, league=None):
    """One resumable queue item per existing service turn, across all leagues.

    This is scheduling, not an input cap. Every valid competition remains in
    the queue. Source fingerprints invalidate checks after corrections or imports.
    Persistent age order alternates with upcoming kickoffs across UTC days.
    """
    import daily_learning_models as models
    root=_root(root);current=_now(now)
    if root==Path(api.DATA_DIR).resolve() and now is not None:
        raise ValueError('Le service réel utilise uniquement son horloge actuelle.')
    conf=automation._config(config if config is not None else automation.load_config(root))
    report=dict(version=VERSION,status='idle',started_at=current.isoformat(),prepared=0,
        candidates_unchanged=0,checked=0,issues=[],promoted=False)
    if cloud_storage.configured():return dict(report,status='disabled',reason='Stockage local requis.')
    if not conf['enabled'] or not conf['models_enabled'] or not conf['model_jobs_per_cycle']:
        return dict(report,status='disabled',reason='Préparation des modèles désactivée.')
    if not is_enabled(root):
        return dict(report,status='disabled',reason='Boucle quotidienne non activée.')
    directory=root/'daily_learning'
    if dry_run:
        counts,priorities,signatures=_inventory(root,current.date().isoformat(),current)
        return dict(report,status='dry_run',competitions=len(counts),
            sufficient_league_counts=sum(n>=30 for n in counts.values()),
            provider_requests=0,training_started=False)
    cloud_storage._assert_writable()
    with _lock(directory) as locked:
        if not locked:return dict(report,status='busy')
        timing={'now':now} if now is not None else {}
        models.prepare_protocol(root=root,**timing)
        state=automation._read(directory/'worker_state.json',dict(checked={},prefer_upcoming=True))
        today=current.date().isoformat()
        state['day_utc']=today
        state.setdefault('checked',{})
        state.setdefault('prefer_upcoming',True)
        counts,priorities,signatures=_inventory(root,today,current)
        target=str(league) if league is not None else None
        eligible=[code for code,n in counts.items() if n>=30 and (target is None or code==target)]
        def needs_check(code):
            old=state['checked'].get(code,{})
            return old.get('status') not in ('completed','unchanged','insufficient') or old.get('source')!=signatures[code]
        pending=[code for code in eligible if needs_check(code)]
        # Never reset fairness at midnight. Every other turn serves the oldest
        # attempt; urgent or failing leagues cannot starve the background queue.
        order=sorted(pending,key=lambda code:(
            (code not in priorities,priorities.get(code,''),state['checked'].get(code,{}).get('attempted_at',''),int(code))
            if state['prefer_upcoming'] else
            (state['checked'].get(code,{}).get('attempted_at',''),int(code))))
        for code in order:
            old=state['checked'].get(code,{})
            report['checked']+=1
            try:
                rows,audit=api.load_matches(root/'fixtures.json',leagues={code},end=today,
                    statuses=api.FINISHED_STATUSES)
                planned=models.plan(rows,code,root=root,**timing)
            except (ValueError,OSError,RuntimeError,sqlite3.Error):
                state['checked'][code]=dict(status='error',source=signatures[code],attempted_at=current.isoformat())
                report['issues'].append(f'Historique ou artefacts de la compétition {code} à vérifier ; les autres restent traités.')
                continue
            if not planned.get('ready'):
                state['checked'][code]=dict(status='insufficient',source=signatures[code],attempted_at=current.isoformat(),
                    reason=planned.get('reason','Historique insuffisant.'))
                continue
            if not planned.get('changed',True):
                state['checked'][code]=dict(status='unchanged',source=signatures[code],attempted_at=current.isoformat(),
                    candidate_id=planned.get('candidate_id'))
                report['candidates_unchanged']+=1
                continue
            report.update(status='building',league=code,history_matches=len(rows),
                excluded_scores=len(audit['issues']))
            automation._write(directory/'status.json',report)
            state['last_selected']=code
            state['checked'][code]=dict(status='building',source=signatures[code],attempted_at=current.isoformat())
            state['prefer_upcoming']=not state['prefer_upcoming']
            automation._write(directory/'worker_state.json',state)
            try:
                candidate=models.train(rows,code,root=root,**timing)
                report.update(status='completed',prepared=1,candidate_id=candidate['candidate_id'],
                    trained_through=candidate['trained_through'],cutoff_exclusive=candidate['cutoff_exclusive'])
                state['checked'][code].update(status='completed',candidate_id=candidate['candidate_id'])
            except (ValueError,OSError,RuntimeError) as error:
                report.update(status='error')
                report['issues'].append('Préparation du candidat interrompue ; reprise sur le même instantané, modèle courant conservé.')
                state['checked'][code].update(status='error')
            break
        report['competitions']=len(counts)
        report['remaining_today']=sum(needs_check(code) for code in eligible)
        report['finished_at']=_now(now).isoformat()
        automation._write(directory/'worker_state.json',state)
        automation._write(directory/'status.json',report)
        return report


def capture_cycle(root=None, *, clock=None, refresh=None, check_time=None):
    """Use the provider worker's quota-aware refresh, never another API client."""
    import daily_learning_models as models
    import daily_learning_tracking as tracking
    import high_probability_workspace as probabilities
    root=_root(root)
    if cloud_storage.configured():return dict(status='disabled',created=0)
    if not is_enabled(root):return dict(status='disabled',created=0)
    protocol=models.load_protocol(root=root)
    if protocol is None:return dict(status='not_started',created=0)
    current=_now(clock() if clock else None)
    report=probabilities.load_scan(root=root,now=current)
    candidates={}
    for code in sorted({row['fixture']['league'] for row in report['rows']},key=int):
        if check_time:check_time()
        try:candidate=models.latest(code,root=root)
        except (ValueError,OSError):continue
        if candidate is not None:candidates[code]=candidate
    if not candidates:return dict(status='awaiting_candidate',created=0)
    rows,_=api.load_matches(root/'fixtures.json',leagues=set(candidates),end=report['before'],
        statuses=api.FINISHED_STATUSES)
    return tracking.capture(report,candidates,rows,root=root,clock=clock,
        refresh=refresh,check_time=check_time)


def reconcile(root=None, *, now=None):
    import daily_learning_tracking as tracking
    return tracking.reconcile(root=_root(root),now=now)


def status(root=None):
    """Read-only UI/CLI status: no queue preparation or training."""
    return automation._read(_root(root)/'daily_learning/status.json',dict(status='not_started',prepared=0))
