"""Verify forward proposals only in memory / a disposable manual archive.

Production archives are inspected read-only. No API request or training occurs.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
sys.path.insert(0, '/opt/patternlab')

import adaptive_scenario_proposals as policy
import prospective_models as models
import manual_analysis_store as manual
import manual_proposal_archive
import scenario_tables as tables
import scenario_outcomes as outcomes
import bet_simulation as simulation
from streamlit.testing.v1 import AppTest


def make_fixtures(size, now):
    kickoff = (now+timedelta(days=1)).replace(hour=19, minute=0, second=0, microsecond=0, tzinfo=None)
    return [dict(match_id=str(900000000+i), league='39', provider='api-football',
        home=f'Verification Home {i}', away=f'Verification Away {i}',
        home_id=str(100+i), away_id=str(200+i), timestamp=kickoff.isoformat()) for i in range(size)]


def make_method(size):
    distributions = [dict(zip(models.VALUES, (.51,.28,.21))) for _ in range(size)]
    value = models._method('isolated-computed-verification-v1',
        models._distribution('independent',size,distributions=distributions), {})
    value['ranking'] = models._rank(models._read_distribution(value['distribution']), 4)
    return value


def exercise():
    now = datetime.now(timezone.utc)
    for size in range(2, 21):
        fixtures = make_fixtures(size, now)
        methods = {'clubs':make_method(size)}
        saved = policy.capture(methods, fixtures, now=now, artifact_path='/nonexistent/calibration.json')
        policy.validate(saved, methods, fixtures, now.isoformat())
        assert saved['methods']['clubs']['ranking'] == methods['clubs']['ranking']
        assert saved['unavailable'] == {}
    fixtures = make_fixtures(2, now)
    selected_method = make_method(2)
    core = {'clubs':deepcopy(selected_method), 'frequency':deepcopy(selected_method), 'combined':deepcopy(selected_method)}
    selected = policy.capture(core, fixtures, now=now, artifact={})
    payload = dict(engine='statistical', source='API-Football', fixtures=fixtures,
        forecast=dict(eligible=True, size=2, league='39', before=now.date().isoformat(),
            ranking=deepcopy(selected_method['ranking'][:3])),
        comparison_methods=core, proposal_selection=selected,
        history_cutoff_exclusive=now.date().isoformat())
    with TemporaryDirectory(prefix='computed-proposals-verification-') as temporary:
        path = Path(temporary)/'manual.sqlite3'
        saved = manual.record(payload, path=path, clock=lambda:now, require_complete=True)
        reread = manual.get(saved['id'], path)
        assert reread['proposal_selection'] == selected
        assert {name:len(method['ranking']) for name,method in manual_proposal_archive.methods(reread).items()} == dict(clubs=4,frequency=4,combined=4)
        actual = selected_method['ranking'][3]['sequence']
        entry = dict(id='isolated',source='API-Football',created_at=now.isoformat(),
            size=2,fixtures=fixtures,methods=core,proposal_selection=selected,status='settled',actual=actual)
        exported = json.loads(json.dumps(entry))
        with patch.object(policy,'load_artifact',side_effect=AssertionError('No calibration lookup on read')):
            effective = policy.effective_methods(exported)
            assert outcomes.summarize(exported)['state'] == 'hit'
            expected = {tuple(row['sequence']) for row in selected_method['ranking']}
            assert {row['sequence'] for row in tables.build_matrix(effective,actual=actual)['rows']} == expected
            ledger = simulation.build([exported],now=now+timedelta(days=2))
            assert ledger['summary']['bets'] == 4 and ledger['summary']['won'] == 1
        source = ('import json\nimport adaptive_scenario_proposals as p\nimport scenario_tables as t\n'
            f'entry=json.loads({json.dumps(json.dumps(entry))})\n'
            't.render_archive_scope(entry)\nt.render_matrix(p.effective_methods(entry),actual=entry["actual"],key="verify")\n')
        app = AppTest.from_string(source,default_timeout=20).run()
        assert not app.exception and not app.warning
        assert app.dataframe[0].value['Propositions enregistrées'].tolist() == [4,4,4]
    return dict(sizes_verified=list(range(2,21)), fourth_winner_recognized=True,
        manual_fourth_choice_persisted=True, identical_in_export_table_verdict_simulation=True,
        native_ui_checked=True, inputs_and_outcomes_are_synthetic=True, production_database_writes=0)


def archives():
    result = {}
    for filename,table in (('predictions.sqlite3','predictions'),('manual_analyses.sqlite3','manual_analyses')):
        target = Path('/opt/patternlab/data/api_football')/filename
        with sqlite3.connect(target.resolve().as_uri()+'?mode=ro',uri=True,timeout=30) as connection:
            result[filename] = dict(rows=connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0],
                policies=dict(connection.execute(f"SELECT COALESCE(json_extract(payload,'$.proposal_selection.schema'),'legacy'),count(*) FROM {table} GROUP BY 1").fetchall()))
    return result


if __name__ == '__main__':
    assert policy.is_enabled()
    print(json.dumps(dict(state='verified',verified_at=datetime.now(timezone.utc).isoformat(),
        policy=policy.POLICY_VERSION,isolated=exercise(),production_archives=archives(),
        production_database_writes=0),ensure_ascii=False))
