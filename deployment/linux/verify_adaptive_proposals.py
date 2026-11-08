"""Read real archives; exercise new paths only inside an isolated temporary DB."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
sys.path.insert(0,'/opt/patternlab')

import adaptive_scenario_proposals as policy
import adaptive_scenario_calibration as calibration
import prospective_models as models
import prediction_journal as journal
import scenario_tables as tables
import scenario_outcomes as outcomes
import bet_simulation as simulation
from streamlit.testing.v1 import AppTest


def exercise():
    now=datetime.now(timezone.utc)
    # Synthetic input is deliberately separate from all provider/source data.
    fixtures=[dict(match_id=str(900000000+index),league='39',provider='api-football',
        home=f'Verification Home {index}',away=f'Verification Away {index}',
        home_id=str(100+index),away_id=str(200+index),season=2026,
        timestamp=(now+timedelta(hours=4,minutes=index)).replace(tzinfo=None,microsecond=0).isoformat())
        for index in range(2)]
    positions=[dict(zip(models.VALUES,p)) for p in ((.51,.28,.21),(.46,.31,.23))]
    before=now.date().isoformat()
    core={'clubs':models._method('isolated-verification-v1',
        models._distribution('independent',2,distributions=positions),
        dict(source='API-Football',league='39',training_before=before))}
    models._bind_methods(dict(methods=core),fixtures,before,decision_at=now)
    metrics=dict(groups=30,hits=15,proposals=90,precision=1/6,f05=18.75/97.5)
    evidence=dict(status='qualified',threshold=.11,tuning_groups=40,test_groups=30,
        tuning_days=4,test_days=3,tuning=metrics,test=metrics,reference_top3=metrics,
        test_first_day=(now-timedelta(days=4)).date().isoformat(),
        tuning_labels_before=(now-timedelta(days=4)).isoformat())
    artifact=dict(schema=calibration.VERSION,created_at=(now-timedelta(hours=1)).isoformat(),
        contexts={calibration.context_key('clubs',core['clubs'],2):evidence})
    artifact['sha256']=calibration.digest(artifact)
    raw=[dict(fixture=dict(id=int(f['match_id']),date=f['timestamp']+'Z',status={'short':'NS'}),
        league={'id':39,'season':2026,'name':'Isolated verification'},teams=dict(home=dict(id=int(f['home_id']),name=f['home']),
        away=dict(id=int(f['away_id']),name=f['away'])),
        score=dict(halftime=dict(home=None,away=None),fulltime=dict(home=None,away=None))) for f in fixtures]
    with TemporaryDirectory(prefix='adaptive-proposals-verification-') as temporary:
        path=Path(temporary)/'predictions.sqlite3'
        with patch.object(policy,'is_enabled',return_value=True),patch.object(policy,'load_artifact',return_value=artifact):
            saved=journal.record(fixtures,core,now=now,path=path,raw_fixtures=raw)
        selected=policy.effective_methods(saved)['clubs']['ranking']
        assert len(selected)==4
        actual=selected[3]['sequence']
        for item,label in zip(raw,actual):
            ht,ft={'1st':((1,0),(1,0)),'2nd':((0,0),(1,0)),'Égal':((0,0),(0,0))}[label]
            item['fixture']['status']['short']='FT'
            item['score']=dict(halftime=dict(home=ht[0],away=ht[1]),fulltime=dict(home=ft[0],away=ft[1]))
        journal.reconcile(raw,now=now+timedelta(hours=8),path=path)
        reread=json.loads(json.dumps(journal.list_entries(path)[0]))
        assert reread['proposal_selection']==saved['proposal_selection']
        assert outcomes.summarize(reread)['state']=='hit', (reread['status'],reread.get('issues'),outcomes.summarize(reread))
        assert not reread['evaluations']['clubs']['top3']
        assert reread['evaluations']['clubs']['selected_proposals']['hit']
        expected={tuple(item['sequence']) for item in selected}
        assert {r['sequence'] for r in tables.build_matrix(policy.effective_methods(reread),actual=actual)['rows']}==expected
        ledger=simulation.build([reread],now=now+timedelta(hours=8))
        assert {tuple(ticket['sequence']) for ticket in ledger['tickets']}==expected
        assert ledger['summary']['won']==1
        source=('import json\nimport scenario_tables as t\nimport adaptive_scenario_proposals as p\n'
                f'entry=json.loads({json.dumps(json.dumps(reread))})\n'
                't.render_archive_scope(entry)\nt.render_matrix(p.effective_methods(entry),actual=entry["actual"],key="verification")\n')
        app=AppTest.from_string(source,default_timeout=20).run()
        assert not app.exception
        assert app.dataframe[0].value['Scénarios retenus'].tolist()==[4]
        assert any('sans nombre de scénarios imposé' in x.value for x in app.caption)
    return dict(complete_count=4,fourth_winner_recognized=True,reference_top3_unchanged=True,
                identical_in_storage_export_table_verdict_simulation=True,native_ui_checked=True,
                inputs_and_outcome_are_synthetic=True,production_database_writes=0)


def main():
    artifact=policy.load_artifact(); assert policy.is_enabled()
    qualified=[row for row in artifact['contexts'].values() if row['status']=='qualified']
    entries=journal.list_entries()
    examples=[]
    for entry in entries:
        if 'proposal_selection' in entry:continue
        if entry['size']>4:continue
        expected={tuple(item['sequence']) for payload in entry['methods'].values() for item in payload['ranking']}
        if len(expected)>3:
            assert {r['sequence'] for r in tables.build_matrix(entry['methods'])['rows']}==expected
            examples.append(dict(id=entry['id'],choices=len(expected)))
            if len(examples)>=5:break
    print(json.dumps(dict(state='verified',verified_at=datetime.now(timezone.utc).isoformat(),
        calibration_sha256=artifact['sha256'],qualified_contexts=len(qualified),
        qualified_sizes=sorted({row['size'] for row in qualified}),
        actual_legacy_examples=examples,actual_archive_entries=len(entries),
        isolated=exercise(),production_database_writes=0),ensure_ascii=False))


if __name__=='__main__':main()
