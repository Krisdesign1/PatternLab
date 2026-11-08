"""Frozen, isolated comparison of scores versus half-time shot/xG histories.

Preparation never fits or scores. Evaluation requires fully resolved collection
and an explicit command. No serving artifacts, journal or source DB are written.
"""
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import gzip
import json
from pathlib import Path

import numpy as np

from halftime_research_dataset import extract, write_snapshot, load_snapshot
from halftime_research_features import generate, CORE_COLUMNS, RICH_COLUMNS, LABELS, LEAGUES, VERSION

ROOT=Path(__file__).resolve().parent
CUTOFF='2026-10-26T00:00:00+00:00'
TREE_PARAMETERS=dict(depth=3,iterations=200,l2_leaf_reg=300.,learning_rate=.03,
    loss_function='MultiClass',class_names=list(LABELS),random_seed=20261026,
    thread_count=2,has_time=True,boost_from_average=False,allow_writing_files=False,verbose=False)


def write_new(path,value):
    with Path(path).open('x',encoding='utf-8') as handle:json.dump(value,handle,ensure_ascii=False,indent=2,allow_nan=False)


def protocol():
    prior=json.loads((ROOT/'data/research/calendar_nonlinear/2026-10-26-v1/plan.json').read_text())
    return dict(client_date='2026-10-26',created_at_utc=datetime.now(timezone.utc).isoformat(),
        purpose='Does previous-match half-time shot/xG information improve individual first/equal/second class probabilities?',
        target='Compare regulation total goals first half vs second half. No AET goals or shootout.',
        primary='Enriched score+statistics head versus score-only head; identical fixed learner and chronological cohorts.',
        references=['Causal league Laplace frequency', 'Exact fixed Clubs-v2 law reconstructed incrementally on the same conservative score history'],
        leagues=list(LEAGUES),api_seasons=[2024,2025,2026],cutoff_exclusive_utc=CUTOFF,
        universe='All valid FT fixtures from the21 whole-season discovery manifests; never only successfully enriched fixtures. Current-target own statistics do not select targets.',
        warmup='All earlier valid local regulation score rows in the same seven leagues, including seasons2022/2023 when present; discovery fills missing2024-2026 scores.',
        identity='Exact API-Football providerID within competition, overall and home/away-role histories. No name embeddings or cross-league transfer in this pilot.',
        features=dict(version=VERSION,core_columns=list(CORE_COLUMNS),rich_columns=list(RICH_COLUMNS),
            windows='Last10 ALL previous matches; recent3 difference and explicit recent3 support, never last10 successfully retrieved records.',
            missing='Numeric placeholder0 only accompanied by counts and missing flags. Real0 retains positive support. No target own statistics feature.',
            metrics=['Total Shots','Shots on Goal','Shots insidebox','expected_goals'],
            context='for/against by first/second period, overall and role profiles; score-form, league-frequency, calendar metadata'),
        availability=dict(primary='retrospective_latest',
            day_policy='All targets of aUTCday emitted before current responses update history; previous fixture kickoff+6h must be <= targetUTCday start.',
            caveat='Six hours is a conservative availability assumption, not verified provider publication time. This changes late-prior-day availability versus servingClubs; compare formula on same filtered inputs.',
            diagnostic='stats_as_observed also requires statistics collected timestamp < each targetUTCday start; score historical as-of availability is still not proven.'),
        partitions=dict(fit='2024calendar targets',validation='2025calendar targets; choose fixedhead vs no correction using logloss only',
            refit='2024+2025calendar targets with selected fixed policy',historical_evaluation='2026-01-01 <= actualUTCkickoff <2026-10-26; exploratory because this period has already been investigated',
            prospective='Only newly recorded predictions after model selection and artifact freezing can confirm independent future benefit; no production activation in this experiment.'),
        learner=dict(parameters=TREE_PARAMETERS,baseline='float32logClubs probabilities in CatBoostPool, exactly once in both fitting and prediction',
            null_policy='Each head may choose exactClubs/no correction. Exact validationties prefer no correction. Publish both fixedhead and selected policy.',
            comparison='Four models: frequency,Clubs,score-only head,rich head. Fixedhead rich-vs-score-only is primary attribution; selected-policy comparison is separate.',
            no_class_balance=True,no_hyperparameter_grid=True,no_early_stopping=True),
        readiness=dict(resolved_response_fraction_per_calendar_partition=1.,
            resolved='Availability ready/partial/empty/invalid/unknown_shape counts as known; requesterrors/pending do not. It is collection completeness, not rich availability.',
            min_examples_per_partition=300,min_both_clubs_atleast5_rich_per_partition=100,all_three_classes_in_fit_and_validation=True,
            purpose='Technical feasibility only, not evidence of power or a70% target.',
            failure='Do not fit, score, promote or tune when gate is not met; publish missingness/support only. No override switch.'),
        metrics=['Primary paired mean logarithmic loss difference', 'Brier multiclass', 'argmax accuracy',
                 'per-class reliability in fixed decile bins', 'fixed confidence>=.50/.60/.70 with coverage and counts',
                 'reference accuracy on the exact same selected fixtures', 'per-league and fixed both-clubs>=5rich-history sensitivity'],
        inference='Descriptive paired ISOweek bootstrap2000 draws, seed20261026; require20closed weeks for interval. Does not remove all team dependence, repeated search, selection or multiple comparisons.',
        forbidden=['No threshold chosen on final historical block', 'No70%promise', 'No target own stats',
                   'No serving model writes', 'No saved forecast/journal edits','No data-source replacement or Cloud imports'],
        future_collection='Continue complete historical queue; preparation snapshots are immutable and do not alter order or selection of downloads.',
        frozen_model_code=prior['frozen_code_hashes'],
        code_sha256={name:sha256((ROOT/name).read_bytes()).hexdigest() for name in
            ('halftime_research_features.py','halftime_research_dataset.py','halftime_research_experiment.py','halftime_research_models.py','halftime_statistics_parser.py')})


def readiness(coverage,examples):
    counts=Counter('fit' if e['day']<'2025-01-01' else 'validation' if e['day']<'2026-01-01' else 'historical' for e in examples)
    reasons=[]
    for year in ('2024','2025','2026'):
        block=coverage.get(year,{})
        if not block.get('targets') or block.get('observed',0)!=block.get('targets'):
            reasons.append('Incomplete collection in calendar '+year)
    for part in ('fit','validation','historical'):
        if counts[part]<300:reasons.append('Fewer than300 examples in '+part)
        supported=sum(min(e['support']['home_all'],e['support']['away_all'])>=5 for e in examples
                      if ('fit' if e['day']<'2025-01-01' else 'validation' if e['day']<'2026-01-01' else 'historical')==part)
        if supported<100:reasons.append('Fewer than100 both-clubs-rich-supported examples in '+part)
    for part in ('fit','validation'):
        labels={e['actual'] for e in examples if ('fit' if e['day']<'2025-01-01' else 'validation' if e['day']<'2026-01-01' else 'historical')==part}
        if labels!=set(LABELS):reasons.append('Missing class in '+part)
    return dict(ready=not reasons,reasons=reasons,examples=dict(counts),coverage=coverage)


def prepare(directory,*,fixtures_path=None,statistics_path=None):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=False)
    plan=protocol();write_new(directory/'plan.json',plan)
    cutoff=datetime.fromisoformat(plan['cutoff_exclusive_utc'])
    data=extract(fixtures_path or ROOT/'data/api_football/fixtures.sqlite3',
                 statistics_path or ROOT/'data/api_football/halftime_statistics.sqlite3',cutoff=cutoff)
    if data['source']['discovery_manifests']!=21:raise ValueError('Expected exactly21whole-season manifests.')
    snapshot=directory/'snapshot';write_snapshot(snapshot,data)
    universe={r['match_id'] for r in data['universe']}
    examples=[];strict_nonzero=0
    with gzip.open(directory/'profiles.ndjson.gz','xt',encoding='utf-8') as handle:
        for example in generate(data['rows'],data['statistics']):
            if example['match_id'] in universe and example['eligible']:
                examples.append(example)
                handle.write(json.dumps(example,ensure_ascii=False,allow_nan=False,separators=(',',':'))+'\n')
    for example in generate(data['rows'],data['statistics'],mode='stats_as_observed'):
        if example['match_id'] in universe and any(example['support'].values()):strict_nonzero+=1
    arrays=dict(ids=np.asarray([r['match_id'] for r in examples]),days=np.asarray([r['day'] for r in examples]),
        leagues=np.asarray([r['league'] for r in examples]),y=np.asarray([LABELS.index(r['actual']) for r in examples]),
        core=np.asarray([[r['core'][k] for k in CORE_COLUMNS] for r in examples]),
        rich=np.asarray([[r['rich'][k] for k in RICH_COLUMNS] for r in examples]),
        frequency=np.asarray([r['frequency'] for r in examples]),clubs=np.asarray([r['clubs'] for r in examples]),
        support=np.asarray([[r['support'][k] for k in ('home_all','away_all')] for r in examples]))
    np.savez_compressed(directory/'dataset.npz',**arrays)
    report=readiness(data['coverage'],examples)
    report.update(status='prepared' if report['ready'] else 'waiting_for_collection',
        fixtures_in_score_history=len(data['rows']),statistics_observations=len(data['statistics']),
        feature_dimensions=dict(core=len(CORE_COLUMNS),rich_additional=len(RICH_COLUMNS)),
        strict_statistics_time_targets_with_support=strict_nonzero,
        support_by_partition={},performance_computed=False)
    for part,predicate in (('fit',lambda d:d<'2025-01-01'),('validation',lambda d:'2025-01-01'<=d<'2026-01-01'),('historical',lambda d:d>='2026-01-01')):
        selected=[r for r in examples if predicate(r['day'])]
        report['support_by_partition'][part]=dict(targets=len(selected),
            both_clubs_atleast5=sum(min(r['support']['home_all'],r['support']['away_all'])>=5 for r in selected),
            at_least_one_club_with_any_rich_history=sum(max(r['support']['home_all'],r['support']['away_all'])>0 for r in selected))
    write_new(directory/'readiness.json',report)
    verify_code(plan)
    write_new(directory/'preparation_hashes.json',{p.name:sha256(p.read_bytes()).hexdigest()
        for p in directory.iterdir() if p.is_file()})
    return report


def verify_code(plan):
    for name,digest in {**plan['frozen_model_code'],**plan['code_sha256']}.items():
        if sha256((ROOT/name).read_bytes()).hexdigest()!=digest:raise ValueError('Frozen code changed: '+name)


def evaluate(directory):
    """Only call after data are complete; never used implicitly by prepare."""
    directory=Path(directory);plan=json.loads((directory/'plan.json').read_text());verify_code(plan)
    for name,digest in json.loads((directory/'preparation_hashes.json').read_text()).items():
        if sha256((directory/name).read_bytes()).hexdigest()!=digest:raise ValueError('Preparation artifact hash mismatch.')
    load_snapshot(directory/'snapshot')
    report=json.loads((directory/'readiness.json').read_text())
    if not report['ready']:raise ValueError('Collection incomplete: evaluation refused; create a new complete snapshot later.')
    # The separate model/evaluation module is imported only after the collection gate.
    from halftime_research_models import evaluate_complete_snapshot
    return evaluate_complete_snapshot(directory,plan)
