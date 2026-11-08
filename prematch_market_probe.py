"""Isolated read-only probe of a genuinely new prematch information source.

Only API-Football's fixture/odds reference endpoints are reachable. Responses
are timestamped in a new research directory; production stores remain read-only.
This does not place bets, train models, or treat implied prices as true chances.
"""
from datetime import datetime,timedelta,timezone
from hashlib import sha256
from math import isfinite
from pathlib import Path
from urllib.error import HTTPError,URLError
from urllib.parse import urlencode
from urllib.request import Request,HTTPSHandler,build_opener
import argparse
import json
import sqlite3
import ssl

from halftime_statistics_client import load_key,QUOTA_HEADERS,_NoRedirect

ROOT=Path(__file__).resolve().parent
BASE='https://v3.football.api-sports.io/'
ALLOWED={'odds/bets':frozenset(),'odds':frozenset(('fixture','bet','page')),'fixtures':frozenset(('id',))}
LEAGUES=('39','40','61','78','135','140','71')


def numeric_id(value):
    return not isinstance(value,bool) and str(value).isascii() and str(value).isdigit() and int(value)>0


def stamp(value):
    d=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if d.tzinfo is None:raise ValueError('Explicit provider timezone required.')
    return d.astimezone(timezone.utc)


def payload_ok(envelope):
    p=envelope.get('payload')
    return (envelope.get('transport')=='received' and envelope.get('http_status')==200
            and isinstance(p,dict) and not p.get('errors') and isinstance(p.get('response'),list))


class ProbeClient:
    def __init__(self,*,root=ROOT,key=None,opener=None):
        self.root=Path(root);self._key=key;self._opener=opener

    def get(self,endpoint,**params):
        if endpoint not in ALLOWED or set(params)-ALLOWED[endpoint]:raise ValueError('Probe endpoint/parameters forbidden.')
        if any(not numeric_id(v) for v in params.values()):raise ValueError('Positive numeric identifiers only.')
        key=self._key or load_key(self.root)
        if not isinstance(key,str) or not key.strip():raise ValueError('Existing credential unavailable.')
        key=key.strip()
        if self._opener is None:
            try:
                import certifi
                context=ssl.create_default_context(cafile=certifi.where())
            except ImportError:context=ssl.create_default_context()
            self._opener=build_opener(HTTPSHandler(context=context),_NoRedirect())
        result=dict(endpoint=endpoint,parameters=params,request_started_utc=datetime.now(timezone.utc).isoformat(),headers={},transport='network_error')
        req=Request(BASE+endpoint+('?' +urlencode(params) if params else ''),headers={'x-apisports-key':key,'Accept':'application/json'})
        try:
            with self._opener.open(req,timeout=25) as response:
                result['http_status']=response.status
                result['headers']={k.lower():v for k,v in response.headers.items() if k.lower() in QUOTA_HEADERS}
                raw=response.read(32*1024*1024+1)
                if len(raw)>32*1024*1024:result['transport']='oversized_response'
                else:
                    def reject_constant(_):raise ValueError('NonfiniteJSON')
                    p=json.loads(raw,parse_constant=reject_constant)
                    result.update(transport='received' if isinstance(p,dict) else 'invalid_envelope',payload=p if isinstance(p,dict) else None)
        except HTTPError as error:
            result.update(transport='http_error',http_status=error.code)
            result['headers']={k.lower():v for k,v in (error.headers.items() if error.headers else []) if k.lower() in QUOTA_HEADERS}
        except (URLError,OSError,TimeoutError):result['transport']='network_error'
        except (ValueError,UnicodeError):result['transport']='invalid_json'
        result['observed_at_utc']=datetime.now(timezone.utc).isoformat()
        return json.loads(json.dumps(result,ensure_ascii=False,allow_nan=False).replace(key,'[REDACTED]'))


def find_market(payload):
    if not isinstance(payload,dict) or payload.get('errors') or not isinstance(payload.get('response'),list):raise ValueError('Invalid catalog.')
    found=[m for m in payload['response'] if isinstance(m,dict) and str(m.get('name','')).strip().casefold()=='highest scoring half']
    if len(found)!=1 or not numeric_id(found[0].get('id')):raise ValueError('Exact unique aggregate market not verified.')
    return dict(id=int(found[0]['id']),name=found[0]['name'])


def parse_quotes(payload,market,expected_fixture,observed_at,*,expected_kickoff=None):
    if not numeric_id(expected_fixture) or not numeric_id(market.get('id')):raise ValueError('Invalid expected provider identity.')
    observed=stamp(observed_at);valid=[];diagnostics=[];matches=payload.get('response',[]) if isinstance(payload,dict) else []
    labels={'1sthalf':'1st','firsthalf':'1st','2ndhalf':'2nd','secondhalf':'2nd','draw':'Égal','equal':'Égal','tie':'Égal'}
    if not isinstance(payload,dict) or payload.get('errors') or not isinstance(matches,list):
        return dict(quotes=[],diagnostics=['invalid_payload'])
    for row in matches:
        try:
            if not numeric_id(row['fixture']['id']) or str(row['fixture']['id'])!=str(expected_fixture):raise ValueError('wrong_fixture')
            kickoff=stamp(row['fixture']['date']);updated=stamp(row['update'])
            if expected_kickoff is not None and kickoff!=stamp(expected_kickoff):raise ValueError('kickoff_mismatch')
            if observed>=kickoff:raise ValueError('captured_after_kickoff')
            if updated>observed:raise ValueError('provider_update_in_future')
            if updated>=kickoff:raise ValueError('provider_update_after_kickoff')
        except (KeyError,TypeError,ValueError) as error:
            diagnostics.append(str(error) if isinstance(error,ValueError) else 'missing_identity_or_timestamp');continue
        books=row.get('bookmakers')
        if not isinstance(books,list):diagnostics.append('malformed_bookmakers');continue
        for book in books:
            if not isinstance(book,dict) or not isinstance(book.get('bets'),list):diagnostics.append('malformed_bookmaker');continue
            for bet in book['bets']:
                if not isinstance(bet,dict):diagnostics.append('malformed_bet');continue
                if not numeric_id(bet.get('id')):diagnostics.append('invalid_market_id');continue
                if str(bet.get('id'))!=str(market['id']):continue
                if str(bet.get('name','')).strip().casefold()!=market['name'].strip().casefold():
                    diagnostics.append('market_name_mismatch');continue
                odds={}
                try:
                    for value in bet['values']:
                        normalized=''.join(str(value['value']).casefold().split())
                        label=labels.get(normalized)
                        if label is None:raise ValueError('unknown_outcome_label')
                        if label in odds:raise ValueError('duplicate_outcome')
                        odd=float(value['odd'])
                        if not isfinite(odd) or odd<=1:raise ValueError('invalid_decimal_odds')
                        odds[label]=odd
                    if set(odds)!={'1st','Égal','2nd'}:raise ValueError('incomplete_three_way_market')
                    if not numeric_id(book['id']):raise ValueError('invalid_bookmaker_id')
                except (KeyError,TypeError,ValueError) as error:
                    diagnostics.append(str(error) if isinstance(error,ValueError) else 'malformed_values');continue
                inverse=[1/odds[k] for k in ('1st','Égal','2nd')];total=sum(inverse)
                valid.append(dict(fixture_id=str(expected_fixture),kickoff_utc=kickoff.isoformat(),bookmaker_id=book['id'],bookmaker=book.get('name'),
                    market_id=market['id'],market=market['name'],provider_updated_utc=updated.isoformat(),observed_at_utc=observed.isoformat(),quote_age_hours=(observed-updated).total_seconds()/3600,
                    decimal_odds=odds,overround=total-1,normalized_implied_probabilities=[x/total for x in inverse],
                    settlement_rules_verified=False,meaning='Price-derived reference, not a true probability or performance claim.'))
    return dict(quotes=valid,diagnostics=diagnostics)


def upcoming(now):
    path=ROOT/'data/api_football/fixtures.sqlite3'
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    try:
        sql='SELECT match_id,league,season,kickoff_utc,raw_json FROM fixtures WHERE league IN ('+','.join('?' for _ in LEAGUES)+") AND kickoff_utc>? AND kickoff_utc<? AND status='NS' ORDER BY kickoff_utc,match_id"
        rows=db.execute(sql,(*LEAGUES,now.replace(tzinfo=None).isoformat(),(now+timedelta(days=3)).replace(tzinfo=None).isoformat())).fetchall()
    finally:db.close()
    selected=[];seen=set()
    for row in rows:
        if row['league'] in seen:continue
        raw=json.loads(row['raw_json']);teams=raw.get('teams',{})
        selected.append(dict(match_id=row['match_id'],league=row['league'],season=row['season'],kickoff_utc=row['kickoff_utc'],
            home=teams.get('home',{}).get('name'),away=teams.get('away',{}).get('name')))
        seen.add(row['league'])
        if len(selected)==4:break
    return selected,len(rows)


def run(directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True);now=datetime.now(timezone.utc)
    if (directory/'plan.json').exists():plan=json.loads((directory/'plan.json').read_text())
    else:
        fixtures,count=upcoming(now)
        plan=dict(created_at_utc=now.isoformat(),client_date='2026-10-27',purpose='Read-only availability audit of exact three-way highest-scoring-half market, not an accuracy test.',
            fixtures=fixtures,local_upcoming_in_scope=count,sampling='First upcoming fixture per distinct pilot league, at most4leagues within72h; existing local NS calendar; no inferred world coverage.',
            api='API-Football only: catalog once, freshfixture and filteredodds once each. Provider allowance respected; no automatic retries.',
            no_production_changes=True,no_bets=True,code_sha256=sha256(Path(__file__).read_bytes()).hexdigest())
        with (directory/'plan.json').open('x') as f:json.dump(plan,f,ensure_ascii=False,indent=2)
    if sha256(Path(__file__).read_bytes()).hexdigest()!=plan['code_sha256']:raise ValueError('Frozen probe changed.')
    client=ProbeClient();calls=0;paused=False
    def fetch(name,endpoint,**params):
        nonlocal calls,paused
        path=directory/(name+'.json')
        if path.exists():return json.loads(path.read_text())
        if paused:return dict(transport='not_requested_provider_pause')
        value=client.get(endpoint,**params);calls+=1
        with path.open('x') as f:json.dump(value,f,ensure_ascii=False,indent=2)
        headers=value.get('headers',{})
        errors=(value.get('payload') or {}).get('errors')
        if value.get('http_status') in (401,403,429) or errors or headers.get('x-ratelimit-requests-remaining')=='0' or headers.get('x-ratelimit-remaining')=='0':paused=True
        return value
    catalog=fetch('catalog','odds/bets');report=dict(created_at_utc=datetime.now(timezone.utc).isoformat(),status='catalog_unavailable',new_requests=0,fixtures=[],no_performance_evaluated=True)
    if payload_ok(catalog):
        try:market=find_market(catalog['payload'])
        except ValueError:market=None
        paging=catalog['payload'].get('paging',{})
        complete=isinstance(paging,dict) and paging.get('total')==1
        report.update(status=('exact_market_not_found' if complete else 'catalog_completeness_unverified') if market is None else 'availability_probed',market=market,
            catalog_paging=paging,catalog_complete=complete,catalog_markets=len(catalog['payload']['response']))
        if market:
            for fixture in plan['fixtures']:
                fresh=fetch('fixture_'+fixture['match_id'],'fixtures',id=fixture['match_id'])
                record=dict(fixture=fixture,current_fixture_received=payload_ok(fresh),quotes=[],diagnostics=[])
                if payload_ok(fresh):
                    rows=fresh['payload']['response']
                    valid=(len(rows)==1 and str(rows[0].get('fixture',{}).get('id'))==fixture['match_id'] and rows[0].get('fixture',{}).get('status',{}).get('short')=='NS')
                    if valid:
                        try:
                            kickoff=stamp(rows[0]['fixture']['date'])
                            valid=kickoff>datetime.now(timezone.utc) and str(rows[0].get('league',{}).get('id'))==fixture['league']
                        except (KeyError,ValueError,TypeError):valid=False
                    if valid:
                        response=fetch('odds_'+fixture['match_id'],'odds',fixture=fixture['match_id'],bet=market['id'],page=1)
                        record['odds_received']=payload_ok(response)
                        if payload_ok(response):
                            record.update(parse_quotes(response['payload'],market,fixture['match_id'],response['observed_at_utc'],expected_kickoff=kickoff.isoformat()))
                            record['paging']=response['payload'].get('paging')
                    else:record['diagnostics'].append('fixture_not_verified_as_upcoming_NS')
                report['fixtures'].append(record)
    report.update(new_requests=calls,provider_pause=paused,quote_count=sum(len(r['quotes']) for r in report['fixtures']),
        fixtures_with_quotes=sum(bool(r['quotes']) for r in report['fixtures']),settlement_rules_verified=False)
    with (directory/'summary.json').open('w') as f:json.dump(report,f,ensure_ascii=False,indent=2)
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--directory',type=Path,required=True);a=p.parse_args()
    r=run(a.directory)
    print(json.dumps({k:r[k] for k in ('status','market','new_requests','quote_count','fixtures_with_quotes','provider_pause') if k in r},ensure_ascii=False))
