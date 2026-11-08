"""Frozen exploratory diagnosis of fit volume and goal-volume/allocation signal.

No API, application or credentials imports. No 2026 outcomes are evaluated.
Sampling reduces supervised fitting rows; causal feature histories stay complete.
This distinction prevents a learning curve from being sold as collection power.
"""
from collections import defaultdict,deque
from datetime import datetime,timedelta,timezone
from hashlib import sha256
from pathlib import Path
import argparse
import gzip
import json

import numpy as np
from scipy.special import xlogy
from scipy.stats import binom,poisson,skellam

from halftime_count_experiment import ROOT,SOURCE,read,write
from halftime_count_models import fit_count,predict_means

PRIOR=ROOT/'data/research/halftime_counts/2026-10-27-priors-v1'
SEEDS=(2026102701,2026102702,2026102703,2026102704,2026102705)
FRACTIONS=(.25,.5,.75,1.)
FOLDS=(dict(name='2025_H1',fit_before='2025-01-01',start='2025-01-01',end='2025-07-01'),
       dict(name='2025_H2',fit_before='2025-07-01',start='2025-07-01',end='2026-01-01'))


def class_labels(goals):
    g=np.asarray(goals)
    return np.where(g[:,0]>g[:,1],0,np.where(g[:,0]==g[:,1],1,2))


def probabilities(means):
    means=np.asarray(means,dtype=float)
    if means.ndim!=2 or means.shape[1]!=2 or not np.isfinite(means).all() or (means<=0).any():raise ValueError('Positive finite means required.')
    a,b=means.T
    result=np.column_stack((skellam.sf(0,a,b),skellam.pmf(0,a,b),skellam.cdf(-1,a,b)))
    if not np.isfinite(result).all() or not np.allclose(result.sum(1),1.,atol=1e-10):raise ValueError('Invalid Skellam probabilities.')
    result=np.maximum(result,1e-15)
    return result/result.sum(1)[:,None]


def losses(goals,means):
    goals=np.asarray(goals,dtype=int);means=np.asarray(means,dtype=float)
    total=goals.sum(1);mu=means.sum(1);share=means[:,0]/mu
    volume=-poisson.logpmf(total,mu)
    allocation=-binom.logpmf(goals[:,0],total,share)
    joint=-poisson.logpmf(goals,means).sum(1)
    if not np.allclose(volume+allocation,joint,atol=1e-10):raise ValueError('Factorization mismatch.')
    p=probabilities(means);y=class_labels(goals)
    return dict(class_nll=-np.log(p[np.arange(len(y)),y]),total_nll=volume,allocation_nll=allocation,joint_nll=joint)


def class_scores(goals,p):
    y=class_labels(goals);p=np.asarray(p)
    return dict(matches=len(y),class_logloss=float(-np.log(p[np.arange(len(y)),y]).mean()),
        accuracy=float((p.argmax(1)==y).mean()),brier=float(((p-np.eye(3)[y])**2).sum(1).mean()))


def score(goals,means):
    result=class_scores(goals,probabilities(means));l=losses(goals,means)
    result.update({name:float(values.mean()) for name,values in l.items() if name!='class_nll'})
    total=goals.sum(1);positive=total>0;share=means[:,0]/means.sum(1)
    result.update(total_mae=float(np.abs(total-means.sum(1)).mean()),
        allocation_nll_positive_total=float(l['allocation_nll'][positive].mean()) if positive.any() else None,
        allocation_crossentropy_per_goal=float(-(xlogy(goals[:,0],share)+xlogy(goals[:,1],1-share)).sum()/total.sum()) if total.sum() else None,
        mean_predicted_halves=means.mean(0).tolist(),mean_observed_halves=goals.mean(0).tolist())
    return result


def nested_indices(days,leagues,fraction,seed):
    """Nested whole league-day sampling within league x calendar month strata."""
    if fraction not in FRACTIONS:raise ValueError('Unplanned fraction.')
    groups=defaultdict(set)
    for day,lid in zip(days,leagues):groups[str(lid),str(day)[:7]].add(str(day))
    selected=set()
    for (lid,month),dates in sorted(groups.items()):
        ranked=sorted(dates,key=lambda day:(sha256(f'{seed}|{lid}|{month}|{day}'.encode()).digest(),day))
        count=int(np.floor(fraction*len(ranked)+.5))
        selected.update((lid,day) for day in ranked[:count])
    index=np.asarray([i for i,(d,l) in enumerate(zip(days,leagues)) if (str(l),str(d)) in selected],dtype=int)
    if set(map(str,np.asarray(leagues)[index]))!=set(map(str,leagues)):raise ValueError('Sampling omitted a league; no outcome-based repair permitted.')
    return index


def permuted_goals(goals,days,leagues,seed):
    result=np.asarray(goals).copy();rng=np.random.default_rng(seed);groups=defaultdict(list)
    for i,(day,lid) in enumerate(zip(days,leagues)):groups[str(lid),str(day)[:7]].append(i)
    for key,indices in sorted(groups.items()):result[indices]=goals[rng.permutation(indices)]
    return result


def synthetic_counts(train_x,valid_x,seed):
    column=16;center=float(train_x[:,column].mean());scale=float(train_x[:,column].std())
    if scale<=0:raise ValueError('Synthetic-control feature has no variance.')
    rng=np.random.default_rng(seed);outputs=[];truth=[]
    for x in (train_x,valid_x):
        z=np.clip((x[:,column]-center)/scale,-3.,3.)
        means=np.column_stack((1.2*np.exp(.5*z),1.5*np.exp(-.5*z)))
        truth.append(means);outputs.append(rng.poisson(means))
    return *outputs,dict(column=column,center=center,scale=scale,coefficient=.5,clip_z=[-3.,3.]),truth


def stamp(value):
    d=datetime.fromisoformat(value.replace('Z','+00:00'))
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def causal_league_means(rows,ids):
    ordered=sorted(rows,key=lambda r:(stamp(r['timestamp']),int(r['match_id'])))
    state=defaultdict(lambda:[0,0,0]);pending=deque();wanted=set(map(str,ids));out={}
    for day in sorted({r['timestamp'][:10] for r in ordered}):
        start=stamp(day+'T00:00:00')
        while pending and stamp(pending[0]['timestamp'])+timedelta(hours=6)<=start:
            r=pending.popleft();n=sum(map(int,r['ht'].split('-')));total=sum(map(int,r['ft'].split('-')))
            s=state[str(r['league'])];s[0]+=n;s[1]+=total-n;s[2]+=1
        batch=[r for r in ordered if r['timestamp'].startswith(day)]
        for r in batch:
            if r['match_id'] in wanted:
                s=state[str(r['league'])];out[r['match_id']]=[(s[0]+1)/(s[2]+1),(s[1]+1)/(s[2]+1)]
        pending.extend(batch)
    if set(out)!=wanted:raise ValueError('Missing causal league baseline.')
    return np.asarray([out[str(mid)] for mid in ids])


def prepare(directory):
    from halftime_count_experiment import verify
    from halftime_research_features import generate
    from halftime_count_priors import prepare_arrays
    from halftime_research_dataset import load_snapshot
    old=verify(PRIOR);load_snapshot(SOURCE/'snapshot')
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=False)
    plan=dict(created_at_utc=datetime.now(timezone.utc).isoformat(),client_date='2026-10-27',
        purpose='Diagnose supervised fit-row volume, enriched signal for total versus allocation, and finite-sample sensitivity; no new model selection.',
        source=str(PRIOR.relative_to(ROOT)),source_preparation_sha256=sha256((PRIOR/'preparation.json').read_bytes()).hexdigest(),
        source_snapshot_hashes_sha256=sha256((SOURCE/'snapshot/hashes.json').read_bytes()).hexdigest(),
        validation_folds=list(FOLDS),fractions=list(FRACTIONS),seeds=list(SEEDS),
        sampling='Rank whole league-day blocks by seeded hash within league x calendar month; round half up fraction*blocks. Nested ranks; no class/outcome repairs. Require all training leagues.',
        full_fraction='Fit100%once per fold/family. Source causal histories remain complete even for omitted supervised labels; this tests coefficient-estimation sample size, not reduced collection.',
        features='Frozen prior-centred16scores/48rich, no feature selection or parameter grid.',
        learner='Existing fixed two-head Poisson ridge, numeric prior strength30 and league1. Scaling fit-only; effective mean-loss penalty30/n decreases with n as part of unchanged estimator.',
        controls=dict(null='3seeds2026102711..13: paired(G1,G2) shuffled only within fit league x month; validation untouched. Consistency control, not exact permutation p-value.',
            synthetic='One seed2026102799+foldindex, feature16 standardized on full fit only; zclipped[-3,3]; trueG1mean1.2exp(.5z),G2mean1.5exp(-.5z). Compare rich versus league-only. Synthetic labels separate, not real accuracy.'),
        augmentation='Secondary fullfit plus eligible pre2024 score labels only. No statistics for those labels; they cover only39/61. Changed time/geography/composition, not pure volume evidence.',
        factorization='PoissonNLL(G1)+PoissonNLL(G2)=PoissonNLL(total)+BinomialNLL(G1|total,p). Observed total only scores conditional likelihood; never used in class prediction.',
        hybrids='Fullfits: cross predicted total mean from scores/rich with predicted first share from scores/rich; integrate unknown target total for all class forecasts.',
        uncertainty='Seed spread describes fit-subset sensitivity, not sample confidence. Paired ISOweek bootstrap2000seed20261027 for fullfit rich-score gains,>=20closed weeks; descriptive, no independence or multiplicity correction.',
        no_2026_evaluation=True,no_production_changes=True,
        caveat='Historical2025 repeatedly examined, folds sequential and not independent. A learning-curve plateau cannot prove all possible features or models lack signal.',
        frozen_prior_code={**old['frozen_prior_code'],**old['code_sha256']},
        code_sha256=sha256((ROOT/'halftime_learning_diagnostic.py').read_bytes()).hexdigest())
    write(directory/'plan.json',plan)
    with np.load(PRIOR/'dataset.npz',allow_pickle=False) as f:
        mask=f['days']<'2026-01-01';a={k:f[k][mask] for k in f.files}
    rows=[r for r in read(SOURCE/'snapshot/rows.json.gz') if r['timestamp']<'2026-01-01']
    a['league_means']=causal_league_means(rows,a['ids'])
    past=[r for r in rows if r['timestamp']<'2024-01-01'];profiles={p['match_id']:p for p in generate(past,{}) if p['eligible']}
    ids=np.asarray(list(profiles));rowmap={r['match_id']:r for r in past}
    scores,rich=prepare_arrays(profiles,rowmap,{},ids)
    a.update(warmup_ids=ids,warmup_days=np.asarray([p['day'] for p in profiles.values()]),
        warmup_leagues=np.asarray([p['league'] for p in profiles.values()]),warmup_scores=scores,warmup_rich=rich,
        warmup_goals=np.asarray([(sum(map(int,rowmap[mid]['ht'].split('-'))),sum(map(int,rowmap[mid]['ft'].split('-')))-sum(map(int,rowmap[mid]['ht'].split('-')))) for mid in ids]))
    np.savez_compressed(directory/'dataset.npz',**a)
    summary=dict(examples=len(a['ids']),max_target_day=str(max(a['days'])),warmup_examples=len(ids),warmup_by_league={lid:int((a['warmup_leagues']==lid).sum()) for lid in set(a['warmup_leagues'])},
        data_sha256=sha256((directory/'dataset.npz').read_bytes()).hexdigest(),plan_sha256=sha256((directory/'plan.json').read_bytes()).hexdigest())
    write(directory/'preparation.json',summary);return summary


def verify(directory):
    plan=read(directory/'plan.json');prepared=read(directory/'preparation.json')
    for name,digest in plan['frozen_prior_code'].items():
        if sha256((ROOT/name).read_bytes()).hexdigest()!=digest:raise ValueError('Frozen code changed '+name)
    if sha256((ROOT/'halftime_learning_diagnostic.py').read_bytes()).hexdigest()!=plan['code_sha256']:raise ValueError('Diagnostic code changed.')
    for filename,key in (('dataset.npz','data_sha256'),('plan.json','plan_sha256')):
        if sha256((directory/filename).read_bytes()).hexdigest()!=prepared[key]:raise ValueError('Artifact changed.')
    return plan


def paired_intervals(goals,rich,scores,days,fold):
    a,b=losses(goals,rich),losses(goals,scores);groups=defaultdict(list)
    for i,day in enumerate(days):
        d=datetime.fromisoformat(str(day));monday=d-timedelta(days=d.weekday())
        if monday<datetime.fromisoformat(fold['start']) or monday+timedelta(days=7)>datetime.fromisoformat(fold['end']):continue
        groups[monday.date().isoformat()].append(i)
    out={};indices=list(groups.values());rng=np.random.default_rng(20261027)
    draws=rng.integers(0,len(indices),(2000,len(indices))) if len(indices)>=20 else None
    for key in a:
        gain=b[key]-a[key];block_sums=np.asarray([gain[idx].sum() for idx in indices]);counts=np.asarray([len(idx) for idx in indices])
        interval=None
        if draws is not None:interval=np.quantile(block_sums[draws].sum(1)/counts[draws].sum(1),[.025,.975]).tolist()
        out[key]=dict(gain_positive_is_better=float(gain.mean()),closed_weeks=len(groups),interval=interval)
        closed=[i for idx in indices for i in idx]
        out[key].update(matches_in_interval=len(closed),gain_closed_weeks=float(gain[closed].mean()) if closed else None)
    return out


def run(directory):
    directory=Path(directory);plan=verify(directory);out=directory/'evaluation';out.mkdir(exist_ok=False)
    with np.load(directory/'dataset.npz',allow_pickle=False) as f:a={k:f[k] for k in f.files}
    if (a['days']>='2026-01-01').any():raise ValueError('No2026targets permitted.')
    entries=[];factor_reports={};full_predictions={}

    def fit_one(name,family,train,valid,*,override=None,extra=False,valid_override=None):
        x=a[family][train] if family!='league' else np.empty((len(train),0));v=a[family][valid] if family!='league' else np.empty((len(valid),0))
        goals=a['goals'][train] if override is None else override
        lids=a['leagues'][train];ids=a['ids'][train]
        if extra:
            x=np.vstack((a['warmup_'+family],x));goals=np.vstack((a['warmup_goals'],goals));lids=np.concatenate((a['warmup_leagues'],lids));ids=np.concatenate((a['warmup_ids'],ids))
        model=fit_count(x,goals,lids,penalty=30.)
        tmeans=predict_means(model,x,lids);means=predict_means(model,v,a['leagues'][valid])
        target=a['goals'][valid] if valid_override is None else valid_override
        write(out/(name+'_model.json'),model)
        np.savez_compressed(out/(name+'_predictions.npz'),train_ids=ids,train_goals=goals,train_means=tmeans,valid_ids=a['ids'][valid],goals=target,means=means,probabilities=probabilities(means))
        item=dict(name=name,family=family,fit_matches=len(ids),fit_min_day=str(min(a['days'][train])),fit_max_day=str(max(a['days'][train])),validation_matches=len(valid),train=score(goals,tmeans),validation=score(target,means),
            by_league={lid:score(target[a['leagues'][valid]==lid],means[a['leagues'][valid]==lid]) for lid in sorted(set(a['leagues'][valid]),key=int)})
        if extra:item['fit_min_day']=str(min(a['warmup_days']))
        entries.append(item)
        print(json.dumps(dict(completed=name,fit=len(ids),validation_class_logloss=item['validation']['class_logloss']),ensure_ascii=False),flush=True)
        return means,item

    for fnum,fold in enumerate(plan['validation_folds']):
        train=np.flatnonzero(a['days']<fold['fit_before']);valid=np.flatnonzero((a['days']>=fold['start'])&(a['days']<fold['end']))
        base=score(a['goals'][valid],a['league_means'][valid]);clubs=class_scores(a['goals'][valid],a['clubs'][valid])
        full={}
        for fraction in plan['fractions']:
            for seed in ((SEEDS[0],) if fraction==1 else SEEDS):
                relative=nested_indices(a['days'][train],a['leagues'][train],fraction,seed);subset=train[relative]
                for family in ('scores','rich'):
                    name=fold['name']+f'_curve_{int(fraction*100)}_{seed}_{family}'
                    means,item=fit_one(name,family,subset,valid);item.update(kind='curve',fold=fold['name'],fraction=fraction,seed=seed)
                    if fraction==1:full[family]=means
        _,anchor=fit_one(fold['name']+'_league_anchor','league',train,valid);anchor.update(kind='league_anchor',fold=fold['name'])
        for family in ('scores','rich'):
            _,item=fit_one(fold['name']+'_old_labels_'+family,family,train,valid,extra=True);item.update(kind='old_labels',fold=fold['name'])
        for seed in (2026102711,2026102712,2026102713):
            perm=permuted_goals(a['goals'][train],a['days'][train],a['leagues'][train],seed)
            for family in ('scores','rich'):
                _,item=fit_one(fold['name']+f'_null_{seed}_{family}',family,train,valid,override=perm);item.update(kind='shuffled_control',fold=fold['name'],seed=seed)
        sy,vy,synthetic_spec,truth=synthetic_counts(a['rich'][train],a['rich'][valid],2026102799+fnum)
        synthetic={}
        for family in ('league','scores','rich'):
            _,item=fit_one(fold['name']+'_synthetic_'+family,family,train,valid,override=sy,valid_override=vy);item.update(kind='synthetic_control',fold=fold['name']);synthetic[family]=item['validation']
        mu={name:m.sum(1) for name,m in full.items()};share={name:m[:,0]/m.sum(1) for name,m in full.items()};hybrids={}
        for volume in ('scores','rich'):
            for allocation in ('scores','rich'):
                means=np.column_stack((mu[volume]*share[allocation],mu[volume]*(1-share[allocation])))
                hybrids['volume_'+volume+'_allocation_'+allocation]=score(a['goals'][valid],means)
        factor_reports[fold['name']]=dict(league_causal=base,clubs=clubs,hybrids=hybrids,
            paired_rich_vs_scores=paired_intervals(a['goals'][valid],full['rich'],full['scores'],a['days'][valid],fold),
            synthetic_specification=synthetic_spec,synthetic_true_law_score=score(vy,truth[1]),synthetic_results=synthetic)
        full_predictions[fold['name']]=dict(ids=a['ids'][valid].tolist(),days=a['days'][valid].tolist())
    result=dict(status='exploratory_learning_diagnostic',created_at_utc=datetime.now(timezone.utc).isoformat(),
        entries=entries,factors=factor_reports,validation_targets=full_predictions,no2026evaluated=True,production_changed=False)
    write(out/'results.json',result);verify(directory);return dict(status=result['status'],fits=len(entries),directory=str(directory))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=('prepare','run'));p.add_argument('--directory',type=Path,required=True)
    args=p.parse_args();print(json.dumps(prepare(args.directory) if args.action=='prepare' else run(args.directory),ensure_ascii=False))
