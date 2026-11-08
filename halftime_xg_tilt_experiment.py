"""Frozen single-indicator offset test; a rejected branch is explicitly closed.

Research snapshots only. No model serving, API calls or production writes.
"""
from datetime import datetime,timedelta,timezone
from hashlib import sha256
from pathlib import Path
import argparse
import json

import numpy as np

from halftime_count_experiment import ROOT,read,write
from halftime_xg_tilt import indicator,fit,predict,BOUNDS,PENALTY

SOURCE=ROOT/'data/research/halftime_learning/2026-10-27-v1'
FOLDS=(dict(name='2025_H1',fit_before='2025-01-01',start='2025-01-01',end='2025-07-01'),
       dict(name='2025_H2',fit_before='2025-07-01',start='2025-07-01',end='2026-01-01'))
NAMES_FIRST=('home_all_first_xg_for_contrast','home_all_first_xg_against_contrast',
             'away_all_first_xg_for_contrast','away_all_first_xg_against_contrast')
NAMES_SECOND=tuple(name.replace('_first_','_second_') for name in NAMES_FIRST)


def prepare(directory):
    from halftime_learning_diagnostic import verify
    from halftime_count_priors import SCORE_COLUMNS,RICH_COLUMNS
    old=verify(SOURCE);directory=Path(directory);directory.mkdir(parents=True,exist_ok=False)
    columns=SCORE_COLUMNS+RICH_COLUMNS
    indices_first=[columns.index(name) for name in NAMES_FIRST];indices_second=[columns.index(name) for name in NAMES_SECOND]
    if indices_first!=[16,17,32,33] or indices_second!=[20,21,36,37]:raise ValueError('Frozen schema mismatch.')
    plan=dict(created_at_utc=datetime.now(timezone.utc).isoformat(),client_date='2026-10-27',
        purpose='One final targeted aggregate-xG profile hypothesis: one prematch signal corrects fixed Clubs probabilities; null correction allowed.',
        source=str(SOURCE.relative_to(ROOT)),source_preparation_sha256=sha256((SOURCE/'preparation.json').read_bytes()).hexdigest(),
        source_data_sha256=read(SOURCE/'preparation.json')['data_sha256'],
        signal=dict(first_columns=list(NAMES_FIRST),second_columns=list(NAMES_SECOND),first_indices=indices_first,second_indices=indices_second,
            formula='(sum4first_xG_contrasts-sum4second_xG_contrasts)/4; only generalhome/awayprofiles, no venuefeatures, shots, rest or additional scale/intercept.',
            meaning='Mean log-relative first-versus-second xG intensity, for/against both clubs, shrunk8 toward past competition period baseline. Missingprofile=>itscontrast0; entirelymissinghistories=>signal0.',
            limitations='retrospective_latest xG collected in2026; causal match ordering does not establish historical publication availability.'),
        learner=dict(formula='softmax(log(PClubs)+beta*signal*[+1,0,-1])',order=['1st','Égal','2nd'],bounds=list(BOUNDS),penalty=PENALTY,
            objective='SUM train class negative loglikelihood +8*beta^2/2; no class weights, intercept, extra scaling or hyperparameter search.',
            optimizer='Convex derivative root, or KKT boundary solution; beta=0 baseline compared explicitly.'),
        validation_folds=list(FOLDS),gate='Strictly lower class logloss AND correctclass count>=Clubs in EACH2025fold. Exactties in loss preferClubs. Brier/calibration descriptive; no averaging compensates a failedfold.',
        stop_rule='If anyfold fails, reject correction and close further variants of the current aggregate-xG-profile branch. Keep all data/artifacts. Reopening needs a separately justified hypothesis or new evidence.',
        pass_rule='Historical pass only: freeze candidate before an independently specified prospective stage. No automatic production promotion.',
        exploration='2025repeatedly researched; this test is exploratory and not an independent confirmation. No2026outcomes evaluated. No70%promise.',
        uncertainty='PairedISOweek bootstrap2000seed20261027 on completeweeks with>=20blocks; report closed-block point separately; descriptive with dependencies/repeatedtesting.',
        frozen_prior_code={**old['frozen_prior_code'],'halftime_learning_diagnostic.py':old['code_sha256']},
        code_sha256={name:sha256((ROOT/name).read_bytes()).hexdigest() for name in ('halftime_xg_tilt.py','halftime_xg_tilt_experiment.py')})
    write(directory/'plan.json',plan)
    with np.load(SOURCE/'dataset.npz',allow_pickle=False) as f:
        a={k:f[k] for k in ('ids','days','leagues','y','goals','clubs')};a['signal']=indicator(f['rich'])
    if len(set(a['ids']))!=len(a['ids']) or (a['days']>='2026-01-01').any():raise ValueError('Invalid target universe.')
    np.savez_compressed(directory/'dataset.npz',**a)
    summary=dict(examples=len(a['ids']),max_target_day=str(max(a['days'])),signal_nonzero=int((a['signal']!=0).sum()),
        data_sha256=sha256((directory/'dataset.npz').read_bytes()).hexdigest(),plan_sha256=sha256((directory/'plan.json').read_bytes()).hexdigest())
    write(directory/'preparation.json',summary);return summary


def verify(directory):
    plan=read(directory/'plan.json');prep=read(directory/'preparation.json')
    for name,digest in {**plan['frozen_prior_code'],**plan['code_sha256']}.items():
        if sha256((ROOT/name).read_bytes()).hexdigest()!=digest:raise ValueError('Frozen code changed '+name)
    for filename,key in (('plan.json','plan_sha256'),('dataset.npz','data_sha256')):
        if sha256((directory/filename).read_bytes()).hexdigest()!=prep[key]:raise ValueError('Prepared artifact changed.')
    return plan


def paired(y,p,base,days,fold):
    from collections import defaultdict
    gain=np.log(p[np.arange(len(y)),y])-np.log(base[np.arange(len(y)),y]);groups=defaultdict(list)
    for i,day in enumerate(days):
        d=datetime.fromisoformat(str(day));monday=d-timedelta(days=d.weekday())
        if monday<datetime.fromisoformat(fold['start']) or monday+timedelta(days=7)>datetime.fromisoformat(fold['end']):continue
        groups[monday.date().isoformat()].append(i)
    blocks=list(groups.values());flat=[i for block in blocks for i in block];interval=None
    if len(blocks)>=20:
        rng=np.random.default_rng(20261027);draws=rng.integers(0,len(blocks),(2000,len(blocks)))
        sums=np.asarray([gain[idx].sum() for idx in blocks]);counts=np.asarray([len(idx) for idx in blocks])
        interval=np.quantile(sums[draws].sum(1)/counts[draws].sum(1),[.025,.975]).tolist()
    return dict(gain_positive_is_better=float(gain.mean()),closed_weeks=len(blocks),matches_in_interval=len(flat),
        closed_week_gain=float(gain[flat].mean()) if flat else None,interval=interval)


def run(directory):
    from halftime_research_models import metrics
    directory=Path(directory);plan=verify(directory);out=directory/'evaluation';out.mkdir(exist_ok=False)
    with np.load(directory/'dataset.npz',allow_pickle=False) as f:a={k:f[k] for k in f.files}
    results={};all_y=[];all_p=[];all_base=[]
    for fold in plan['validation_folds']:
        train=np.flatnonzero(a['days']<fold['fit_before']);valid=np.flatnonzero((a['days']>=fold['start'])&(a['days']<fold['end']))
        if not len(train) or not len(valid) or max(a['days'][train])>=min(a['days'][valid]):raise ValueError('Invalid chronological split.')
        model=fit(a['clubs'][train],a['signal'][train],a['y'][train],penalty=plan['learner']['penalty'],bounds=plan['learner']['bounds'])
        write(out/(fold['name']+'_model.json'),model)
        base=a['clubs'][valid];p=predict(base,a['signal'][valid],model['beta']);y=a['y'][valid]
        zero=predict(base,a['signal'][valid],0.)
        if not np.array_equal(zero,base):raise ValueError('Null policy changed baseline.')
        m,b=metrics(y,p,base),metrics(y,base,base)
        passes=m['logloss']<b['logloss'] and m['correct']>=b['correct']
        results[fold['name']]=dict(model=model,train_matches=len(train),baseline=b,candidate=m,passes=passes,
            paired=paired(y,p,base,a['days'][valid],fold),
            by_league={lid:dict(baseline=metrics(y[a['leagues'][valid]==lid],base[a['leagues'][valid]==lid]),
                               candidate=metrics(y[a['leagues'][valid]==lid],p[a['leagues'][valid]==lid])) for lid in sorted(set(a['leagues'][valid]),key=int)})
        np.savez_compressed(out/(fold['name']+'_predictions.npz'),train_ids=a['ids'][train],ids=a['ids'][valid],days=a['days'][valid],
            y=y,signal=a['signal'][valid],clubs=base,candidate=p)
        all_y.extend(y);all_p.append(p);all_base.append(base)
    passes=all(value['passes'] for value in results.values())
    decision=dict(status='historical_pass_requires_prospective' if passes else 'rejected_branch_closed',
        selected_policy='candidate_pending_prospective' if passes else 'clubs',
        aggregate_xg_profile_branch='pending_prospective' if passes else 'closed',
        production_changed=False,reason='Everyfoldpassedthefrozenrule.' if passes else 'Atleastonefoldfailedthefrozenrule.Noadditionalvariantauthorizedbythisprotocol.')
    write(out/'decision.json',decision)
    p=np.vstack(all_p);base=np.vstack(all_base);y=np.asarray(all_y)
    report=dict(status='exploratory_single_xg_tilt',created_at_utc=datetime.now(timezone.utc).isoformat(),folds=results,decision=decision,
        combined=dict(baseline=metrics(y,base,base),candidate=metrics(y,p,base)),no2026evaluated=True)
    write(out/'results.json',report);verify(directory);return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=('prepare','run'));p.add_argument('--directory',type=Path,required=True)
    args=p.parse_args();print(json.dumps(prepare(args.directory) if args.action=='prepare' else run(args.directory),ensure_ascii=False,allow_nan=False))
