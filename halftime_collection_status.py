"""Cheap read-only collection status; no API, credentials, Cloud or model imports."""
from datetime import datetime, timezone
import json
from math import isfinite
from pathlib import Path
import sqlite3

ROOT=Path(__file__).resolve().parent
LEAGUES=('39','40','61','78','135','140','71')
CUTOFF='2026-10-26T00:00:00'
KNOWN=('ready','partial','empty','invalid','unknown_shape')


def _read_json(path,limit=2*1024*1024):
    try:
        with path.open('rb') as handle:
            raw=handle.read(limit+1)
        return json.loads(raw) if len(raw)<=limit else None
    except (OSError,ValueError,UnicodeError):
        return None


def _iso(value):
    return datetime.fromtimestamp(value,timezone.utc).isoformat() if value is not None else None


def _comparison(root,total,remaining):
    result=dict(stage='not_prepared',prepared_examples=None,snapshot_created_utc=None,
                detail='Aucun instantané de comparaison préparé sur ce Mac.')
    directory=root/'data/research/halftime_enrichment'
    if not directory.exists():return result
    # Only tiny summaries are inspected, never datasets, models or raw fixtures.
    paths=sorted(directory.glob('*/readiness.json'),key=lambda p:p.stat().st_mtime,reverse=True)
    for path in paths:
        ready=_read_json(path,128*1024)
        if not isinstance(ready,dict):continue
        examples=ready.get('examples')
        if not isinstance(examples,dict):continue
        plan=_read_json(path.parent/'plan.json',128*1024)
        if not isinstance(plan,dict):plan={}
        result['prepared_examples']=sum(v for v in examples.values() if type(v) is int and v>=0)
        result['snapshot_created_utc']=plan.get('created_at_utc')
        completed=_read_json(path.parent/'evaluation/results.json')
        if (isinstance(completed,dict) and completed.get('status')=='exploratory_historical_evaluation'
            and isinstance(completed.get('metrics'),dict)):
            result.update(stage='results_available',detail='Résultats historiques disponibles. Aucun modèle de l’application n’est activé par ce suivi.')
        elif remaining:
            result.update(stage='waiting_collection',detail='Protocole préparé. La collecte du pilote reste à compléter avant la comparaison.')
        elif total and ready.get('ready') is True:
            result.update(stage='collection_ready',detail='Instantané complet. La comparaison peut être lancée ; elle n’a pas encore été exécutée.')
        elif total:
            result.update(stage='snapshot_waiting',detail='Collecte terminée. Préparer un nouvel instantané et vérifier ses conditions avant de lancer la comparaison.')
        return result
    return result


def load_status(root=None,now=None,*,compact=False):
    root=Path(root) if root is not None else ROOT
    now=now or datetime.now(timezone.utc)
    if not isinstance(now,datetime) or now.tzinfo is None:raise ValueError('Aware current time required.')
    current=now.timestamp();path=root/'data/api_football/halftime_statistics.sqlite3'
    result=dict(exists=path.exists(),status='not_started',total=0,resolved=0,remaining=0,
        ready=0,partial=0,empty=0,invalid=0,retry_errors=0,last_activity_utc=None,
        last_activity_age_seconds=None,rate_per_hour=None,eta_seconds=None,estimated_finish_utc=None,
        provider_pause_until=0,provider_remaining_day=None,eta_limited_by_quota=False,per_year=[],
        comparison=dict(stage='not_prepared',prepared_examples=None,snapshot_created_utc=None,
                        detail='Aucun instantané de comparaison préparé sur ce Mac.'))
    if not result['exists']:return result
    scope="t.league IN ("+','.join('?' for _ in LEAGUES)+") AND t.kickoff_utc>=? AND t.kickoff_utc<?"
    params=(*LEAGUES,'2024-01-01T00:00:00',CUTOFF)
    db=None
    try:
        db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.25)
        db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('PRAGMA busy_timeout=250');db.execute('BEGIN')
        counts={r['availability']:r['n'] for r in db.execute('SELECT availability,COUNT(*) n FROM tasks t WHERE '+scope+' GROUP BY availability',params)}
        total=sum(counts.values());resolved=sum(counts.get(k,0) for k in KNOWN)
        result.update(total=total,resolved=resolved,remaining=total-resolved,ready=counts.get('ready',0),
            partial=counts.get('partial',0),empty=counts.get('empty',0),
            invalid=counts.get('invalid',0)+counts.get('unknown_shape',0),
            retry_errors=sum(v for k,v in counts.items() if k not in (*KNOWN,'unobserved')))
        # The completed sidebar only displays these counters. Do not scan the
        # attempts table or research summaries every fifteen seconds on every
        # open page. The detail view keeps its full activity/comparison data.
        if compact and result['remaining']==0:
            result['status']='complete' if total else 'not_started'
            return result
        for row in db.execute('SELECT substr(t.kickoff_utc,1,4) year,t.availability,COUNT(*) n FROM tasks t WHERE '+scope+' GROUP BY year,t.availability ORDER BY year',params):
            if not result['per_year'] or result['per_year'][-1]['year']!=row['year']:
                result['per_year'].append(dict(year=row['year'],total=0,resolved=0,remaining=0,ready=0))
            block=result['per_year'][-1];block['total']+=row['n']
            block['resolved']+=row['n'] if row['availability'] in KNOWN else 0
            block['remaining']=block['total']-block['resolved']
            block['ready']+=row['n'] if row['availability']=='ready' else 0
        last=db.execute('''SELECT MAX(COALESCE(a.finished_at,a.started_at)) FROM attempts a
            JOIN tasks t ON t.match_id=a.match_id WHERE '''+scope+" AND a.source='network'",params).fetchone()[0]
        # Grouping by fixture prevents duplicate retries inflating useful throughput.
        samples=db.execute('''SELECT a.match_id,MAX(a.finished_at) at FROM attempts a
            JOIN tasks t ON t.match_id=a.match_id WHERE '''+scope+" AND a.source='network' AND a.finished_at IS NOT NULL AND a.classification IN ('ready','partial','empty','invalid','unknown_shape') GROUP BY a.match_id ORDER BY at DESC LIMIT 100",params).fetchall()
        meta={r['key']:r['value'] for r in db.execute("SELECT key,value FROM metadata WHERE key IN ('provider_pause_until','last_provider_headers')")}
        db.rollback();db.close();db=None
        if last is not None and isfinite(last):
            result['last_activity_utc']=_iso(last);result['last_activity_age_seconds']=max(0.,current-last)
        try:
            pause=json.loads(meta.get('provider_pause_until','0'))
            result['provider_pause_until']=pause if type(pause) in (int,float) and isfinite(pause) and pause>0 else 0
            headers=json.loads(meta.get('last_provider_headers','{}'))
            value=headers.get('x-ratelimit-requests-remaining')
            if not isinstance(value,bool) and isinstance(value,(str,int)) and str(value).isdigit():result['provider_remaining_day']=int(value)
        except (TypeError,ValueError,AttributeError):pass
        if total==0:result['status']='not_started'
        elif result['remaining']==0:result['status']='complete'
        elif result['provider_pause_until']>current:result['status']='paused'
        elif result['last_activity_age_seconds'] is not None and result['last_activity_age_seconds']<=180:result['status']='collecting'
        elif result['retry_errors'] and result['retry_errors']==result['remaining']:result['status']='needs_attention'
        else:result['status']='inactive'
        if len(samples)>=10:
            elapsed=samples[0]['at']-samples[-1]['at']
            if elapsed>=5:result['rate_per_hour']=(len(samples)-1)*3600/elapsed
        result['eta_limited_by_quota']=(result['provider_remaining_day'] is not None and result['provider_remaining_day']<result['remaining'])
        if result['status']=='collecting' and result['rate_per_hour'] and not result['eta_limited_by_quota']:
            result['eta_seconds']=result['remaining']*3600/result['rate_per_hour']
            result['estimated_finish_utc']=_iso(current+result['eta_seconds'])
        result['comparison']=_comparison(root,total,result['remaining'])
        return result
    except (OSError,sqlite3.Error,TypeError,ValueError,OverflowError):
        result.update(status='unavailable',rate_per_hour=None,eta_seconds=None,estimated_finish_utc=None)
        return result
    finally:
        if db is not None:db.close()
