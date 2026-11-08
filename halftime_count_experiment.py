"""Isolated chronological research: learn half goal counts from prematch profiles.

Never imports the application or credentials, modifies serving models, or fetches
data. Existing immutable enrichment snapshot is the only source.
"""
from datetime import datetime, timezone
from hashlib import sha256
import argparse
import gzip
import json
from pathlib import Path

import numpy as np

ROOT=Path(__file__).resolve().parent
SOURCE=ROOT/'data/research/halftime_enrichment/2026-10-26-complet-v1'
EXCLUDED_XG=('1208600','1213857','1351043')
PROFILES=('home_all','home_home','away_all','away_away')
CORE=tuple(p+'_'+m+'_mean' for p in PROFILES for m in ('ht_for','ht_against','sh_for','sh_against'))+tuple(
    p+'_'+m for p in PROFILES for m in ('last10_count','days_rest'))
RICH=tuple(p+'_'+half+'_'+metric+'_'+side+'_mean'
    for p in PROFILES for half in ('first','second') for metric in ('xg','on_goal') for side in ('for','against'))+tuple(
    p+'_'+half+'_'+metric+'_for_count' for p in PROFILES for half in ('first','second') for metric in ('xg','on_goal'))
CANDIDATES=('scores_poisson','rich_poisson','scores_compound','rich_compound')


def read(path):
    if str(path).endswith('.gz'):
        with gzip.open(path,'rt',encoding='utf-8') as f:return json.load(f)
    return json.loads(Path(path).read_text())


def write(path,value):
    with Path(path).open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2,allow_nan=False)


def clean_rich(profile,rows,statistics):
    """Remove known inconsistent xG records, preserving last10 history positions."""
    result=dict(profile['rich'])
    for prefix,ids in profile['history_ids'].items():
        # Recompute xG only; other metrics retain the verified frozen profiles.
        for half in ('first','second'):
            for perspective in ('for','against'):
                values=[]
                for mid in ids:
                    record=rows[mid];item=statistics.get(mid)
                    pair=(item or {}).get('periods',{}).get(half,{}).get('expected_goals')
                    if mid in EXCLUDED_XG or pair is None:
                        values.append(None);continue
                    is_home=prefix.startswith('home_')
                    target=profile['_home_id'] if is_home else profile['_away_id']
                    side=0 if str(record['home_id'])==target else 1
                    if str(record['away_id' if side else 'home_id'])!=target:raise ValueError('History identity mismatch.')
                    values.append(float(pair[side if perspective=='for' else 1-side]))
                known=[v for v in values if v is not None]
                base=prefix+'_'+half+'_xg_'+perspective+'_'
                result[base+'mean']=sum(known)/len(known) if known else 0.
                result[base+'count']=len(known)
    return result


def features(profile,rows,statistics):
    source=rows[profile['match_id']]
    profile=dict(profile,_home_id=str(source['home_id']),_away_id=str(source['away_id']))
    rich=clean_rich(profile,rows,statistics)
    core=[]
    for key in CORE:
        value=float(profile['core'][key])
        if key.endswith('days_rest'):value=min(max(value,0.),60.)
        core.append(float(np.log1p(value)))
    extra=[float(np.log1p(float(rich[key]))) for key in RICH]
    return core,extra


def prepare(directory):
    from halftime_research_experiment import verify_code
    from halftime_research_dataset import load_snapshot
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=False)
    old=read(SOURCE/'plan.json');verify_code(old)
    for name,digest in read(SOURCE/'preparation_hashes.json').items():
        if sha256((SOURCE/name).read_bytes()).hexdigest()!=digest:raise ValueError('Source artifact changed.')
    load_snapshot(SOURCE/'snapshot')
    if not read(SOURCE/'readiness.json')['ready']:raise ValueError('Source incomplete.')
    plan=dict(created_at_utc=datetime.now(timezone.utc).isoformat(),client_date='2026-10-27',
        hypothesis='Predict goal counts rather than class labels using prior-match xG/shots; integrate unknown total and half allocation.',
        novelty_scope='Count processes themselves and score-only state models already tested. New experiment changes learning target and uses newly collected rich histories.',
        source=str(SOURCE.relative_to(ROOT)),source_preparation_sha256=sha256((SOURCE/'preparation_hashes.json').read_bytes()).hexdigest(),
        features=dict(scores=list(CORE),rich_additional=list(RICH),transform='log1p; rest clipped0..60days; model standardizes using training only',
            excluded_xg_fixture_ids=list(EXCLUDED_XG),cleaning='Only known FT/H1/H2-inconsistent xG observations excluded; last10 membership unchanged. Other statistics preserved.'),
        targets='Observed first/second regulation goal counts, derived from HT/FT; current-target statistics never feature.',
        learner=dict(family='Two ridge Poisson GLMs; numeric coefficients penalty30, weak league-intercept prior1; no class balancing',penalty=30.,
            dispersion='train-only NB total concentration in[1,1e6]; allocation power-binomial phi in[.25,4]. phi1 binomial; phi>1 sharper; nominal p is not guaranteed mean after power.',
            probability_order=['1st','Égal','2nd'],no_parameter_grid=True),
        validation_folds=[dict(name='2025_H1',fit_before='2025-01-01',start='2025-01-01',end='2025-07-01'),
                          dict(name='2025_H2',fit_before='2025-07-01',start='2025-07-01',end='2026-01-01')],
        candidates=list(CANDIDATES),
        selection='Candidate must beat causal Clubs logloss in each2025half; choose lowest combined validation logloss among eligible, otherwiseClubs. Exact ties preferClubs. Write choice before2026 evaluation.',
        refit='All eligible2024+2025 targets, then fixed2026 evaluation on same2075 IDs as prior experiment.',
        metrics=['Class logloss/Brier/accuracy','Four count targets reduced to total first/second; count error','Fixed.5/.6/.7 coverage/accuracy/reference-on-same-subset','Perleague','Paired weekly bootstrap for rich-vs-scores samefamily'],
        exploration='All historical periods already examined. No independent confirmation, no70%promise, no production activation.',
        sources=['https://arxiv.org/abs/2103.07272','https://pmc.ncbi.nlm.nih.gov/articles/PMC4157759/'],
        frozen_prior_code={**old['frozen_model_code'],**old['code_sha256']},
        code_sha256={name:sha256((ROOT/name).read_bytes()).hexdigest() for name in ('halftime_count_experiment.py','halftime_count_models.py')})
    write(directory/'plan.json',plan) # Persist choices before target/feature extraction or fitting.
    rows={r['match_id']:r for r in read(SOURCE/'snapshot/rows.json.gz')}
    statistics=read(SOURCE/'snapshot/statistics.json.gz')
    with np.load(SOURCE/'dataset.npz',allow_pickle=False) as f:arrays={k:f[k] for k in ('ids','days','leagues','y','clubs','frequency')}
    core=[];extra=[];goals=[]
    with gzip.open(SOURCE/'profiles.ndjson.gz','rt',encoding='utf-8') as f:
        profiles={p['match_id']:p for p in map(json.loads,f)}
    for mid in arrays['ids']:
        c,e=features(profiles[str(mid)],rows,statistics);core.append(c);extra.append(e)
        row=rows[str(mid)];first=sum(map(int,row['ht'].split('-')));total=sum(map(int,row['ft'].split('-')))
        goals.append((first,total-first))
    arrays.update(scores=np.asarray(core),rich=np.column_stack((core,extra)),goals=np.asarray(goals,dtype=int))
    np.savez_compressed(directory/'dataset.npz',**arrays)
    write(directory/'preparation.json',dict(examples=len(core),score_features=len(CORE),rich_features=len(CORE)+len(RICH),
        counts_by_year={y:int(np.char.startswith(arrays['days'],y).sum()) for y in ('2024','2025','2026')},
        data_sha256=sha256((directory/'dataset.npz').read_bytes()).hexdigest(),
        plan_sha256=sha256((directory/'plan.json').read_bytes()).hexdigest()))
    return read(directory/'preparation.json')


def verify(directory):
    plan=read(directory/'plan.json');prepared=read(directory/'preparation.json')
    for name,digest in {**plan['frozen_prior_code'],**plan['code_sha256']}.items():
        if sha256((ROOT/name).read_bytes()).hexdigest()!=digest:raise ValueError('Code changed '+name)
    if sha256((directory/'dataset.npz').read_bytes()).hexdigest()!=prepared['data_sha256']:raise ValueError('Data changed.')
    if sha256((directory/'plan.json').read_bytes()).hexdigest()!=prepared['plan_sha256']:raise ValueError('Plan changed.')
    return plan


def run(directory):
    from halftime_count_models import fit_count,predict_means,fit_dispersion,class_probabilities
    from halftime_research_models import metrics,compare
    directory=Path(directory);plan=verify(directory)
    out=directory/'evaluation';out.mkdir(exist_ok=False)
    with np.load(directory/'dataset.npz',allow_pickle=False) as f:a={k:f[k] for k in f.files}
    days=a['days'];validation={};combined={name:[] for name in ('clubs',*CANDIDATES)};val_ids=[];val_y=[]
    for fold in plan['validation_folds']:
        train=np.flatnonzero(days<fold['fit_before']);valid=np.flatnonzero((days>=fold['start'])&(days<fold['end']))
        predictions={'clubs':a['clubs'][valid]};means_saved={}
        for family in ('scores','rich'):
            x=a[family];model=fit_count(x[train],a['goals'][train],a['leagues'][train],penalty=plan['learner']['penalty'])
            train_means=predict_means(model,x[train],a['leagues'][train]);means=predict_means(model,x[valid],a['leagues'][valid])
            dispersion=fit_dispersion(a['goals'][train],train_means)
            write(out/(fold['name']+'_'+family+'.json'),dict(model=model,dispersion=dispersion))
            means_saved[family]=means
            for shape in ('poisson','compound'):
                predictions[family+'_'+shape]=class_probabilities(means,shape=shape,dispersion=dispersion)
        validation[fold['name']]={name:metrics(a['y'][valid],p,a['clubs'][valid]) for name,p in predictions.items()}
        for name,p in predictions.items():combined[name].append(p)
        val_ids.extend(a['ids'][valid]);val_y.extend(a['y'][valid])
        np.savez_compressed(out/(fold['name']+'_predictions.npz'),ids=a['ids'][valid],y=a['y'][valid],**predictions,**{k+'_means':v for k,v in means_saved.items()})
    combined={name:np.vstack(blocks) for name,blocks in combined.items()}
    val_metrics={name:metrics(np.asarray(val_y),p,combined['clubs']) for name,p in combined.items()}
    eligible=[name for name in CANDIDATES if all(v[name]['logloss']<v['clubs']['logloss'] for v in validation.values())]
    winner=min(eligible,key=lambda n:val_metrics[n]['logloss']) if eligible else 'clubs'
    write(out/'validation_selection.json',dict(by_fold=validation,combined=val_metrics,eligible=eligible,winner=winner))
    train=np.flatnonzero(days<'2026-01-01');test=np.flatnonzero(days>='2026-01-01')
    predictions={'clubs':a['clubs'][test],'frequency':a['frequency'][test]};means_saved={}
    for family in ('scores','rich'):
        x=a[family];model=fit_count(x[train],a['goals'][train],a['leagues'][train],penalty=plan['learner']['penalty'])
        means=predict_means(model,x[test],a['leagues'][test]);dispersion=fit_dispersion(a['goals'][train],predict_means(model,x[train],a['leagues'][train]))
        write(out/('final_'+family+'.json'),dict(model=model,dispersion=dispersion));means_saved[family]=means
        for shape in ('poisson','compound'):
            predictions[family+'_'+shape]=class_probabilities(means,shape=shape,dispersion=dispersion)
    predictions['selected_policy']=predictions[winner]
    summary={name:metrics(a['y'][test],p,a['clubs'][test]) for name,p in predictions.items()}
    comparisons={shape:compare(a['y'][test],predictions['rich_'+shape],predictions['scores_'+shape],days[test]) for shape in ('poisson','compound')}
    comparison_selected=compare(a['y'][test],predictions['selected_policy'],predictions['clubs'],days[test])
    by_league={}
    for lid in sorted(set(a['leagues'][test].tolist()),key=int):
        mask=a['leagues'][test]==lid
        by_league[lid]={name:metrics(a['y'][test][mask],p[mask],a['clubs'][test][mask]) for name,p in predictions.items()}
    np.savez_compressed(out/'predictions.npz',ids=a['ids'][test],days=days[test],y=a['y'][test],goals=a['goals'][test],
        leagues=a['leagues'][test],**predictions,**{k+'_means':v for k,v in means_saved.items()})
    result=dict(status='exploratory_historical_counts',created_at_utc=datetime.now(timezone.utc).isoformat(),
        selection=winner,validation=val_metrics,metrics=summary,comparisons=comparisons,selected_vs_clubs=comparison_selected,
        count_errors={name:dict(mean_predicted=means.mean(0).tolist(),mean_observed=a['goals'][test].mean(0).tolist(),
            mae=np.abs(means-a['goals'][test]).mean(0).tolist()) for name,means in means_saved.items()},
        by_league=by_league,production_changed=False,independent_confirmation=False)
    write(out/'results.json',result);verify(directory)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('prepare','run'));parser.add_argument('--directory',type=Path,required=True)
    args=parser.parse_args()
    value=prepare(args.directory) if args.action=='prepare' else run(args.directory)
    print(json.dumps(value,ensure_ascii=False,allow_nan=False))
