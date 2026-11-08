from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import catboost_scenarios as ml
import prediction_journal as journal
import prospective_models as models
import scenario_group_contract as contract
from test_catboost_scenarios import fixtures, sample
from test_prediction_journal import raw_fixtures
from test_scenario_group_contract import entry as synthetic_entry


DECISION = datetime(2025,3,19,9,tzinfo=timezone.utc)


class ScenarioJournalContractTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)/'journal.sqlite3'
        for p in (patch.object(ml,'load_report',return_value=None),
                  patch.object(models,'_context_neighbors',side_effect=models.SnapshotError('No context')),
                  patch.object(api.ApiFootballClient,'get',side_effect=AssertionError('No API'))):
            p.start(); self.addCleanup(p.stop)

    def methods(self, **kwargs):
        return models.build_methods(sample(),fixtures(),decision_at=DECISION,**kwargs)['methods']

    def record(self, methods=None, **kwargs):
        return journal.record(fixtures(), self.methods() if methods is None else methods,
            path=self.path, now=DECISION, raw_fixtures=raw_fixtures(), **kwargs)

    def test_fresh_future_group_keeps_current_cutoff_and_bound_identity(self):
        saved = self.record()
        result = contract.inspect(saved)
        self.assertEqual(result['status'],'valid',result['issues'])
        self.assertEqual(result['common_cutoff'],'2025-03-19')
        self.assertEqual(saved['group_contract']['status'],'valid')
        self.assertEqual(saved['group_contract']['decision_at'],DECISION.isoformat())
        self.assertEqual(saved['group_contract']['ordered_match_ids'],[r['match_id'] for r in fixtures()])

    def test_future_declared_history_cutoff_is_refused_before_any_database_write(self):
        methods = models.build_methods(sample(),fixtures())['methods']
        with self.assertRaisesRegex(journal.JournalError,'coupure'):
            self.record(methods)
        self.assertFalse(self.path.exists())

    def test_wrong_group_probabilities_and_mixed_method_cutoffs_are_refused(self):
        for problem in ('ids','cutoff','decision'):
            methods = self.methods()
            if problem == 'ids':
                methods['clubs']['ordered_match_ids'].reverse()
            elif problem == 'cutoff':
                methods['clubs']['training_before']='2025-03-18'
            else:
                methods['clubs']['decision_at']='2025-03-19T08:00:00+00:00'
            with self.subTest(problem=problem),self.assertRaises(journal.JournalError):
                self.record(methods)
            self.assertFalse(self.path.exists())

    def test_duplicate_provider_records_never_use_last_observation_silently(self):
        raw = raw_fixtures()
        for duplicate in (deepcopy(raw[0]), dict(deepcopy(raw[0]),goals={'home':1,'away':0})):
            with self.subTest(conflicting='goals' in duplicate),self.assertRaises(journal.JournalError):
                journal.record(fixtures(), self.methods(), path=self.path, now=DECISION,
                               raw_fixtures=raw+[duplicate])
            self.assertFalse(self.path.exists())

    def test_same_physical_fixture_with_distinct_ids_is_not_two_independent_legs(self):
        chosen = fixtures()[:2]
        chosen[1] = dict(chosen[0],match_id='9999')
        methods = synthetic_entry()['methods']
        with self.assertRaises(journal.JournalError):
            journal.record(chosen,methods,path=self.path,now=DECISION,raw_fixtures=raw_fixtures(chosen))
        self.assertFalse(self.path.exists())

    def test_old_first_prediction_is_preserved_even_if_new_attempt_has_bad_methods(self):
        original = self.record()
        with patch('scenario_group_contract.require_recordable',side_effect=AssertionError('No replacement')):
            reused = self.record({'clubs':{'ranking':'bad'}})
        self.assertFalse(reused['created'])
        self.assertEqual(reused['methods'],original['methods'])
        self.assertEqual(reused['created_at'],original['created_at'])

    def test_clock_reversal_cannot_save_a_group_before_its_declared_decision(self):
        times = iter([DECISION,datetime(2025,3,19,8,tzinfo=timezone.utc)])
        methods=models.build_mixed_methods(sample(),fixtures(),decision_at=DECISION)['methods']
        with self.assertRaises(journal.JournalError):
            journal.record_mixed(fixtures(), methods, path=self.path,
                raw_fixtures=raw_fixtures(), clock=lambda:next(times))
        self.assertEqual(journal.list_entries(self.path),[])

    def test_all_group_sizes_record_and_preserve_absolute_mass(self):
        for size in range(2,21):
            source = synthetic_entry(size)
            chosen=source['fixtures']; methods=source['methods']
            now=datetime.fromisoformat(source['created_at'])
            with self.subTest(size=size):
                saved=journal.record(chosen,methods,path=self.path,now=now,raw_fixtures=raw_fixtures(chosen))
                self.assertEqual(saved['size'],size)
                self.assertEqual(saved['methods'],methods)
                result=contract.inspect(saved)
                self.assertEqual(result['status'],'valid',result['issues'])
                self.assertAlmostEqual(result['methods'][0]['top1_probability'],.5**size)
                self.assertAlmostEqual(result['methods'][0]['top3_probability']+result['methods'][0]['remaining_probability'],1.)

    def test_tiny_group_probabilities_still_require_relative_agreement(self):
        chosen=synthetic_entry(20)['fixtures']
        distribution=models._distribution('independent',20,
            distributions=[{'1st':1/3,'Égal':1/3,'2nd':1/3} for _ in chosen])
        method=models._method('test',distribution,dict(source='API-Football',league='39'))
        self.assertAlmostEqual(models.probability_for_sequence(method,['1st']*20),(1/3)**20)
        # The former absolute 1e-12 tolerance accepted this material relative
        # discrepancy in a uniform twenty-match scenario (about 2.9e-10).
        original=method['ranking'][0]['probability']
        method['ranking'][0]['probability']=original+1e-13
        with self.assertRaises(models.SnapshotError):models.validate_method(method,20)


if __name__=='__main__':
    unittest.main()
