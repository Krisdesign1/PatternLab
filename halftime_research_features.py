"""Pure prematch profiles for the isolated halftime-enrichment experiment.

No application, network, storage, training or credential imports. Entire UTC
days are emitted before any current-day response may update histories. Scores
enter history at most conservatively after kickoff+6h and next UTC-day boundary.
Retrospective statistics are explicitly distinguished from observed-time stats.
"""
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta, timezone
from itertools import groupby
from math import exp, fsum, isfinite

VERSION='halftime-rich-profiles-v1'
LABELS=('1st','Égal','2nd')
LEAGUES=('39','40','61','78','135','140','71')
PROFILES=('home_all','home_home','away_all','away_away')
GOALS=('ht_for','ht_against','sh_for','sh_against')
METRICS=(('total_shots','Total Shots'),('on_goal','Shots on Goal'),
         ('inside_box','Shots insidebox'),('xg','expected_goals'))
CORE_COLUMNS=('league_count','eligible','personalized','season','month','weekday','hour_utc')+tuple(
    'league_'+league for league in LEAGUES)+tuple('frequency_'+str(i) for i in range(3))+tuple('clubs_'+str(i) for i in range(3))+tuple(
    prefix+'_'+field for prefix in PROFILES for field in ('count','last10_count','days_rest')+tuple(
        field+'_'+stat for field in GOALS for stat in ('mean','recent3_delta')))
RICH_COLUMNS=tuple(prefix+'_'+period+'_'+metric+'_'+perspective+'_'+stat
    for prefix in PROFILES for period in ('first','second') for metric,_ in METRICS
    for perspective in ('for','against') for stat in ('mean','count','recent3_delta','recent3_count','missing'))


def _stamp(value):
    result=datetime.fromisoformat(value.replace('Z','+00:00'))
    return (result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result).astimezone(timezone.utc)


def _prepared(rows):
    selected={}
    for row in rows:
        if row.get('provider')!='api-football':
            raise ValueError('Only API-Football is permitted.')
        ids=[]
        for name in ('match_id','league','home_id','away_id'):
            value=row[name]
            if isinstance(value,bool) or not str(value).isascii() or not str(value).isdigit() or int(value)<=0:
                raise ValueError('Invalid provider identifier.')
            ids.append(str(int(value)))
        if ids[2]==ids[3]:raise ValueError('Identical clubs.')
        stamp=_stamp(row['timestamp'])
        pairs=[]
        for name in ('ht','ft'):
            parts=row[name].split('-')
            if len(parts)!=2 or any(not p.isascii() or not p.isdigit() for p in parts):
                raise ValueError('Invalid regulation score.')
            pairs.append(tuple(map(int,parts)))
        ht,ft=pairs
        if any(a>b for a,b in zip(ht,ft)):raise ValueError('HT exceeds FT.')
        first=sum(ht);second=sum(ft)-first
        actual='1st' if first>second else '2nd' if second>first else 'Égal'
        if row['value']!=actual:raise ValueError('Score/label contradiction.')
        season=row.get('season',stamp.year)
        if type(season) is not int:raise ValueError('Invalid season.')
        item=dict(match_id=ids[0],league=ids[1],home_id=ids[2],away_id=ids[3],stamp=stamp,
                  day=stamp.date().isoformat(),season=season,ht=ht,ft=ft,actual=actual)
        if ids[0] in selected and selected[ids[0]]!=item:raise ValueError('Conflicting fixture ID.')
        selected[ids[0]]=item
    return sorted(selected.values(),key=lambda r:(r['stamp'],int(r['match_id'])))


def _poisson(mean):
    if not 0<=mean<=100:raise ValueError('Unsupported goal mean.')
    terms=[exp(-mean)]
    while fsum(terms)<1-1e-12:
        terms.append(terms[-1]*mean/len(terms))
    total=fsum(terms)
    return [p/total for p in terms]


def _half_probabilities(first,second):
    result=[0.,0.,0.]
    for i,a in enumerate(_poisson(first)):
        for j,b in enumerate(_poisson(second)):
            result[0 if i>j else 2 if j>i else 1]+=a*b
    return result


def _rich_value(record,statistics,period,metric,perspective,*,mode,day_start,observed_before):
    entry=statistics.get(record['match_id'])
    if not entry:return None
    if str(entry.get('home_id'))!=record['home_id'] or str(entry.get('away_id'))!=record['away_id']:
        raise ValueError('Statistics team IDs do not match the score fixture.')
    observed=entry.get('observed_at')
    if not isinstance(observed,(int,float)) or isinstance(observed,bool) or not isfinite(observed):
        raise ValueError('Statistics observation time is required.')
    if observed_before is not None and observed>=observed_before:return None
    if mode=='stats_as_observed' and observed>=day_start.timestamp():return None
    pair=entry.get('periods',{}).get(period,{}).get(metric)
    if pair is None:return None
    if not isinstance(pair,(list,tuple)) or len(pair)!=2 or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not isfinite(v) or v<0 for v in pair):
        raise ValueError('Invalid statistical measurement.')
    side=record['side'] if perspective=='for' else 1-record['side']
    return float(pair[side])


def generate(rows,statistics,*,mode='retrospective_latest',observed_before=None):
    """Yield per-fixture dictionaries with stable core/rich numeric schemas.

    Last10 refers to all past matches, including those missing statistics, never
    the last ten available rich matches. Target statistics do not determine its
    inclusion. stats_as_observed constrains statistics only, not undocumented
    historical availability/corrections of score files.
    """
    if mode not in ('retrospective_latest','stats_as_observed'):raise ValueError('Unknown availability mode.')
    if isinstance(observed_before,datetime):
        if observed_before.tzinfo is None:raise ValueError('An aware observation cutoff is required.')
        observed_before=observed_before.timestamp()
    if observed_before is not None and (isinstance(observed_before,bool) or not isinstance(observed_before,(int,float)) or not isfinite(observed_before)):
        raise ValueError('A finite observation cutoff is required.')
    prepared=_prepared(rows)
    history=defaultdict(lambda:deque(maxlen=10));counts=Counter();latest={}
    league_states=defaultdict(lambda:dict(n=0,days=set(),classes=Counter(),goals=[0.,0.,0.,0.]))
    role_totals=defaultdict(lambda:dict(n=0,scored=[0.,0.],conceded=[0.,0.]))
    pending=deque()

    def update(row):
        lid=row['league'];state=league_states[lid];ht,ft=row['ht'],row['ft']
        values=(ht[0],ht[1],ft[0]-ht[0],ft[1]-ht[1])
        state['n']+=1;state['days'].add(row['day']);state['classes'][row['actual']]+=1
        for i,value in enumerate(values):state['goals'][i]+=value
        for side,(team,role) in enumerate(((row['home_id'],'home'),(row['away_id'],'away'))):
            goal=(values[side],values[1-side],values[side+2],values[3-side])
            record=dict(row,side=side,goal=goal)
            for context in ('all',role):
                key=(lid,team,context);history[key].append(record);counts[key]+=1;latest[key]=row['stamp']
            total=role_totals[(lid,team,role)];total['n']+=1
            for half in (0,1):
                total['scored'][half]+=goal[2*half];total['conceded'][half]+=goal[2*half+1]

    for day,day_rows in groupby(prepared,key=lambda row:row['day']):
        batch=list(day_rows);day_start=_stamp(day+'T00:00:00')
        while pending and pending[0]['stamp']+timedelta(hours=6)<=day_start:
            update(pending.popleft())
        for row in batch:
            lid=row['league'];state=league_states[lid];n=state['n']
            frequency=[(state['classes'][label]+1)/(n+3) for label in LABELS]
            home=role_totals[(lid,row['home_id'],'home')];away=role_totals[(lid,row['away_id'],'away')]
            eligible=n>=30 and len(state['days'])>=3
            personalized=eligible and min(home['n'],away['n'])>=3
            clubs=list(frequency)
            if personalized:
                means=[]
                for half in (0,1):
                    hm=state['goals'][2*half]/n;am=state['goals'][2*half+1]/n
                    hs=(home['scored'][half]+8*hm)/(home['n']+8)
                    ac=(away['conceded'][half]+8*hm)/(away['n']+8)
                    ass=(away['scored'][half]+8*am)/(away['n']+8)
                    hc=(home['conceded'][half]+8*am)/(home['n']+8)
                    means.append((hs+ac+ass+hc)/2)
                team=_half_probabilities(*means);weight=min(home['n'],away['n'])/(min(home['n'],away['n'])+8)
                clubs=[weight*p+(1-weight)*base for p,base in zip(team,frequency)]
            core=dict(league_count=n,eligible=float(eligible),personalized=float(personalized),season=row['season'],
                      month=row['stamp'].month,weekday=row['stamp'].weekday(),hour_utc=row['stamp'].hour)
            core.update({'league_'+league:float(lid==league) for league in LEAGUES})
            core.update({'frequency_'+str(i):value for i,value in enumerate(frequency)})
            core.update({'clubs_'+str(i):value for i,value in enumerate(clubs)})
            keys=((lid,row['home_id'],'all'),(lid,row['home_id'],'home'),(lid,row['away_id'],'all'),(lid,row['away_id'],'away'))
            rich={};support={};history_ids={}
            for prefix,key in zip(PROFILES,keys):
                records=list(history[key]);history_ids[prefix]=[r['match_id'] for r in records]
                core[prefix+'_count']=counts[key];core[prefix+'_last10_count']=len(records)
                core[prefix+'_days_rest']=(day_start-latest[key]).total_seconds()/86400 if key in latest else -1.
                for i,name in enumerate(GOALS):
                    values=[r['goal'][i] for r in records]
                    mean=fsum(values)/len(values) if values else 0.
                    core[prefix+'_'+name+'_mean']=mean
                    core[prefix+'_'+name+'_recent3_delta']=(fsum(values[-3:])/len(values[-3:])-mean) if values else 0.
                core_support=[]
                for record in records:
                    valid=all(_rich_value(record,statistics,period,metric,'for',mode=mode,day_start=day_start,observed_before=observed_before) is not None
                              for period in ('first','second') for metric in ('Total Shots','Shots on Goal'))
                    core_support.append(valid)
                support[prefix]=sum(core_support)
                for period in ('first','second'):
                    for tag,metric in METRICS:
                        for perspective in ('for','against'):
                            values=[_rich_value(r,statistics,period,metric,perspective,mode=mode,day_start=day_start,observed_before=observed_before) for r in records]
                            known=[v for v in values if v is not None];recent=[v for v in values[-3:] if v is not None]
                            mean=fsum(known)/len(known) if known else 0.
                            base=prefix+'_'+period+'_'+tag+'_'+perspective+'_'
                            rich.update({base+'mean':mean,base+'count':len(known),base+'recent3_delta':fsum(recent)/len(recent)-mean if recent else 0.,base+'recent3_count':len(recent),base+'missing':float(not known)})
            assert set(core)==set(CORE_COLUMNS) and set(rich)==set(RICH_COLUMNS)
            assert all(isfinite(float(v)) for obj in (core,rich) for v in obj.values())
            yield dict(match_id=row['match_id'],day=day,league=lid,actual=row['actual'],
                       frequency=frequency,clubs=clubs,core=core,rich=rich,support=support,
                       history_ids=history_ids,personalized=personalized,eligible=eligible)
        pending.extend(batch)
