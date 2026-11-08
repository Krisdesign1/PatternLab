"""Information-time reads use one known response, never a later projection."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
import sqlite3
import tempfile
import unittest

from halftime_statistics_store import Store


FIRST = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
SECOND = FIRST + timedelta(hours=1)
BEFORE = '2025-01-01T00:00:00'


def fixture(match_id='100', **changes):
    row = dict(match_id=match_id, league='39', season=2024,
               kickoff_utc='2024-09-01T15:00:00', home_id='10', away_id='20')
    return dict(row, **changes)


class AsOfStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.path = self.root / 'statistics.sqlite3'
        self.store = Store(self.path)
        self.task = fixture()
        self.store.enqueue([self.task])

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def record(self, when, *, task=None, value=3, classification='ready', partial=False, xg=None):
        task = task or self.task
        periods = {}
        if classification in ('ready', 'partial'):
            periods['first'] = dict(pairs={'Total Shots': [value, value + 1]})
            if not partial:
                periods['first']['pairs']['Shots on Goal'] = [1, 2]
                periods['second'] = dict(pairs={'Total Shots': [value + 2, value + 3]})
        if xg is not None:
            for index, period in enumerate(('full', 'first', 'second')):
                if xg[0][index] is not None and xg[1][index] is not None:
                    periods.setdefault(period, dict(pairs={}))['pairs']['expected_goals'] = [
                        xg[0][index], xg[1][index]]
        parsed = dict(parser_version='test', periods=periods)
        envelope = dict(endpoint='fixtures/statistics', transport='received', http_status=200,
                        parameters=dict(fixture=int(task['match_id']), half='true'),
                        observed_at_utc=when.isoformat(),
                        payload=dict(classification=classification, value=value, partial=partial))
        attempt = self.store.reserve(task['match_id'], source='test', now=when)
        self.store.finish(attempt, task, envelope, parsed, state=classification,
                          availability=classification, now=when)
        return attempt

    def values(self, cutoff, *, team='10', before=BEFORE, store=None):
        return (store or self.store).before(team, before, observed_before=cutoff)

    def test_previous_response_remains_available_before_latest_collection(self):
        self.record(FIRST, value=3)
        self.record(SECOND, value=8)
        old = self.values(SECOND)
        self.assertTrue(old)
        self.assertEqual({row['collected_at'] for row in old}, {FIRST.timestamp()})
        self.assertEqual(next(row['value'] for row in old if row['metric'] == 'Total Shots'), 3)
        current = self.values(SECOND + timedelta(microseconds=1))
        self.assertEqual({row['collected_at'] for row in current}, {SECOND.timestamp()})
        self.assertEqual(self.store.before('10', BEFORE), current)

    def test_observation_and_kickoff_bounds_are_strict_and_timezone_aware(self):
        self.record(FIRST)
        self.assertEqual(self.values(FIRST), [])
        self.assertEqual(self.values(FIRST - timedelta(microseconds=1)), [])
        self.assertTrue(self.values(FIRST + timedelta(microseconds=1)))
        same_instant = FIRST.astimezone(timezone(timedelta(hours=2)))
        self.assertEqual(self.values(same_instant), [])
        self.assertEqual(self.values(SECOND, before=self.task['kickoff_utc']), [])
        with self.assertRaises(ValueError):
            self.values(FIRST.replace(tzinfo=None))

    def test_empty_invalid_or_failed_response_masks_prior_measurements(self):
        for number, classification in enumerate(('empty', 'invalid', 'unknown_shape',
                                                  'transport_error', 'api_error')):
            with self.subTest(classification=classification):
                task = fixture(str(200 + number))
                self.store.enqueue([task])
                self.record(FIRST, task=task)
                self.record(SECOND, task=task, classification=classification)
                previous = [r for r in self.values(SECOND) if r['match_id'] == task['match_id']]
                self.assertTrue(previous)
                after = [r for r in self.values(SECOND + timedelta(seconds=1))
                         if r['match_id'] == task['match_id']]
                self.assertEqual(after, [])
                self.assertFalse(any(r['match_id'] == task['match_id']
                                     for r in self.store.before('10', BEFORE)))

    def test_partial_response_does_not_fill_missing_metrics_from_old_response(self):
        self.record(FIRST)
        self.record(SECOND, classification='partial', value=9, partial=True)
        result = self.values(SECOND + timedelta(seconds=1))
        self.assertEqual([(r['period'], r['metric'], r['value']) for r in result],
                         [('first', 'Total Shots', 9)])

    def test_unfinished_and_interrupted_requests_do_not_supersede_a_response(self):
        self.record(FIRST)
        self.store.reserve('100', source='test', now=SECOND)
        self.assertTrue(self.values(SECOND + timedelta(seconds=1)))
        self.store.recover(SECOND + timedelta(seconds=2))
        self.assertTrue(self.values(SECOND + timedelta(seconds=3)))
        self.assertEqual({r['collected_at'] for r in self.values(SECOND + timedelta(seconds=3))},
                         {FIRST.timestamp()})

    def test_information_time_order_precedes_import_order_and_ties_use_latest_id(self):
        self.record(SECOND, value=8)
        self.record(FIRST, value=3)
        self.assertEqual({r['collected_at'] for r in self.values(SECOND + timedelta(seconds=1))},
                         {SECOND.timestamp()})
        # The no-cutoff path deliberately retains its existing latest pointer semantics.
        self.assertEqual({r['collected_at'] for r in self.store.before('10', BEFORE)},
                         {FIRST.timestamp()})
        self.record(SECOND, value=12)
        latest = self.values(SECOND + timedelta(seconds=1))
        self.assertEqual(next(r['value'] for r in latest if r['metric'] == 'Total Shots'), 12)

    def test_match_and_team_references_remain_separate(self):
        other = fixture('101', home_id='30')
        self.store.enqueue([other])
        self.record(FIRST, value=3)
        self.record(FIRST, task=other, value=9)
        self.record(SECOND, value=7)
        self.assertEqual({r['match_id'] for r in self.values(SECOND)}, {'100'})
        self.assertEqual({r['match_id'] for r in self.values(SECOND, team='30')}, {'101'})
        away = self.values(SECOND, team='20')
        self.assertEqual({r['match_id'] for r in away}, {'100', '101'})
        self.assertEqual(self.values(SECOND, team='999'), [])

    def test_identity_change_never_relabels_old_observations(self):
        changes = (dict(league='40'), dict(season=2025),
                   dict(kickoff_utc='2024-09-02T15:00:00'), dict(home_id='30'),
                   dict(away_id='30'), dict(home_id='20', away_id='10'))
        for number, changed in enumerate(changes):
            with self.subTest(changed=changed):
                original = fixture(str(300 + number))
                revised = dict(original, **changed)
                self.store.enqueue([original])
                self.record(FIRST, task=original)
                self.store.enqueue([revised])
                self.assertFalse(any(r['match_id'] == original['match_id']
                                     for r in self.values(SECOND, team=revised['home_id'])))
                self.record(SECOND, task=revised, value=8)
                # Even after a fresh collection, the preceding identity stays invalid.
                self.assertFalse(any(r['match_id'] == original['match_id']
                                     for r in self.values(SECOND, team=revised['home_id'])))
                current = [r for r in self.values(SECOND + timedelta(seconds=1),
                                                  team=revised['home_id'])
                           if r['match_id'] == original['match_id']]
                self.assertTrue(current)
                self.assertEqual({r['league'] for r in current}, {revised['league']})
                self.assertEqual({r['kickoff_utc'] for r in current}, {revised['kickoff_utc']})

    def test_unchanged_enqueue_preserves_observation_history(self):
        self.record(FIRST)
        self.store.enqueue([dict(self.task)])
        self.record(SECOND, value=8)
        self.assertTrue(self.values(SECOND))

    def test_readonly_existing_store_needs_no_index_migration_or_writes(self):
        self.record(FIRST)
        self.record(SECOND, value=8)
        self.store.db.execute('DROP INDEX attempts_information_time')
        self.store.close()
        content = sha256(self.path.read_bytes()).hexdigest()
        with Store(self.path, readonly=True) as store:
            self.assertTrue(self.values(SECOND, store=store))
            self.assertEqual(store.db.execute('PRAGMA query_only').fetchone()[0], 1)
            self.assertFalse(store.db.execute("SELECT 1 FROM sqlite_master WHERE name='attempts_information_time'").fetchone())
            with self.assertRaises(sqlite3.OperationalError):
                store.db.execute("UPDATE tasks SET availability='empty'")
        self.assertEqual(sha256(self.path.read_bytes()).hexdigest(), content)

    def test_readonly_missing_store_creates_no_parent_or_database(self):
        path = self.root / 'absent' / 'statistics.sqlite3'
        with Store(path, readonly=True) as store:
            self.assertEqual(self.values(SECOND, store=store), [])
            self.assertEqual(store.before('10', BEFORE), [])
        self.assertFalse(path.parent.exists())

    def test_existing_anomalies_hide_both_teams_xg_but_preserve_storage_and_shots(self):
        anomalies = (('1213857', 0, (0.17, 0.17, 0.12)),
                     ('1208600', 0, (0.89, 0.89, 0.03)),
                     ('1351043', 1, (0.24, 0.24, 0.12)))
        for match_id, side, bad in anomalies:
            task = fixture(match_id)
            self.store.enqueue([task])
            xg = [(1.2, 0.5, 0.7), (0.7, 0.4, 0.3)]
            xg[side] = bad
            self.record(FIRST, task=task, xg=xg)
        stored = [tuple(r) for r in self.store.db.execute('SELECT * FROM measurements ORDER BY observation_id,period,team_id,metric')]
        envelopes = [tuple(r) for r in self.store.db.execute('SELECT id,envelope_json FROM attempts ORDER BY id')]
        for team in ('10', '20'):
            for cutoff in (None, SECOND):
                with self.subTest(team=team, cutoff=cutoff):
                    result = self.values(cutoff, team=team)
                    self.assertTrue(result)
                    self.assertFalse(any(r['metric'] == 'expected_goals' for r in result))
                    self.assertTrue(all(r['metric'] in ('Total Shots', 'Shots on Goal') for r in result))
        self.assertEqual([tuple(r) for r in self.store.db.execute('SELECT * FROM measurements ORDER BY observation_id,period,team_id,metric')], stored)
        self.assertEqual([tuple(r) for r in self.store.db.execute('SELECT id,envelope_json FROM attempts ORDER BY id')], envelopes)

    def test_xg_quality_uses_selected_observation_without_mix_or_fallback(self):
        self.record(FIRST, xg=((1.2, 0.5, 0.7), (0.7, 0.4, 0.3)))
        self.record(SECOND, xg=((0.89, 0.89, 0.03), (0.7, 0.4, 0.3)))
        self.assertEqual(sum(r['metric'] == 'expected_goals' for r in self.values(SECOND)), 3)
        self.assertFalse(any(r['metric'] == 'expected_goals'
                             for r in self.values(SECOND + timedelta(seconds=1))))
        self.assertFalse(any(r['metric'] == 'expected_goals' for r in self.store.before('10', BEFORE)))
        # A fresh consistent response can make xG available again.
        self.record(SECOND + timedelta(minutes=1), xg=((0, 0, 0), (0.7, 0.4, 0.3)))
        zeros = [r['value'] for r in self.store.before('10', BEFORE) if r['metric'] == 'expected_goals']
        self.assertEqual(zeros, [0, 0, 0])

    def test_xg_rounding_boundary_and_missing_period_remain_measurements(self):
        examples = (((0.315, 0.1, 0.2), 3), ((0.285, 0.1, 0.2), 3),
                    ((0.316, 0.1, 0.2), 0), ((0.284, 0.1, 0.2), 0),
                    ((0.89, 0.89, None), 2))
        for number, (home, expected) in enumerate(examples):
            with self.subTest(home=home):
                task = fixture(str(400 + number))
                self.store.enqueue([task])
                self.record(FIRST, task=task, xg=(home, (0.7, 0.4, 0.3)))
                rows = [r for r in self.values(SECOND) if r['match_id'] == task['match_id']
                        and r['metric'] == 'expected_goals']
                self.assertEqual(len(rows), expected)


if __name__ == '__main__':
    unittest.main()
