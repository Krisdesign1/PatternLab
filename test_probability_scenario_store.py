"""Scenario choices append to forecasts without changing their first snapshot."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import prediction_journal as journal
import probability_scenario_store as store
import prospective_models as models


NOW = datetime(2026, 10, 22, 9, tzinfo=timezone.utc)
P = [{'1st': .6, 'Égal': .1, '2nd': .3}, {'1st': .2, 'Égal': .3, '2nd': .5}]
B = {'1st': .3, 'Égal': .3, '2nd': .4}


def forecast(*, mixed=False, baseline=True):
    fixtures = [dict(match_id=str(100+i), league=str(39 if not mixed or i == 0 else 61),
                     home_id=str(1000+i*2), away_id=str(1001+i*2), home=f'Home {i}', away=f'Away {i}',
                     timestamp=f'2026-10-22T{14+i}:00:00', provider='api-football') for i in range(2)]
    provenance = dict(source='API-Football', league='39', training_before='2026-10-22')
    if not mixed:
        details = [dict(probabilities=p, baseline=B) if baseline else dict(probabilities=p) for p in P]
        method = models._method('clubs-test', models._distribution('independent', 2, distributions=P),
                                provenance, club_estimates=details)
    else:
        components = []
        for i, fixture in enumerate(fixtures):
            details = [dict(probabilities=P[i], baseline=B) if baseline else dict(probabilities=P[i])]
            child = models._method('clubs-test', models._distribution('independent', 1, distributions=[P[i]]),
                                   dict(provenance, league=fixture['league']), _component=True,
                                   club_estimates=details)
            components.append(dict(league=fixture['league'], indices=[i], method=child))
        method = models._method(models.MIXED_VERSION, dict(schema=models.DISTRIBUTION_VERSION,
            type='block_product', size=2, components=components),
            dict(provenance, league='mixed', group_kind='mixed', provider='api-football',
                 leagues=['39', '61'], fixtures=fixtures, assumes_block_independence=True,
                 correction_scope='within_each_league_only'))
    return dict(id=journal._group_key(fixtures), created_at=NOW.isoformat(), source='API-Football',
                size=2, fixtures=fixtures, methods={'clubs': method},
                metadata={'competitions': {'39': {'competition': 'Premier League'}}}, status='pending')


def selection(sequence=('1st', '2nd')):
    # A caller may be holding fresher probabilities; these must never override P.
    return dict(requested_scenario=dict(sequence=list(sequence), probability=.999),
                search={'outcome': 'mixed', 'minimum': 0, 'query': 'clubs'},
                match_probabilities=[dict(match_id='100', probabilities={'1st': .1, 'Égal': .8, '2nd': .1},
                                          baseline={'1st': .1, 'Égal': .8, '2nd': .1})])


class ProbabilityScenarioStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)/'new-directory'/'probability_scenarios.sqlite3'

    def record(self, entry=None, chosen=None, **kwargs):
        return store.record(forecast() if entry is None else entry,
                            selection() if chosen is None else chosen, path=self.path,
                            clock=kwargs.get('clock', lambda: NOW+timedelta(minutes=1)))

    def test_read_of_absent_storage_creates_neither_file_nor_parent(self):
        self.assertEqual(store.list_intents(self.path), [])
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.parent.exists())

    def test_record_mixed_choices_uses_frozen_probabilities_and_baselines_only(self):
        entry, chosen = forecast(mixed=True), selection()
        original, original_selection = deepcopy(entry), deepcopy(chosen)
        with patch.object(api.ApiFootballClient, 'get', side_effect=AssertionError('No API')):
            intent = self.record(entry, chosen)
        self.assertTrue(intent['created'])
        self.assertEqual(intent['selection']['policy'], 'high-probability-v3')
        self.assertEqual(intent['selection']['requested_scenario']['probability'], .3)
        self.assertEqual(intent['selection']['search']['outcome'], 'mixed')
        self.assertEqual(intent['entry_id'], entry['id'])
        self.assertEqual(intent['forecast_digest'], store.forecast_digest(entry))
        rows = intent['selection']['match_probabilities']
        self.assertEqual(rows[0]['probabilities'], P[0])
        self.assertEqual(rows[0]['baseline'], B)
        self.assertAlmostEqual(rows[0]['delta_points'], 30)
        self.assertAlmostEqual(rows[1]['delta_points'], 10)
        self.assertEqual([row['search_outcome'] for row in rows], ['1st', '2nd'])
        self.assertEqual(entry, original)
        self.assertEqual(chosen, original_selection)
        self.assertEqual(store.list_intents(self.path), [{k: v for k, v in intent.items() if k != 'created'}])

    def test_repeated_click_is_idempotent_but_changed_choice_is_distinct(self):
        first = self.record()
        second = self.record()
        changed = self.record(chosen=selection(('Égal', '2nd')))
        self.assertFalse(second['created'])
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(first['created_at'], second['created_at'])
        self.assertTrue(changed['created'])
        self.assertNotEqual(first['id'], changed['id'])
        self.assertEqual(changed['selection']['requested_scenario']['probability'], .05)
        self.assertEqual(len(store.list_intents(self.path)), 2)

    def test_single_outcome_search_preserves_criteria_without_relabeling_favorite(self):
        intent = self.record(chosen=selection(('Égal', 'Égal')))
        self.assertEqual(intent['selection']['search'], dict(outcome='Égal', minimum=0, query='clubs'))
        self.assertEqual([row['choice'] for row in intent['selection']['match_probabilities']], ['1st', '2nd'])
        self.assertEqual([row['search_outcome'] for row in intent['selection']['match_probabilities']], ['Égal', 'Égal'])

    def test_missing_frozen_baseline_stays_missing_despite_current_caller_baseline(self):
        intent = self.record(forecast(baseline=False))
        self.assertTrue(all(row['baseline'] is None and row['delta_points'] is None
                            for row in intent['selection']['match_probabilities']))

    def test_origin_metadata_baseline_precedes_fallback_and_probabilities_must_agree(self):
        entry = forecast(baseline=False)
        entry['metadata']['selection'] = dict(before='2026-10-21', match_probabilities=[
            dict(match_id=str(100+i), probabilities=p, baseline=B) for i, p in enumerate(P)])
        intent = self.record(entry)
        self.assertEqual(intent['selection']['before'], '2026-10-21')
        self.assertEqual(intent['selection']['match_probabilities'][0]['baseline'], B)
        altered = deepcopy(entry)
        altered['metadata']['selection']['match_probabilities'][0]['probabilities'] = P[1]
        with self.assertRaises(store.ScenarioStoreError):
            self.record(altered, selection(('Égal', 'Égal')))
        self.assertEqual(len(store.list_intents(self.path)), 1)

    def test_digest_ignores_result_revisions_but_detects_forecast_change(self):
        entry = forecast()
        digest = store.forecast_digest(entry)
        entry.update(status='settled', actual=['1st', '2nd'], revision=5,
                     result_history=[{'revision': 5}], evaluations={'clubs': {'hit': True}})
        self.assertEqual(digest, store.forecast_digest(entry))
        entry['metadata']['other_origin_fact'] = 'changed'
        self.assertNotEqual(digest, store.forecast_digest(entry))

    def test_reusing_same_identity_with_changed_forecast_is_rejected(self):
        entry = forecast()
        first = self.record(entry)
        entry['methods']['clubs']['version'] = 'modified'
        with self.assertRaises(store.ScenarioStoreError):
            self.record(entry)
        self.assertEqual(store.list_intents(self.path)[0]['forecast_digest'], first['forecast_digest'])

    def test_clock_guards_apply_before_create_and_to_duplicate_clicks(self):
        entry = forecast()
        late = NOW.replace(hour=14)
        for clock in (lambda: late, lambda: NOW+timedelta(days=1), lambda: NOW-timedelta(seconds=1),
                      lambda: NOW.replace(tzinfo=None)):
            with self.subTest(clock=clock), self.assertRaises(store.ScenarioStoreError):
                self.record(entry, clock=clock)
            self.assertFalse(self.path.exists())
        self.record(entry)
        original = self.path.read_bytes()
        with self.assertRaises(store.ScenarioStoreError):
            self.record(entry, clock=lambda: late)
        self.assertEqual(self.path.read_bytes(), original)

    def test_insert_and_precommit_time_guards_roll_back_entire_intent(self):
        late = NOW.replace(hour=14)
        for times in ([NOW, late], [NOW, NOW, late]):
            with self.subTest(times=times):
                sequence = iter(times)
                with self.assertRaises(store.ScenarioStoreError):
                    self.record(clock=lambda: next(sequence))
                self.assertEqual(store.list_intents(self.path), [])

    def test_bad_sequence_forecast_identity_or_baseline_never_creates_storage(self):
        cases = []
        for seq in (['1st'], ['1st', 'wrong'], '1st'):
            value = selection()
            value['requested_scenario']['sequence'] = seq
            cases.append((forecast(), value))
        for field, value in [('source', 'foreign'), ('id', 'wrong'), ('size', 3), ('created_at', 'invalid'),
                             ('created_at', NOW.replace(hour=14).isoformat())]:
            entry = forecast()
            entry[field] = value
            cases.append((entry, selection()))
        entry = forecast()
        entry['methods']['clubs']['club_estimates'][0]['baseline'] = {'1st': 2, 'Égal': 0, '2nd': -1}
        cases.append((entry, selection()))
        for entry, chosen in cases:
            with self.subTest(entry=entry.get('id'), selected=chosen), self.assertRaises(store.ScenarioStoreError):
                self.record(entry, chosen)
            self.assertFalse(self.path.exists())

    def test_sqlite_guards_prevent_updates_and_deletions(self):
        self.record()
        with sqlite3.connect(self.path) as con:
            for statement in ('UPDATE intents SET payload=payload', 'DELETE FROM intents'):
                with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                    con.execute(statement)
        self.assertEqual(len(store.list_intents(self.path)), 1)

    def test_read_checks_payload_digest_and_record_refuses_corrupt_existing_history(self):
        self.record()
        with sqlite3.connect(self.path) as con:
            con.execute('DROP TRIGGER intents_no_update')
            con.execute("UPDATE intents SET payload='{}'")
        with self.assertRaises(store.ScenarioStoreError):
            store.list_intents(self.path)
        with self.assertRaises(store.ScenarioStoreError):
            self.record(chosen=selection(('Égal', 'Égal')))
        with sqlite3.connect(self.path) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM intents').fetchone()[0], 1)

    def test_list_is_read_only_and_rejects_an_unrelated_database(self):
        self.record()
        original = self.path.read_bytes()
        store.list_intents(self.path)
        self.assertEqual(self.path.read_bytes(), original)
        unrelated = self.path.parent/'unrelated.sqlite3'
        with sqlite3.connect(unrelated) as con:
            con.execute('CREATE TABLE other(id TEXT)')
        with self.assertRaises(store.ScenarioStoreError):
            store.list_intents(unrelated)

    def test_default_path_is_the_dedicated_store_not_the_forecast_journal(self):
        with patch.object(api, 'DATA_DIR', self.path.parent):
            result = store.record(forecast(), selection(), clock=lambda: NOW)
            self.assertEqual(store.list_intents()[0]['id'], result['id'])
        self.assertTrue(self.path.exists())
        self.assertFalse((self.path.parent/'predictions.sqlite3').exists())


if __name__ == '__main__':
    unittest.main()
