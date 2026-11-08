"""Fixed research-only heads and paired evaluation; no serving-model writes."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import numpy as np

LABELS=('1st','Égal','2nd')


def _write(path,value):
    with Path(path).open('x',encoding='utf-8') as handle:json.dump(value,handle,ensure_ascii=False,indent=2,allow_nan=False)


def fit(x,y,offset,parameters):
    from catboost import CatBoostClassifier,Pool
    if set(np.asarray(y).tolist())!={0,1,2}:raise ValueError('Three training classes required.')
    pool=Pool(np.asarray(x,dtype=np.float32),label=[LABELS[i] for i in y],
              baseline=np.log(offset).astype(np.float32))
    model=CatBoostClassifier(**parameters)
    model.fit(pool)
    if list(model.classes_)!=list(LABELS):raise ValueError('Unexpected class order.')
    return model


def predict(model,x,offset):
    from catboost import Pool
    result=np.asarray(model.predict_proba(Pool(np.asarray(x,dtype=np.float32),
        baseline=np.log(offset).astype(np.float32))))
    _probabilities(result,len(x))
    return result


def _probabilities(p,n):
    if p.shape!=(n,3) or not np.isfinite(p).all() or (p<=0).any() or not np.allclose(p.sum(1),1.,atol=1e-10):
        raise ValueError('Invalid class distribution.')


def metrics(y,p,reference=None):
    y=np.asarray(y,dtype=int);p=np.asarray(p,dtype=float);_probabilities(p,len(y))
    if not len(y):return dict(matches=0)
    winner=p.argmax(1)
    result=dict(matches=len(y),correct=int((winner==y).sum()),accuracy=float((winner==y).mean()),
        logloss=float(-np.log(p[np.arange(len(y)),y]).mean()),
        brier=float(((p-np.eye(3)[y])**2).sum(1).mean()),
        predicted_classes={label:int((winner==i).sum()) for i,label in enumerate(LABELS)},
        confidence_thresholds={},calibration={})
    for threshold in (.5,.6,.7):
        mask=p.max(1)>=threshold;n=int(mask.sum())
        result['confidence_thresholds'][str(threshold)]=dict(matches=n,coverage=float(mask.mean()),
            correct=int((winner[mask]==y[mask]).sum()),accuracy=float((winner[mask]==y[mask]).mean()) if n else None,
            mean_confidence=float(p[mask].max(1).mean()) if n else None,
            reference_accuracy=float((reference[mask].argmax(1)==y[mask]).mean()) if n and reference is not None else None)
    for i,label in enumerate(LABELS):
        bins=[]
        for b in range(10):
            mask=(p[:,i]>=b/10)&(p[:,i]<(b+1)/10 if b<9 else p[:,i]<=1)
            n=int(mask.sum())
            bins.append(dict(lower=b/10,upper=(b+1)/10,count=n,
                probability=float(p[mask,i].mean()) if n else None,
                outcome_rate=float((y[mask]==i).mean()) if n else None))
        result['calibration'][label]=bins
    return result


def compare(y,candidate,reference,days,*,start='2026-01-01',end='2026-10-26'):
    y=np.asarray(y,dtype=int)
    if not len(y):return dict(matches=0,interval=None)
    delta=np.log(candidate[np.arange(len(y)),y])-np.log(reference[np.arange(len(y)),y])
    groups={}
    for i,day in enumerate(days):
        stamp=datetime.fromisoformat(str(day));year,week,_=stamp.isocalendar()
        monday=datetime.fromisocalendar(year,week,1)
        if monday<datetime.fromisoformat(start) or monday+timedelta(days=7)>datetime.fromisoformat(end):continue
        groups.setdefault((year,week),[]).append(i)
    result=dict(matches=len(y),gain_positive_is_better=float(delta.mean()),
                closed_weeks=len(groups),interval=None,
                warning='Exploratory paired week bootstrap; team dependence, repeated research and multiple comparisons remain.')
    if len(groups)>=20:
        indices=list(groups.values());sums=np.asarray([delta[idx].sum() for idx in indices]);counts=np.asarray([len(idx) for idx in indices])
        rng=np.random.default_rng(20261026);draws=rng.integers(0,len(indices),(2000,len(indices)))
        values=sums[draws].sum(1)/counts[draws].sum(1)
        result['interval']=np.quantile(values,[.025,.975]).tolist()
        result['closed_week_gain']=float(sums.sum()/counts.sum())
    return result


def evaluate_complete_snapshot(directory,plan):
    directory=Path(directory)
    ready=json.loads((directory/'readiness.json').read_text())
    if not ready.get('ready'):raise ValueError('Unresolved collection: no model evaluation.')
    arrays=np.load(directory/'dataset.npz',allow_pickle=False)
    ids,days,y=arrays['ids'],arrays['days'],arrays['y'];clubs=arrays['clubs'];frequency=arrays['frequency']
    if len(set(ids.tolist()))!=len(ids):raise ValueError('Duplicate target IDs.')
    _probabilities(clubs,len(y));_probabilities(frequency,len(y))
    fit_idx=np.flatnonzero(days<'2025-01-01')
    val_idx=np.flatnonzero((days>='2025-01-01')&(days<'2026-01-01'))
    final_idx=np.flatnonzero(days<'2026-01-01');test_idx=np.flatnonzero(days>='2026-01-01')
    if min(len(fit_idx),len(val_idx),len(test_idx))<300:raise ValueError('Insufficient partition sizes.')
    out=directory/'evaluation';out.mkdir(exist_ok=False)
    representations=dict(scores=arrays['core'],rich=np.column_stack((arrays['core'],arrays['rich'])))
    validation=dict(clubs=metrics(y[val_idx],clubs[val_idx]),frequency=metrics(y[val_idx],frequency[val_idx]))
    policies={}
    for name,x in representations.items():
        model=fit(x[fit_idx],y[fit_idx],clubs[fit_idx],plan['learner']['parameters'])
        p=predict(model,x[val_idx],clubs[val_idx]);validation[name]=metrics(y[val_idx],p,clubs[val_idx])
        model.save_model(str(out/(name+'_development.cbm')))
        policies[name]='head' if validation[name]['logloss']<validation['clubs']['logloss'] else 'clubs'
    # Persist policy choices before evaluating any final-period prediction.
    _write(out/'validation_selection.json',dict(metrics=validation,policies=policies))
    predictions=dict(frequency=frequency[test_idx],clubs=clubs[test_idx])
    for name,x in representations.items():
        model=fit(x[final_idx],y[final_idx],clubs[final_idx],plan['learner']['parameters'])
        predictions[name+'_fixed']=predict(model,x[test_idx],clubs[test_idx])
        predictions[name+'_policy']=predictions[name+'_fixed'] if policies[name]=='head' else clubs[test_idx]
        model.save_model(str(out/(name+'_final.cbm')))
    target=y[test_idx];target_days=days[test_idx]
    summary={name:metrics(target,p,clubs[test_idx]) for name,p in predictions.items()}
    comparisons=dict(rich_information_fixed_heads=compare(target,predictions['rich_fixed'],predictions['scores_fixed'],target_days),
        selected_policies=compare(target,predictions['rich_policy'],predictions['scores_policy'],target_days),
        rich_fixed_vs_clubs=compare(target,predictions['rich_fixed'],predictions['clubs'],target_days))
    supported=np.min(arrays['support'][test_idx],axis=1)>=5
    support_metrics={name:metrics(target[supported],p[supported],clubs[test_idx][supported]) for name,p in predictions.items()}
    league_metrics={}
    for league in sorted(set(arrays['leagues'][test_idx].tolist()),key=int):
        mask=arrays['leagues'][test_idx]==league
        league_metrics[league]={name:metrics(target[mask],p[mask],clubs[test_idx][mask]) for name,p in predictions.items()}
    np.savez_compressed(out/'predictions.npz',ids=ids[test_idx],days=target_days,y=target,**predictions)
    report=dict(created_at_utc=datetime.now(timezone.utc).isoformat(),status='exploratory_historical_evaluation',
        metrics=summary,comparisons=comparisons,policies=policies,both_clubs_atleast5_rich=support_metrics,
        by_league=league_metrics,production_changed=False,
        warning='Already-explored historical years; independent predictive improvement requires new frozen prospective predictions.')
    _write(out/'results.json',report)
    return report
