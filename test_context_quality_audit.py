"""Integration tests for read-only snapshots and receipt eligibility."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from context_quality_audit import canonical, digest, run_audit, validate_observation


def fixture(mid=99, *, status='FT', score=True, lineups=True):
    raw = dict(fixture=dict(id=mid, date='2026-11-07T18:00:00Z', status=dict(short=status)),
        league=dict(id=39, season=2026), teams=dict(home=dict(id=1), away=dict(id=2)),
        goals=dict(home=0 if score else None, away=0 if score else None),
        score=dict(halftime=dict(home=0 if score else None, away=0 if score else None),
                   fulltime=dict(home=0 if score else None, away=0 if score else None)),
        events=[])
    if lineups:
        raw['lineups'] = [dict(team=dict(id=team), coach=dict(id=team+100),
            startXI=[dict(player=dict(id=team*100+i)) for i in range(11)], substitutes=[])
            for team in (1, 2)]
    return raw


def observation(raw, at='2026-11-07T17:10:00Z', *, sequence=1, quarantine=False):
    record = dict(version='fixture-import-quality-v1', match_id=str(raw['fixture']['id']),
        observed_at_utc=at, time_kind='local_import_recorded', decision='received',
        reason=None, received_raw=raw, previous_observation=None,
        received_raw_sha256=digest(raw), selected_raw_sha256=digest(raw),
        selected_quality=dict(version='fixture-import-quality-v1', quarantined=True,
            previous_raw_sha256=digest(raw), received_raw_sha256=digest(raw)) if quarantine else None)
    record['id'] = digest(record)
    return (sequence, record['match_id'], at, canonical(record), record['id'])


def database(path, current, observations=()):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO metadata VALUES('revision','test-1');
        CREATE TABLE fixtures(ordinal INTEGER PRIMARY KEY,match_id TEXT UNIQUE,league TEXT,
            season INTEGER,status TEXT,kickoff_utc TEXT,issue TEXT,raw_json TEXT);
        CREATE TABLE fixture_import_revisions(sequence INTEGER, match_id TEXT,
            observed_at_utc TEXT,payload TEXT,digest TEXT);
    """)
    for i, raw in enumerate(current):
        con.execute('INSERT INTO fixtures VALUES(?,?,?,?,?,?,?,?)',
            (i+1,str(raw['fixture']['id']),'39',2026,raw['fixture']['status']['short'],
             '2026-11-07T18:00:00',None,canonical(raw)))
    con.executemany('INSERT INTO fixture_import_revisions VALUES(?,?,?,?,?)', observations)
    con.commit(); con.close()


class ReceiptValidationTests(unittest.TestCase):
    def test_record_and_index_hashes_are_checked(self):
        row = observation(fixture(status='NS', score=False))
        value, error = validate_observation(row)
        self.assertIsNone(error)
        self.assertEqual(value[1]['fixture']['id'], 99)
        for replacement in [
            (row[0], '100', *row[2:]),
            (*row[:2], '2026-11-07T17:11:00Z', *row[3:]),
            (*row[:4], 'wrong_digest'),
        ]:
            self.assertIsNotNone(validate_observation(replacement)[1])
        broken = json.loads(row[3]); broken['received_raw']['lineups'][0]['team']['id']=55
        self.assertEqual(validate_observation((*row[:3],canonical(broken),row[4]))[1], 'record_digest_mismatch')

    def test_rejected_received_version_and_quarantine_do_not_attest_prematch(self):
        row = observation(fixture(), quarantine=True)
        self.assertEqual(validate_observation(row)[1], 'quarantined_observation')
        record = json.loads(observation(fixture())[3])
        record['selected_raw_sha256'] = 'different'
        record['id'] = digest({k:v for k,v in record.items() if k!='id'})
        row = (1,'99',record['observed_at_utc'],canonical(record),record['id'])
        self.assertEqual(validate_observation(row)[1], 'received_version_not_retained')

    def test_unknown_or_naive_receipt_times_are_rejected(self):
        for value in ('2026-11-07T17:10:00', 'garbage', None):
            self.assertIsNotNone(validate_observation(observation(fixture(),at=value))[1])

    def test_quarantined_decision_blocks_even_without_selected_annotation(self):
        row=observation(fixture())
        record=json.loads(row[3]); record['decision']='quarantined'; record['selected_quality']=None
        record['id']=digest({k:v for k,v in record.items() if k!='id'})
        self.assertEqual(validate_observation((1,'99',row[2],canonical(record),record['id']))[1],
                         'quarantined_observation')

    def test_explicit_foreign_provider_is_rejected(self):
        raw=fixture(); raw['provider']='football-data'
        self.assertEqual(validate_observation(observation(raw))[1], 'foreign_provider')

    def test_malformed_quality_and_illegal_sequence_or_decision_rejected(self):
        row = observation(fixture())
        self.assertEqual(validate_observation((0,*row[1:]))[1], 'invalid_sequence')
        for field, value, reason in [('selected_quality', {'quarantined':False}, 'malformed_selected_quality'),
                                      ('decision','invented','invalid_import_decision')]:
            record=json.loads(row[3]); record[field]=value
            record['id']=digest({k:v for k,v in record.items() if k!='id'})
            self.assertEqual(validate_observation((1,'99',row[2],canonical(record),record['id']))[1], reason)


class ReadOnlyAuditTests(unittest.TestCase):
    def test_cli_emits_a_new_receipt_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'fixtures.sqlite3'; output=Path(folder)/'report.json'
            database(p,[fixture()])
            script=Path(__file__).resolve().parent/'scripts/audit_context_quality.py'
            args=[sys.executable,str(script),'--database',str(p),'--label','synthetic','--output',str(output)]
            before=p.read_bytes()
            first=subprocess.run(args,capture_output=True,text=True)
            self.assertEqual(first.returncode,0,first.stderr)
            receipt=output.read_bytes()
            self.assertEqual(json.loads(receipt)['fixture_count'],1)
            self.assertEqual(json.loads(receipt)['database_writes'],0)
            second=subprocess.run(args,capture_output=True,text=True)
            self.assertNotEqual(second.returncode,0)
            self.assertEqual(output.read_bytes(),receipt)
            self.assertEqual(p.read_bytes(),before)

    def test_snapshot_distinguishes_coverage_quality_and_h30_provenance(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'fixtures.sqlite3'
            current = [fixture(99),fixture(100),fixture(101,lineups=False)]
            old_99=fixture(99,status='NS',score=False)
            old_100=fixture(100,status='NS',score=False)
            # The first receipt is before H-30, the second after H-30.
            database(p,current,[observation(old_99),observation(old_100,at='2026-11-07T17:40:00Z',sequence=2)])
            before=p.read_bytes()
            result=run_audit(p,source_label='synthetic')
            self.assertEqual(before,p.read_bytes())
            self.assertEqual(result['fixture_count'],3)
            self.assertEqual(result['availability']['lineups'],{'present':2,'absent':1})
            self.assertEqual(result['availability']['injuries'],{'absent':3})
            self.assertEqual(result['lineups']['complete_starting_elevens'],2)
            self.assertEqual(result['lineups']['current_version_known_before_kickoff'],2)
            self.assertEqual(result['lineups']['current_version_known_by_h30'],1)
            self.assertEqual(result['revisions']['lineups_complete_NS_consistent_by_h30'],1)
            self.assertFalse(result['events']['completeness_of_all_events_proven'])
            self.assertEqual(result['database_writes'],0)

    def test_future_schedule_or_other_identity_never_rescues_a_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'fixtures.sqlite3'
            old=fixture(99,status='NS',score=False)
            old['fixture']['date']='2026-11-08T18:00:00Z'
            database(p,[fixture(99)],[observation(old)])
            result=run_audit(p,source_label='synthetic')
            self.assertEqual(result['revisions']['lineups_complete_NS_consistent_by_h30'],0)
            self.assertEqual(result['revisions']['ids_rejected_by_current_identity_schedule_or_quality'],1)

    def test_current_lineup_correction_does_not_backdate_replacement(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'fixtures.sqlite3'
            old=fixture(99,status='NS',score=False)
            current=fixture(99)
            current['lineups'][0]['startXI'][0]['player']['id']=99999
            database(p,[current],[observation(old)])
            result=run_audit(p,source_label='synthetic')
            self.assertEqual(result['revisions']['lineups_complete_NS_consistent_by_h30'],1)
            self.assertEqual(result['lineups'].get('current_version_known_by_h30',0),0)

    def test_exact_h30_is_not_earlier_than_decision_and_live_snapshot_is_not_prematch(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'fixtures.sqlite3'
            database(p,[fixture(99),fixture(100)],[
                observation(fixture(99,status='NS',score=False),at='2026-11-07T17:30:00Z'),
                observation(fixture(100,status='FT'),sequence=2)])
            result=run_audit(p,source_label='synthetic')
            self.assertEqual(result['revisions']['lineups_complete_NS_consistent_by_h30'],0)
            self.assertIn('target_status_not_NS', result['revisions']['cutoff_exclusion_counts']['by_h30'])

    def test_withdrawn_lineup_does_not_reappear_before_it_was_restored(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'fixtures.sqlite3'
            initial=fixture(status='NS',score=False)
            rows=[observation(initial,at='2026-11-07T17:00:00Z')]
            for sequence, at, value in [(2,'2026-11-07T17:10:00Z',[]),
                                        (3,'2026-11-07T17:40:00Z',initial['lineups'])]:
                raw=deepcopy(initial); raw['lineups']=value
                record=json.loads(observation(raw,at=at,sequence=sequence)[3])
                record['previous_observation']=rows[-1][4]
                record['id']=digest({k:v for k,v in record.items() if k!='id'})
                rows.append((sequence,'99',at,canonical(record),record['id']))
            database(p,[fixture()],rows)
            result=run_audit(p,source_label='synthetic')
            self.assertEqual(result['revisions']['lineups_complete_NS_consistent_by_h30'],0)
            self.assertEqual(result['revisions']['lineups_complete_NS_consistent_before_kickoff'],1)

    def test_disconnected_chain_is_not_a_valid_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'fixtures.sqlite3'
            record=json.loads(observation(fixture(status='NS',score=False))[3])
            record['previous_observation']='a'*64
            record['id']=digest({k:v for k,v in record.items() if k!='id'})
            database(p,[fixture()],[(1,'99',record['observed_at_utc'],canonical(record),record['id'])])
            result=run_audit(p,source_label='synthetic')
            self.assertEqual(result['revisions']['lineups_complete_NS_consistent_by_h30'],0)
            self.assertEqual(result['revisions']['issue_counts']['invalid_or_disconnected_observation_chain'],1)

    def test_late_corruption_does_not_erase_earlier_valid_state(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'fixtures.sqlite3'
            good=observation(fixture(status='NS',score=False),at='2026-11-07T17:00:00Z')
            bad=observation(fixture(status='NS',score=False),at='2026-11-07T17:40:00Z',sequence=2)
            database(p,[fixture()],[good,bad])  # bad has a disconnected previous link.
            result=run_audit(p,source_label='synthetic')
            self.assertEqual(result['revisions']['lineups_complete_NS_consistent_by_h30'],1)
            self.assertEqual(result['revisions']['lineups_complete_NS_consistent_before_kickoff'],0)

    def test_missing_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'missing.sqlite3'
            with self.assertRaises(FileNotFoundError):
                run_audit(p,source_label='synthetic')
            self.assertFalse(p.exists())

    def test_two_missing_kickoffs_are_not_a_valid_time_match(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'fixtures.sqlite3'
            raw=fixture();raw['fixture']['date']=None
            database(p,[raw])
            c=sqlite3.connect(p);c.execute('UPDATE fixtures SET kickoff_utc=NULL');c.commit();c.close()
            result=run_audit(p,source_label='synthetic')
            self.assertEqual(result['lineups']['complete_starting_elevens'],0)
            self.assertEqual(result['events']['goal_compatible'],0)
            self.assertEqual(result['anomalies'][0]['kind'],'raw_index_identity_schedule_or_status_mismatch')


if __name__ == '__main__':
    unittest.main()
