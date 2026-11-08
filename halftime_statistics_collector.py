"""Resumable local collection, without forecasts, training or Cloud imports."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from halftime_statistics_quality import parse_statistics
from halftime_statistics_store import StoreError, timestamp, encoded

DEFAULT_LEAGUES = ('39','40','61','78','135','140','71')
DEFAULT_SEASONS = (2024,2025,2026)


def clock(now=None):
    value = now or datetime.now(timezone.utc)
    timestamp(value)  # Reject naive clocks.
    return value.astimezone(timezone.utc)


def fixture(raw, *, before):
    """Validate regulation-time completed match metadata, without app imports."""
    try:
        event, teams, league = raw['fixture'],raw['teams'],raw['league']
        if raw.get('provider') not in (None,'api-football','API-Football') or event['status']['short'] != 'FT':
            return None
        stamp = datetime.fromisoformat(event['date'].replace('Z','+00:00'))
        if stamp.tzinfo is None or stamp >= before:
            return None
        ids = [event['id'],league['id'],teams['home']['id'],teams['away']['id']]
        if any(type(v) is not int or v<=0 for v in ids) or ids[2] == ids[3]:
            return None
        season = league['season']
        if type(season) is not int or season < 2024:
            return None
        for side in ('home','away'):
            first, full = raw['score']['halftime'][side],raw['score']['fulltime'][side]
            if type(first) is not int or type(full) is not int or not 0<=first<=full:
                return None
        return dict(match_id=str(ids[0]),league=str(ids[1]),season=season,
                    kickoff_utc=stamp.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds'),
                    home_id=str(ids[2]),away_id=str(ids[3]))
    except (KeyError,TypeError,ValueError,AttributeError):
        return None


def plan_local(store, source_path, leagues=DEFAULT_LEAGUES, seasons=DEFAULT_SEASONS, before=None):
    before = clock(before) if before else clock()-timedelta(hours=6)
    source_path=Path(source_path).resolve()
    connection=sqlite3.connect(source_path.as_uri()+'?mode=ro',uri=True)
    connection.execute('PRAGMA query_only=ON')
    connection.execute('BEGIN')
    selected=[]; ignored=0
    league_set=None if leagues is None else set(map(str,leagues))
    try:
        for raw, in connection.execute("SELECT raw_json FROM fixtures WHERE status='FT' AND normalized_json IS NOT NULL AND season>=2024"):
            row=fixture(json.loads(raw),before=before)
            if row and row['season'] in seasons and (league_set is None or row['league'] in league_set):
                selected.append(row)
            else:
                ignored+=1
    finally:
        connection.rollback(); connection.close()
    with store.lock():
        counts=store.enqueue(selected)
    return dict(**counts,eligible=len(selected),ignored=ignored,before_utc=before.isoformat())


def _provider_pause(store, result, now):
    """Persist only current response quotas; stale audit headers never enter here."""
    headers=result.get('headers',{})
    def number(key):
        value=headers.get(key)
        return int(value) if isinstance(value,(str,int)) and not isinstance(value,bool) and str(value).isdigit() else None
    until=0
    if number('x-ratelimit-requests-remaining') == 0:
        until=(now+timedelta(days=1)).replace(hour=0,minute=0,second=5,microsecond=0).timestamp()
    if number('x-ratelimit-remaining') == 0:
        until=max(until,timestamp(now)+max(60,number('retry-after') or 0))
    if result.get('http_status') == 429:
        until=max(until,timestamp(now)+max(60,number('retry-after') or 900))
    if result.get('http_status') in (401,403):
        until=max(until,timestamp(now)+3600)
    errors=result.get('payload',{}).get('errors')
    if errors:
        # Provider errors may arrive with HTTP200; do not call them empty data.
        kind=parse_statistics(result['payload'],('1','2'))['provider_error_kind']
        if kind=='quota_or_rate':
            until=max(until,timestamp(now)+900)
        elif kind in ('authentication','access'):
            until=max(until,timestamp(now)+3600)
    with store.transaction():
        store.set_meta('last_provider_headers',headers)
        if until:
            store.set_meta('provider_pause_until',until)
    return until


def _get(store,client,endpoint,now,**params):
    pause=store.get_meta('provider_pause_until',0)
    if pause>timestamp(now):
        return None
    # Persist a request reservation before network. Network never holds SQLite locks.
    with store.transaction():
        store.set_meta('last_request',dict(endpoint=endpoint,parameters=params,started_at=timestamp(now)))
    result=client.get(endpoint,**params)
    _provider_pause(store,result,now)
    return result


def _classify(envelope, task, now):
    if (envelope.get('endpoint') != 'fixtures/statistics'
        or str(envelope.get('parameters',{}).get('fixture')) != task['match_id']
        or str(envelope.get('parameters',{}).get('half')).lower() != 'true'):
        raise StoreError('Réponse de statistiques incompatible avec le match demandé.')
    if envelope.get('transport') != 'received' or envelope.get('http_status') not in (200,204):
        parsed=dict(status='transport_error',periods={})
    else:
        parsed=parse_statistics(envelope.get('payload'),(task['home_id'],task['away_id']))
    status=parsed['status']
    if parsed.get('halves_core_shots_available'):
        return parsed,'ready','ready',0
    if status=='ok':
        # Partial data is retained and is never filled with fictitious zeroes.
        return parsed,'partial','partial',0
    if status=='empty':
        kickoff=datetime.fromisoformat(task['kickoff_utc']).replace(tzinfo=timezone.utc)
        if now-kickoff<timedelta(days=7):
            return parsed,'retry','empty',timestamp(now+timedelta(hours=24))
        return parsed,'empty','empty',0
    if status in ('invalid','unknown_shape'):
        return parsed,'invalid',status,0
    delay=min(3600,60*(2**min(task['attempts'],6)))
    return parsed,'retry',status,timestamp(now)+delay


def _save_response(store,task,envelope,*,source,now,attempt=None):
    if store.has_response(envelope):
        return 'already_imported'
    parsed,state,availability,next_at=_classify(envelope,task,now)
    attempt=attempt or store.reserve(task['match_id'],source=source,now=now)
    store.finish(attempt,task,envelope,parsed,state=state,availability=availability,next_at=next_at,now=now)
    return availability


def seed_audit(store,directory):
    """Use verified cached data idempotently, without spending an API request."""
    directory=Path(directory)
    manifests=[directory/name for name in ('sample_manifest.json','supplement_manifest.json')]
    samples={str(r['match_id']):r for p in manifests if p.exists() for r in json.loads(p.read_text())['samples']}
    counts=Counter()
    with store.lock():
        for path in sorted(directory.glob('fixtures_statistics-*.json')):
            envelope=json.loads(path.read_text())
            mid=str(envelope.get('parameters',{}).get('fixture'))
            sample=samples.get(mid)
            if sample is None:
                counts['outside_manifest']+=1; continue
            task=dict(match_id=mid,league=str(sample['league_id']),season=sample['season'],
                kickoff_utc=sample['kickoff_utc'],home_id=str(sample['expected_team_ids'][0]),
                away_id=str(sample['expected_team_ids'][1]),attempts=0)
            store.enqueue([task],priority=50)
            now=clock(datetime.fromisoformat(envelope['observed_at_utc']))
            counts[_save_response(store,task,envelope,source='audit',now=now)]+=1
    return dict(counts)


def discover(store,client,leagues=DEFAULT_LEAGUES,seasons=DEFAULT_SEASONS,now=None):
    """Discover whole seasons, preserving raw API lists and metadata in sidecar."""
    report=Counter(); issues=[]
    with store.lock():
        for season in seasons:
            current=clock(now)
            key='coverage:'+str(season)
            coverage=store.get_meta(key)
            if not coverage:
                response=_get(store,client,'leagues',current,season=season)
                if response is None:
                    issues.append('provider_pause'); break
                report['requests']+=1
                if (response.get('transport')!='received' or response.get('http_status')!=200
                    or response.get('payload',{}).get('errors')
                    or not isinstance(response.get('payload',{}).get('response'),list)
                    or response.get('payload',{}).get('paging',{}).get('total',1)!=1):
                    issues.append('coverage_unavailable'); break
                coverage=response
                with store.transaction():
                    store.set_meta(key,coverage)
            available={}
            for row in coverage.get('payload',{}).get('response',[]):
                for s in row.get('seasons',[]):
                    if s.get('year')==season:
                        available[str(row['league']['id'])]=s.get('coverage',{}).get('fixtures',{}).get('statistics_fixtures')
            targets=sorted(available,key=int) if leagues is None else list(map(str,leagues))
            for league in targets:
                if available.get(league) is not True:
                    report['not_declared_covered']+=1; continue
                existing=store.db.execute('SELECT observed_at,payload_json FROM discoveries WHERE league=? AND season=?',(league,season)).fetchone()
                # Season labels can cross calendar years; refresh manifests daily.
                if existing and timestamp(current)-existing['observed_at']<86400:
                    envelope=json.loads(existing['payload_json']); report['cached_seasons']+=1
                else:
                    envelope=_get(store,client,'fixtures',current,league=int(league),season=season,status='FT',timezone='UTC')
                    if envelope is None:
                        issues.append('provider_pause'); break
                    report['requests']+=1
                    payload=envelope.get('payload',{})
                    if envelope.get('transport')!='received' or envelope.get('http_status')!=200 or payload.get('errors') or not isinstance(payload.get('response'),list) or payload.get('paging',{}).get('total',1)!=1:
                        issues.append('fixtures_unavailable'); break
                    with store.transaction():
                        store.db.execute('INSERT OR REPLACE INTO discoveries VALUES(?,?,?,?)',
                            (league,season,timestamp(current),encoded(envelope)))
                before=current-timedelta(hours=6)
                rows=[fixture(raw,before=before) for raw in envelope['payload']['response']]
                rows=[r for r in rows if r and r['league']==league and r['season']==season]
                counts=store.enqueue(rows)
                report.update(counts); report['eligible']+=len(rows); report['seasons']+=1
            if issues:
                break
        if not issues:
            with store.transaction():
                store.set_meta('last_discovery_cycle',timestamp(clock(now)))
    return dict(status='paused' if issues else 'completed',**report,issues=issues)


def collect_once(store,client,batch_size=25,now=None):
    """Finite scheduling slice; restarting continues all remaining queue entries."""
    if type(batch_size) is not int or batch_size<1:
        raise ValueError('La taille du lot doit être un entier positif.')
    report=Counter(); status='completed'
    with store.lock():
        report['recovered_interruptions']=store.recover(clock(now))
        for _ in range(batch_size):
            current=clock(now)
            if store.get_meta('provider_pause_until',0)>timestamp(current):
                status='paused'; break
            task=store.due(current)
            if task is None:
                status='idle'; break
            attempt=store.reserve(task['match_id'],source='network',now=current)
            envelope=_get(store,client,'fixtures/statistics',current,fixture=int(task['match_id']),half='true')
            if envelope is None:
                status='paused'; break
            report['requests']+=1
            result=_save_response(store,task,envelope,source='network',now=clock(now),attempt=attempt)
            report[result]+=1
            if result in ('transport_error','api_error'):
                status='paused'; break
    return dict(status=status,**report,queue=store.status())
