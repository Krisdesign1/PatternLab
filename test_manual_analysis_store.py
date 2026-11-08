"""Real SQLite archive boundaries and real API regulation-result projection."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import manual_analysis_store as archive


NOW = datetime(2026, 11, 6, 9, tzinfo=timezone.utc)


def payload():
    return dict(source='API-Football', engine='statistical',
        fixtures=[dict(match_id=str(100 + i), league='39', home_id=str(10 + i * 2),
            away_id=str(11 + i * 2), home=f'Home {i}', away=f'Away {i}',
            timestamp=f'2026-11-06T{14 + i}:00:00', provider='api-football',
            status='TIMED', raw_status='NS', is_upcoming=True) for i in range(2)],
        forecast=dict(eligible=True, version='calculation-v1', size=2, league='39',
            before='2026-11-06', latest='2026-11-05T20:00:00',
            ranking=[dict(sequence=['1st', '2nd'], probability=.3),
                     dict(sequence=['Égal', '2nd'], probability=.2),
                     dict(sequence=['2nd', '2nd'], probability=.1)],
            normalization=1.1, training_groups=35, club_estimates=[dict(diagnostic='Public football calculation')]),
        model_generation='generation-1', competition=dict(competition='Premier League', country='England'),
        country='England')


def raw(fixture, status='FT', ht=(1, 0), ft=(1, 0)):
    return dict(provider='api-football', fixture=dict(id=int(fixture['match_id']),
        date=fixture['timestamp'] + 'Z', status=dict(short=status, elapsed=90)),
        league=dict(id=int(fixture['league']), name='Premier League', country='England', season=2026),
        teams=dict(home=dict(id=int(fixture['home_id']), name=fixture['home']),
                   away=dict(id=int(fixture['away_id']), name=fixture['away'])),
        score=dict(halftime=dict(home=ht[0], away=ht[1]),
                   fulltime=dict(home=ft[0], away=ft[1]),
                   extratime=dict(home=7, away=7), penalty=dict(home=9, away=8)))


class ManualAnalysisStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'manual' / 'manual_analyses.sqlite3'
        self.enterContext(patch.dict(os.environ, {'PATTERNLAB_STORAGE_MODE': 'local'}))
        self.enterContext(patch.object(archive.api, 'urlopen', side_effect=AssertionError('No network')))

    def record(self, value=None, clock=None):
        return archive.record(payload() if value is None else value, path=self.path,
                              clock=(lambda: NOW) if clock is None else clock)

    def test_reading_absent_archive_creates_nothing(self):
        self.assertEqual(archive.list_entries(self.path), [])
        self.assertEqual(archive.days(self.path), {})
        self.assertIsNone(archive.get('a' * 64, self.path))
        self.assertFalse(self.path.parent.exists())

    def test_days_aggregates_only_indexed_dates_without_decoding_forecasts(self):
        self.record()
        changed = payload(); changed['forecast']['version'] = 'calculation-v2'
        self.record(changed)
        tomorrow = payload()
        for fixture in tomorrow['fixtures']:
            fixture['timestamp'] = fixture['timestamp'].replace('2026-11-06', '2026-11-07')
        self.record(tomorrow)
        before = self.path.read_bytes()
        with patch.object(archive, '_read', side_effect=AssertionError('Do not decode snapshots')), \
                patch.object(archive, 'list_entries', side_effect=AssertionError('Do not load entries')):
            self.assertEqual(archive.days(self.path), {'2026-11-07': 1, '2026-11-06': 2})
        self.assertEqual(self.path.read_bytes(), before)

    def test_days_rejects_an_invalid_indexed_iso_date(self):
        self.record()
        with sqlite3.connect(self.path) as con:
            con.execute('DROP TRIGGER manual_analyses_no_update')
            con.execute("UPDATE manual_analyses SET day='2026-13-01'")
            con.execute(archive._TRIGGERS['manual_analyses_no_update'])
        with self.assertRaises(archive.ManualAnalysisError):
            archive.days(self.path)

    def test_exact_snapshot_deduplicates_and_different_versions_are_preserved(self):
        original = payload(); before = deepcopy(original)
        first = self.record(original)
        again = self.record(original, clock=lambda: NOW + timedelta(minutes=1))
        self.assertTrue(first['created']); self.assertFalse(again['created'])
        self.assertEqual(first['id'], again['id'])
        self.assertEqual(first['created_at'], again['created_at'])
        self.assertEqual(first['forecast'], original['forecast'])
        self.assertEqual(original, before)
        changed = deepcopy(original); changed['forecast']['version'] = 'calculation-v2'
        second = self.record(changed, clock=lambda: NOW + timedelta(minutes=2))
        self.assertNotEqual(second['id'], first['id'])
        self.assertEqual([entry['id'] for entry in archive.list_entries(self.path)], [second['id'], first['id']])
        self.assertEqual(archive.days(self.path), {'2026-11-06': 2})
        self.assertEqual(len(archive.list_entries(self.path, day='2026-11-07')), 0)
        self.assertEqual(archive.get(first['id'], self.path)['id'], first['id'])
        self.assertEqual({path.name for path in self.path.parent.iterdir()}, {self.path.name})

    def test_only_capture_time_does_not_create_a_distinct_calculation(self):
        original = payload(); original['forecast']['computed_at'] = NOW.isoformat()
        first = self.record(original)
        original['forecast']['computed_at'] = (NOW + timedelta(minutes=1)).isoformat()
        self.assertEqual(self.record(original)['id'], first['id'])
        original['model_generation'] = dict(id='another-generation')
        self.assertNotEqual(self.record(original)['id'], first['id'])

    def test_tomorrow_before_retains_real_exclusive_history_cutoff(self):
        value = payload()
        for fixture in value['fixtures']:
            fixture['timestamp'] = fixture['timestamp'].replace('2026-11-06', '2026-11-07')
        value['forecast']['before'] = value['forecast']['training_before'] = '2026-11-07'
        value['history_cutoff_exclusive'] = '2026-11-06'
        entry = self.record(value)
        self.assertEqual(entry['forecast']['before'], '2026-11-07')
        self.assertEqual(entry['history_cutoff_exclusive'], '2026-11-06')
        del value['history_cutoff_exclusive']
        with self.assertRaises(archive.ManualAnalysisError):
            self.record(value)

    def test_results_history_rows_and_private_identity_are_not_copied(self):
        value = payload()
        value['email'] = 'private@example.invalid'
        value['result'] = dict(matches=['Must not be archived'])
        value['forecast']['blocks'] = ['x' * 600000]
        value['forecast']['neighbors'] = [dict(sequence=['1st', '1st'], distance=.2,
                                            matches=[dict(ht='1-0', ft='1-0')], user_id='private')]
        saved = self.record(value)
        encoded = json.dumps(saved)
        self.assertNotIn('private', encoded)
        self.assertNotIn('blocks', saved['forecast'])
        self.assertEqual(saved['forecast']['neighbors'], [dict(sequence=['1st', '1st'], distance=.2)])
        self.assertNotIn('result', saved)

    def test_invalid_or_noncausal_inputs_leave_no_database(self):
        cases = []
        for key, value in [('source', 'sample'), ('engine', 'unknown')]:
            case = payload(); case[key] = value; cases.append(case)
        for probability in (float('nan'), float('inf'), -1, 2, True):
            case = payload(); case['forecast']['ranking'][0]['probability'] = probability; cases.append(case)
        case = payload(); case['forecast']['ranking'][1]['sequence'] = ['1st', '2nd']; cases.append(case)
        case = payload(); case['forecast']['ranking'][0]['sequence'] = ['home', 'away']; cases.append(case)
        case = payload(); case['forecast']['ranking'].pop(); cases.append(case)
        case = payload(); case['forecast']['ranking'][0]['probability'] = .8; cases.append(case)
        case = payload(); case['forecast']['latest'] = '2026-11-06T00:00:00'; cases.append(case)
        case = payload(); case['forecast']['before'] = '2026-11-07'; cases.append(case)
        case = payload(); case['forecast']['training_cutoff'] = '2026-11-07'; cases.append(case)
        case = payload(); case['forecast']['eligible'] = False; cases.append(case)
        case = payload(); case['fixtures'][0]['ht'] = None; cases.append(case)
        case = payload(); case['fixtures'].reverse(); cases.append(case)
        case = payload(); case['fixtures'][0]['raw_status'] = 'TBD'; cases.append(case)
        case = payload(); case['fixtures'][0]['timestamp'] = NOW.isoformat(); cases.append(case)
        case = payload(); case['forecast']['diagnostic'] = 'x' * archive.MAX_BYTES; cases.append(case)
        case = payload(); case['forecast']['custom'] = object(); cases.append(case)
        for case in cases:
            with self.subTest(case=repr(case)[:150]), self.assertRaises(archive.ManualAnalysisError):
                self.record(case)
            self.assertFalse(self.path.exists())

    def test_injected_clock_requires_an_explicit_isolated_path(self):
        with patch.object(archive.api, 'DATA_DIR', self.path.parent):
            with self.assertRaises(archive.ManualAnalysisError):
                archive.record(payload(), clock=lambda: NOW)
            with self.assertRaises(archive.ManualAnalysisError):
                archive.record(payload(), path=self.path, clock=lambda: NOW)
        self.assertFalse(self.path.exists())

    def test_real_sqlite_concurrent_identical_writers_create_one_entry(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            records = list(pool.map(lambda _: self.record(), range(16)))
        self.assertEqual(sum(record['created'] for record in records), 1)
        self.assertEqual(len({record['id'] for record in records}), 1)
        self.assertEqual(len(archive.list_entries(self.path)), 1)

    def test_crossing_kickoff_before_or_after_insert_rolls_back(self):
        kickoff = NOW.replace(hour=14)
        for stamps in ((NOW, kickoff), (NOW, NOW, kickoff)):
            with self.subTest(stamps=stamps):
                values = iter(stamps)
                with self.assertRaises(archive.ManualAnalysisError):
                    self.record(clock=lambda: next(values))
                self.assertEqual(archive.list_entries(self.path), [])
        self.record()
        with self.assertRaises(archive.ManualAnalysisError):
            self.record(clock=lambda: kickoff)
        self.assertEqual(len(archive.list_entries(self.path)), 1)

    def test_database_refuses_update_delete_and_replace(self):
        self.record()
        with sqlite3.connect(self.path) as con:
            row = con.execute('SELECT * FROM manual_analyses').fetchone()
            for statement, parameters in (
                ("UPDATE manual_analyses SET day='2099-01-01'", ()),
                ('DELETE FROM manual_analyses', ()),
                ('INSERT OR REPLACE INTO manual_analyses VALUES(?,?,?,?,?)', row)):
                with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                    con.execute(statement, parameters)
        self.assertEqual(len(archive.list_entries(self.path)), 1)

    def test_reads_detect_payload_tamper_and_schema_tamper(self):
        entry = self.record()
        with sqlite3.connect(self.path) as con:
            con.execute('DROP TRIGGER manual_analyses_no_update')
            con.execute("UPDATE manual_analyses SET payload='{}'")
            con.execute(archive._TRIGGERS['manual_analyses_no_update'])
        with self.assertRaises(archive.ManualAnalysisError):
            archive.get(entry['id'], self.path)
        with self.assertRaises(archive.ManualAnalysisError):
            archive.list_entries(self.path, day='2026-11-06')
        # Navigation counts inspect the day index; opening a day verifies its snapshots.
        self.assertEqual(archive.days(self.path), {'2026-11-06': 1})
        with sqlite3.connect(self.path) as con:
            con.execute('DROP TRIGGER manual_analyses_no_delete')
        with self.assertRaises(archive.ManualAnalysisError):
            archive.list_entries(self.path)
        with self.assertRaises(archive.ManualAnalysisError):
            archive.days(self.path)

    def test_rehashed_tampered_semantic_snapshot_is_still_rejected(self):
        self.record()
        with sqlite3.connect(self.path) as con:
            encoded = con.execute('SELECT payload FROM manual_analyses').fetchone()[0]
            value = json.loads(encoded); value['forecast']['ranking'][0]['probability'] = .31
            encoded = archive._json(value)
            con.execute('DROP TRIGGER manual_analyses_no_update')
            con.execute('UPDATE manual_analyses SET payload=?,digest=?', (encoded, sha256(encoded.encode()).hexdigest()))
            con.execute(archive._TRIGGERS['manual_analyses_no_update'])
        with self.assertRaises(archive.ManualAnalysisError):
            archive.list_entries(self.path)

    def test_evaluate_uses_real_api_halftime_and_regulation_fulltime_without_writing(self):
        entry = self.record()
        observations = [raw(entry['fixtures'][0], ht=(1, 0), ft=(1, 0)),
                        raw(entry['fixtures'][1], status='PEN', ht=(0, 0), ft=(2, 1))]
        before = deepcopy((entry, observations)); database = self.path.read_bytes()
        with patch.object(archive.journal, '_settle', side_effect=AssertionError('No journal settlement')):
            result = archive.evaluate(entry, observations, now=NOW.replace(hour=20))
        self.assertEqual(result['status'], 'settled')
        self.assertEqual(result['actual'], ['1st', '2nd'])
        self.assertEqual([row['matches_all'] for row in result['scenario_verdicts']], [True, False, False])
        self.assertTrue(result['top1']); self.assertTrue(result['top3'])
        self.assertEqual((entry, observations), before)
        self.assertEqual(self.path.read_bytes(), database)

    def test_missing_scores_nonfinal_and_wrong_identity_never_invent_results(self):
        entry = self.record()
        fixtures = entry['fixtures']
        observations = [raw(fixtures[0]), raw(fixtures[1])]
        cases = []
        for status in ('NS', '2H', 'HT', 'PST', 'SUSP'):
            case = deepcopy(observations); case[1]['fixture']['status']['short'] = status
            cases.append((case, NOW.replace(hour=17), 'pending'))
            cases.append((case, NOW.replace(hour=22), 'non_evaluable'))
        case = deepcopy(observations); case[1]['score']['halftime']['home'] = None
        cases.append((case, NOW.replace(hour=22), 'non_evaluable'))
        case = deepcopy(observations); case[1]['score']['fulltime']['home'] = None
        cases.append((case, NOW.replace(hour=22), 'non_evaluable'))
        case = deepcopy(observations); case[1]['teams']['home']['id'] = 999
        cases.append((case, NOW.replace(hour=22), 'non_evaluable'))
        case = deepcopy(observations); case[1]['league']['id'] = 999
        cases.append((case, NOW.replace(hour=22), 'non_evaluable'))
        cases.append((observations[:1], NOW.replace(hour=22), 'non_evaluable'))
        for code in ('CANC', 'ABD', 'AWD', 'WO'):
            case = deepcopy(observations); case[1]['fixture']['status']['short'] = code
            cases.append((case, NOW.replace(hour=22), 'void'))
        for case, now, status in cases:
            with self.subTest(status=status, case=case):
                result = archive.evaluate(entry, case, now)
                self.assertEqual(result['status'], status)
                self.assertIsNone(result['actual'])
                self.assertIsNone(result['top1']); self.assertIsNone(result['top3'])
                self.assertEqual([row['matches_all'] for row in result['scenario_verdicts']], [None] * 3)


if __name__ == '__main__':
    unittest.main()
