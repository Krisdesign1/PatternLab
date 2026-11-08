"""Explicit exploratory sensitivity to cold-start encoding in count models.

This second experiment follows the failed raw-feature count test. It cannot be
treated as independent discovery. Prior means are reconstructed using past days
only; missing history is neutral relative to that competition/period, never zero
attacking ability. Existing experiment/code/data remain immutable.
"""
from collections import defaultdict,deque
from datetime import datetime,timedelta,timezone
from hashlib import sha256
from itertools import groupby
from pathlib import Path
import argparse
import json

import numpy as np

from halftime_count_experiment import SOURCE,ROOT,PROFILES,EXCLUDED_XG,read,write,clean_rich,run

GOALS=('ht_for','ht_against','sh_for','sh_against')
SCORE_COLUMNS=tuple(p+'_'+goal+'_contrast' for p in PROFILES for goal in GOALS)
RICH_COLUMNS=tuple(p+'_'+period+'_'+metric+'_'+side+'_contrast'
    for p in PROFILES for period in ('first','second') for metric in ('xg','on_goal') for side in ('for','against'))
SHRINKAGE=8.


def stamp(value):
    d=datetime.fromisoformat(value.replace('Z','+00:00'))
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def contrast(mean,count,prior):
    if count<0 or prior<0 or mean<0:raise ValueError('Negative profile quantity.')
    if count==0 or prior==0:return 0.
    return float(np.log((mean*count+SHRINKAGE*prior)/((count+SHRINKAGE)*prior)))


def prepare_arrays(profiles,rows,statistics,ids):
    ordered=sorted(rows.values(),key=lambda r:(stamp(r['timestamp']),int(r['match_id'])))
    state=defaultdict(lambda:defaultdict(lambda:[0.,0]))
    pending=deque();output={}

    def update(row):
        lid=row['league'];ht=list(map(int,row['ht'].split('-')));ft=list(map(int,row['ft'].split('-')))
        for period,goals in (('first',ht),('second',[ft[i]-ht[i] for i in (0,1)])):
            for side in (0,1):
                v=state[lid][period,'goals',side];v[0]+=goals[side];v[1]+=1
            item=statistics.get(row['match_id'])
            for tag,metric in (('xg','expected_goals'),('on_goal','Shots on Goal')):
                if tag=='xg' and row['match_id'] in EXCLUDED_XG:continue
                pair=(item or {}).get('periods',{}).get(period,{}).get(metric)
                if pair is None:continue
                for side in (0,1):
                    v=state[lid][period,tag,side];v[0]+=pair[side];v[1]+=1

    def prior(lid,period,metric,prefix,perspective):
        role=prefix.split('_')[1]
        sides=(0,1) if role=='all' else ((0,) if role=='home' else (1,))
        if perspective=='against':sides=tuple(1-side for side in sides)
        cells=[state[lid][period,metric,side] for side in sides]
        n=sum(cell[1] for cell in cells)
        return sum(cell[0] for cell in cells)/n if n else 0.

    wanted=set(map(str,ids))
    for day,items in groupby(ordered,key=lambda r:r['timestamp'][:10]):
        batch=list(items);start=stamp(day+'T00:00:00')
        while pending and stamp(pending[0]['timestamp'])+timedelta(hours=6)<=start:update(pending.popleft())
        for row in batch:
            mid=row['match_id']
            if mid not in wanted:continue
            p=profiles[mid];lid=row['league']
            cleaned=clean_rich(dict(p,_home_id=str(row['home_id']),_away_id=str(row['away_id'])),rows,statistics)
            core=[];extra=[]
            for prefix in PROFILES:
                n=p['core'][prefix+'_last10_count']
                for goal in GOALS:
                    period='first' if goal.startswith('ht_') else 'second';perspective=goal.split('_')[1]
                    base=prior(lid,period,'goals',prefix,perspective)
                    core.append(contrast(p['core'][prefix+'_'+goal+'_mean'],n,base))
            for prefix in PROFILES:
                for period in ('first','second'):
                    for metric in ('xg','on_goal'):
                        for perspective in ('for','against'):
                            key=prefix+'_'+period+'_'+metric+'_'+perspective+'_'
                            extra.append(contrast(cleaned[key+'mean'],cleaned[key+'count'],prior(lid,period,metric,prefix,perspective)))
            output[mid]=(core,extra)
        pending.extend(batch)
    if set(output)!=wanted:raise ValueError('Missing target profile.')
    core=np.asarray([output[str(mid)][0] for mid in ids]);extra=np.asarray([output[str(mid)][1] for mid in ids])
    if not np.isfinite(core).all() or not np.isfinite(extra).all():raise ValueError('Invalid contrast.')
    return core,np.column_stack((core,extra))


def prepare(directory):
    from halftime_count_experiment import verify
    previous=ROOT/'data/research/halftime_counts/2026-10-27-v1'
    old=verify(previous)
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=False)
    plan=dict(old,created_at_utc=datetime.now(timezone.utc).isoformat(),
        hypothesis='Sensitivity: replace zero-imputed histories/count indicators by past-league-centered pseudo-count contrasts. Missing profiles neutral, support controls shrinkage.',
        exploration='Second exploratory count experiment after seeing failures of v1, including2026; no independent confirmation or retrospective deployment.',
        features=dict(scores=list(SCORE_COLUMNS),rich_additional=list(RICH_COLUMNS),
            transform='log((n*mean+8*causal_competition_period_role_mean)/(n+8)/causal_mean); zero support or missing causal mean=>neutral0',
            shrinkage=SHRINKAGE,removed='Raw history count, rest and zero-filled level features; no support-based target filtering.',
            excluded_xg_fixture_ids=list(EXCLUDED_XG),cleaning=old['features']['cleaning']),
        previous_experiment_plan_sha256=sha256((previous/'plan.json').read_bytes()).hexdigest(),
        code_sha256={name:sha256((ROOT/name).read_bytes()).hexdigest() for name in ('halftime_count_experiment.py','halftime_count_models.py','halftime_count_priors.py')})
    write(directory/'plan.json',plan)
    with np.load(previous/'dataset.npz',allow_pickle=False) as f:a={k:f[k] for k in f.files if k not in ('scores','rich')}
    rows={r['match_id']:r for r in read(SOURCE/'snapshot/rows.json.gz')}
    statistics=read(SOURCE/'snapshot/statistics.json.gz')
    import gzip
    with gzip.open(SOURCE/'profiles.ndjson.gz','rt',encoding='utf-8') as f:profiles={p['match_id']:p for p in map(json.loads,f)}
    scores,rich=prepare_arrays(profiles,rows,statistics,a['ids']);a.update(scores=scores,rich=rich)
    np.savez_compressed(directory/'dataset.npz',**a)
    summary=dict(examples=len(scores),score_features=scores.shape[1],rich_features=rich.shape[1],
        counts_by_year={year:int(np.char.startswith(a['days'],year).sum()) for year in ('2024','2025','2026')},
        data_sha256=sha256((directory/'dataset.npz').read_bytes()).hexdigest(),plan_sha256=sha256((directory/'plan.json').read_bytes()).hexdigest())
    write(directory/'preparation.json',summary)
    return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('prepare','run'));parser.add_argument('--directory',required=True,type=Path)
    args=parser.parse_args();result=prepare(args.directory) if args.action=='prepare' else run(args.directory)
    print(json.dumps(result,ensure_ascii=False,allow_nan=False))
