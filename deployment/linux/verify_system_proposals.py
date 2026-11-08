from copy import deepcopy
from datetime import datetime,timezone,timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import json,sqlite3,sys,time
sys.path.insert(0,'/opt/patternlab')
import prediction_journal as journal
import scenario_proposal_archive as proposals
import scenario_tables as tables
import scenario_outcomes as outcomes
import bet_simulation as simulation
from coupon_sizes import top_independent
import prospective_models as models
root=Path('/opt/patternlab/data/api_football')
with sqlite3.connect((root/'predictions.sqlite3').resolve().as_uri()+'?mode=ro',uri=True) as con:
    source=None; examples=[]
    for key,stamp,payload,digest in con.execute('SELECT id,created_at,payload,digest FROM predictions ORDER BY created_at DESC'):
        assert sha256(payload.encode()).hexdigest()==digest
        e=json.loads(payload)
        if e.get('size',99)>4: continue
        unique={tuple(r['sequence']) for m in e['methods'].values() for r in m['ranking']}
        if len(unique)>3:
            e.update(id=key,created_at=stamp,status='pending',actual=None,observed=[],evaluations={})
            matrix=tables.build_matrix(e['methods'])
            assert {r['sequence'] for r in matrix['rows']}==unique
            examples.append(dict(id=key,distinct=len(unique),methods=list(e['methods'])))
            if source is None: source=e
            if len(examples)>=10: break
assert source is not None
snapshot=deepcopy(source)
fixtures=source['fixtures']; current=journal._stamp(source['created_at'])
raw=[dict(fixture=dict(id=int(f['match_id']),date=f['timestamp']+'Z',status={'short':'NS'}),
    league={'id':int(f['league']),'season':f.get('season',2026)},teams=dict(
    home=dict(id=int(f['home_id']),name=f['home']),away=dict(id=int(f['away_id']),name=f['away'])),
    score=dict(halftime={'home':None,'away':None},fulltime={'home':None,'away':None})) for f in fixtures]
with TemporaryDirectory(prefix='system-proposals-verification-') as directory:
    path=Path(directory)/'predictions.sqlite3'
    record=journal.record_mixed if source['league']=='mixed' else journal.record
    entry=record(fixtures,source['methods'],now=current,path=path,raw_fixtures=raw,metadata=source['metadata'])
    info=proposals.validate_manifest(entry['methods'],entry['proposal_manifest'])
    expected={tuple(r['sequence']) for m in entry['methods'].values() for r in m['ranking']}
    exported=json.loads(json.dumps(journal.list_entries(path)[0]))
    assert expected=={tuple(r['sequence']) for m in exported['methods'].values() for r in m['ranking']}
    payload=json.loads(sqlite3.connect(path).execute('SELECT payload FROM predictions').fetchone()[0])
    assert payload['methods']==source['methods']
    actual=next(iter(sorted(expected-set(tuple(r['sequence']) for r in source['methods']['clubs']['ranking']))))
    settled=dict(exported,status='settled',actual=list(actual))
    assert outcomes.summarize(settled)['state']=='hit'
    assert tables.build_matrix(settled['methods'],actual=settled['actual'])['actual_in_choices']
    simulated_clock=max(datetime.now(timezone.utc), max(journal._stamp(f['timestamp']) for f in fixtures)+timedelta(hours=3))
    simulated=simulation.build([settled],now=simulated_clock)
    assert {tuple(t['sequence']) for t in simulated['tickets']}==expected
    assert simulated['summary']['won']==1
    with __import__('unittest.mock',fromlist=['patch']).patch.object(models,'_rank',side_effect=AssertionError('No model ranking on read')):
        start=time.perf_counter()
        for _ in range(20):proposals.validate_manifest(entry['methods'],entry['proposal_manifest'])
        elapsed=(time.perf_counter()-start)/20
positions=[{'1st':.51,'Égal':.28,'2nd':.21},{'1st':.46,'Égal':.31,'2nd':.23}]
method=dict(version='saved-test',ranking=top_independent(positions,5),distribution=dict(
    schema=models.DISTRIBUTION_VERSION,type='independent',size=2,distributions=positions))
methods={'clubs':method}; actual=method['ranking'][3]['sequence']
assert len(tables.build_matrix(methods,actual=actual)['rows'])==5
assert outcomes.summarize(dict(status='settled',size=2,actual=actual,methods=methods))['matches'][0]['rank']==4
assert source==snapshot
print(json.dumps(dict(state='verified',verified_at=datetime.now(timezone.utc).isoformat(),
    actual_archive_examples=examples,isolated_new_record_distinct=info['distinct_proposal_count'],
    isolated_new_record_saved_counts=info['proposal_counts'],all_saved_survive_export_table_verdict_simulation=True,isolated_simulation_clock=simulated_clock.isoformat(),isolated_outcome_is_synthetic=True,
    fourth_explicit_choice_checked=True,legacy_input_unchanged=True,production_database_writes=0,
    manifest_read_average_seconds=elapsed,read_model_calls=0),ensure_ascii=False))
