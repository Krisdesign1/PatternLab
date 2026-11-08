import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import catboost_scenarios as ml
import prediction_journal as journal
import prospective_models as models
from test_catboost_scenarios import sample, fixtures
from test_prospective_models import context_method

BEFORE = datetime(2025, 3, 19, 12, tzinfo=timezone.utc)
AFTER = datetime(2025, 3, 21, 12, tzinfo=timezone.utc)


def raw_fixtures(chosen=None, status='NS'):
    return [dict(fixture=dict(id=int(r['match_id']), date=r['timestamp']+'Z', status=dict(short=status)),
                 league=dict(id=int(r['league']), name='League', country='Country', season=2025),
                 teams=dict(home=dict(id=int(r['home_id']), name=r['home']), away=dict(id=int(r['away_id']), name=r['away'])),
                 score=dict(halftime=dict(home=1, away=0), fulltime=dict(home=1, away=1))) for r in (chosen or fixtures())]


class JournalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.object(ml, 'load_report', return_value=None), patch.object(models, '_context_neighbors',
                side_effect=models.SnapshotError('Configuration par contexte absente.')):
            cls.methods = models.build_methods(sample(), fixtures(), decision_at=BEFORE)['methods']

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)/'predictions.sqlite3'

    def record(self, **kwargs):
        return journal.record(fixtures(), self.methods, now=BEFORE, path=self.path,
                              raw_fixtures=raw_fixtures(), **kwargs)

    def test_reading_empty_journal_never_creates_a_database(self):
        self.assertEqual(journal.list_entries(self.path), [])
        self.assertEqual(journal.reconcile(path=self.path)['entries'], 0)
        self.assertFalse(self.path.exists())

    def test_new_archive_attests_all_available_methods_and_exact_proposal_union(self):
        import scenario_proposal_archive as proposals
        saved = self.record(metadata={'unavailable': {'catboost': 'Modèle absent.'}})
        info = proposals.validate_manifest(saved['methods'], saved['proposal_manifest'])
        expected = {tuple(row['sequence']) for method in self.methods.values() for row in method['ranking']}
        self.assertEqual(info['distinct_proposal_count'], len(expected))
        self.assertEqual(info['proposal_counts'], {name: len(value['ranking']) for name,value in self.methods.items()})
        self.assertEqual(info['policy'], 'top3_per_method')
        self.assertEqual(info['unavailable'], {'catboost': 'Modèle absent.'})
        self.assertEqual(journal.list_entries(self.path)[0]['proposal_manifest'], info)

    def test_first_forecast_reuse_preserves_its_original_proposal_manifest(self):
        first = self.record()
        again = self.record()
        self.assertFalse(again['created'])
        self.assertEqual(first['methods'], again['methods'])
        self.assertEqual(first['proposal_manifest'], again['proposal_manifest'])

    def test_automatic_multi_method_union_survives_archive_export_verdict_table_and_simulation(self):
        import scenario_tables
        import scenario_outcomes
        import bet_simulation
        saved = self.record()
        choices = {tuple(row['sequence']) for method in self.methods.values() for row in method['ranking']}
        club_choices = {tuple(row['sequence']) for row in self.methods['clubs']['ranking']}
        self.assertGreater(len(choices), 3)
        actual = next(iter(sorted(choices-club_choices)))
        raw = raw_fixtures(status='FT')
        for item,label in zip(raw,actual):
            ht,ft = {'1st': ((1,0),(1,0)), '2nd': ((0,0),(1,0)), 'Égal': ((0,0),(0,0))}[label]
            item['score'] = dict(halftime=dict(home=ht[0],away=ht[1]),fulltime=dict(home=ft[0],away=ft[1]))
        journal.reconcile(raw,now=AFTER,path=self.path)
        exported = json.loads(json.dumps(journal.list_entries(self.path)[0]))
        verdict = scenario_outcomes.summarize(exported)
        self.assertEqual(verdict['state'],'hit')
        self.assertNotIn('clubs',{item['method'] for item in verdict['matches']})
        self.assertFalse(exported['evaluations']['clubs']['top3'])
        matrix = scenario_tables.build_matrix(exported['methods'],actual=exported['actual'])
        self.assertEqual({row['sequence'] for row in matrix['rows']},choices)
        self.assertTrue(matrix['actual_in_choices'])
        simulation = bet_simulation.build([exported],now=AFTER)
        self.assertEqual({tuple(row['sequence']) for row in simulation['tickets']},choices)
        self.assertEqual(simulation['summary']['won'],1)
        self.assertEqual(exported['proposal_manifest'],saved['proposal_manifest'])

    def test_group_lookup_reads_only_its_indexed_forecast_and_latest_revisions(self):
        first = self.record()
        journal.reconcile(raw_fixtures(status='FT'), now=AFTER, path=self.path)
        expected = journal.list_entries(self.path)[0]
        with patch.object(journal, 'list_entries', side_effect=AssertionError('No full journal scan')):
            self.assertEqual(journal.find(fixtures(), self.path), expected)
            self.assertEqual(journal.find_mixed(fixtures(), self.path), expected)
            # An existing first forecast must also be reused through the index.
            reused = self.record(metadata={'selected_engine': 'catboost'})
            self.assertFalse(reused['created'])
            self.assertEqual(reused['id'], first['id'])
            self.assertEqual(reused['result_history'], expected['result_history'])

    def test_new_forecast_commit_does_not_reread_the_whole_journal(self):
        with patch.object(journal, 'list_entries', side_effect=AssertionError('No full journal scan')):
            first = self.record()
            self.assertTrue(first['created'])
            self.assertEqual(journal.find(fixtures(), self.path)['methods'], first['methods'])

    def test_group_lookup_does_not_cache_missing_or_old_result_states(self):
        self.assertIsNone(journal.find(fixtures(), self.path))
        self.assertFalse(self.path.exists())
        first = self.record()
        self.assertEqual(journal.find(fixtures(), self.path)['status'], 'pending')
        journal.reconcile(raw_fixtures(status='FT'), now=AFTER, path=self.path)
        settled = journal.find(fixtures(), self.path)
        self.assertEqual(settled['id'], first['id'])
        self.assertEqual(settled['status'], 'settled')
        self.assertEqual(len(settled['result_history']), 1)

    def test_group_lookup_still_rejects_a_tampered_forecast(self):
        self.record()
        with sqlite3.connect(self.path) as con:
            con.execute('DROP TRIGGER predictions_no_update')
            con.execute("UPDATE predictions SET payload='{}'")
        with self.assertRaises(journal.JournalError):
            journal.find(fixtures(), self.path)
        with self.assertRaises(journal.JournalError):
            journal.find_mixed(fixtures(), self.path)

    def test_reconciliation_uses_only_tracked_ids_in_explicit_fixture_store(self):
        self.record()
        source = self.path.parent / 'fixtures.json'
        raw = raw_fixtures(status='FT')
        journal.api.save_fixtures(raw, source)
        with patch.object(journal.api, 'load_fixtures', wraps=journal.api.load_fixtures) as load:
            result = journal.reconcile(now=AFTER, path=self.path, fixtures_path=source)
        load.assert_called_once_with(source, ids=sorted({r['match_id'] for r in fixtures()}, key=int))
        self.assertEqual(result['settled'], 1)
        self.assertEqual(journal.find(fixtures(), self.path)['status'], 'settled')

    def test_first_forecast_locked_across_reruns_models_and_generations(self):
        first = self.record()
        self.assertTrue(first['created'])
        second = self.record(metadata={'selected_engine':'catboost'})
        self.assertFalse(second['created'])
        self.assertEqual(second['methods'], first['methods'])
        self.assertEqual(second['created_at'], first['created_at'])
        self.assertEqual(len(journal.list_entries(self.path)), 1)
        with sqlite3.connect(self.path) as con:
            with self.assertRaises(sqlite3.IntegrityError): con.execute('DELETE FROM predictions')

    def test_after_kickoff_same_day_and_offset_guards(self):
        for now in (AFTER, datetime(2025,3,20,12,tzinfo=timezone.utc)):
            with self.assertRaises(journal.JournalError):
                journal.record(fixtures(), self.methods, now=now, path=self.path, raw_fixtures=raw_fixtures())
        self.assertFalse(self.path.exists())

    def test_live_missing_changed_kickoff_and_scores_rejected_before_commit(self):
        for kind in ('live','missing','kickoff','team'):
            raw = raw_fixtures()
            if kind == 'live': raw[0]['fixture']['status']['short'] = '1H'
            elif kind == 'missing': raw.pop()
            elif kind == 'kickoff': raw[0]['fixture']['date'] = '2025-03-20T15:00:00Z'
            else: raw[0]['teams']['home']['id'] = 999999
            with self.subTest(kind=kind), self.assertRaises(journal.JournalError):
                journal.record(fixtures(), self.methods, now=BEFORE, path=self.path, raw_fixtures=raw)
        scored = fixtures(); scored[0]['ht']='1-0'
        with self.assertRaises(journal.JournalError): journal.record(scored,self.methods,now=BEFORE,path=self.path)
        self.assertFalse(self.path.exists())

    def test_frozen_probabilities_evaluated_and_corrected_without_retraining(self):
        first = self.record()
        raw = raw_fixtures(status='FT')
        with patch.object(ml, 'forecast', side_effect=AssertionError('No later inference')):
            result = journal.reconcile(raw, now=AFTER, path=self.path)
        self.assertEqual(result['settled'], 1)
        evaluated = journal.list_entries(self.path)[0]
        self.assertEqual(evaluated['actual'], ['Égal']*4)
        self.assertEqual(evaluated['methods'], first['methods'])
        self.assertEqual(journal.reconcile(raw,now=AFTER,path=self.path)['changed'],0)
        raw[0]['score']['halftime']=dict(home=0,away=0)
        journal.reconcile(raw,now=AFTER,path=self.path)
        corrected = journal.list_entries(self.path)[0]
        self.assertEqual(len(corrected['result_history']), 2)
        self.assertEqual(corrected['actual'][0], '2nd')
        self.assertEqual(corrected['methods'], first['methods'])
        self.assertEqual(len(journal.summarize(path=self.path)),3)
        self.assertTrue(all(r['tested']==1 for r in journal.summarize(path=self.path)))

    def test_final_with_overtime_uses_only_regulation_and_incomplete_scores_wait(self):
        self.record()
        raw = raw_fixtures(status='AET')
        for row in raw: row['goals'] = dict(home=4,away=4)
        journal.reconcile(raw,now=AFTER,path=self.path)
        self.assertEqual(journal.list_entries(self.path)[0]['actual'],['Égal']*4)
        raw[0]['score']['halftime']=dict(home=None,away=None)
        journal.reconcile(raw,now=AFTER,path=self.path)
        self.assertEqual(journal.list_entries(self.path)[0]['status'],'unresolved')
        self.assertEqual(journal.summarize(path=self.path),[])

    def test_cancelled_postponed_missing_and_rescheduled_groups_are_not_hits(self):
        self.record()
        for status, expected in [('CANC','void'),('PST','unresolved'),('1H','pending')]:
            raw=raw_fixtures(status='FT'); raw[0]['fixture']['status']['short']=status
            journal.reconcile(raw,now=AFTER,path=self.path)
            self.assertEqual(journal.list_entries(self.path)[0]['status'],expected)
            self.assertEqual(journal.summarize(path=self.path),[])
        raw=raw_fixtures(status='FT'); raw[0]['fixture']['date']='2025-03-22T12:00:00Z'
        journal.reconcile(raw,now=AFTER,path=self.path)
        self.assertEqual(journal.list_entries(self.path)[0]['status'],'unresolved')

    def test_old_nonfinal_status_cannot_undo_a_settled_result(self):
        self.record(); journal.reconcile(raw_fixtures(status='FT'),now=AFTER,path=self.path)
        self.assertEqual(journal.reconcile(raw_fixtures(),now=AFTER,path=self.path)['changed'],0)
        self.assertEqual(journal.list_entries(self.path)[0]['status'],'settled')

    def test_no_other_source_or_mixed_competition_can_be_recorded(self):
        with self.assertRaises(journal.JournalError): self.record(source='football-data.org')
        chosen=fixtures();chosen[0]['league']='61'
        with self.assertRaises(journal.JournalError): journal.record(chosen,self.methods,now=BEFORE,path=self.path)

    def test_paired_comparisons_exclude_groups_with_missing_methods(self):
        self.record();journal.reconcile(raw_fixtures(status='FT'),now=AFTER,path=self.path)
        self.assertEqual(journal.summarize(path=self.path,required_methods=['combined','catboost']),[])
        paired=journal.summarize(path=self.path,required_methods=['clubs','combined'])
        self.assertEqual(len(paired),2)
        self.assertEqual(paired[0]['group_ids'],paired[1]['group_ids'])

    def test_context_settles_from_frozen_json_without_backfilling_older_forecasts(self):
        older = self.record()
        methods = dict(copy.deepcopy(self.methods), context_neighbors=context_method())
        methods['context_neighbors']['training_before'] = BEFORE.date().isoformat()
        existing = journal.record(fixtures(), methods, now=BEFORE, path=self.path,
                                  raw_fixtures=raw_fixtures())
        self.assertFalse(existing['created'])
        self.assertNotIn('context_neighbors', existing['methods'])
        chosen = fixtures()
        for row in chosen: row['match_id'] = str(int(row['match_id'])+100)
        with patch.object(ml, 'load_report', return_value=None), patch.object(models, '_context_neighbors',
                side_effect=models.SnapshotError('Configuration par contexte absente.')):
            methods = dict(models.build_methods(sample(), chosen, decision_at=BEFORE)['methods'],
                           context_neighbors=copy.deepcopy(methods['context_neighbors']))
        newer = journal.record(chosen, methods, now=BEFORE, path=self.path,
                               raw_fixtures=raw_fixtures(chosen))
        frozen = copy.deepcopy(newer['methods']['context_neighbors'])
        expected = models.probability_for_sequence(frozen, ['Égal']*4)
        methods['context_neighbors']['distribution']['eta'] = 0
        with patch.object(ml, 'forecast', side_effect=AssertionError('No later model inference')), \
             patch.object(models, '_context_neighbors', side_effect=AssertionError('No later context inference')):
            journal.reconcile(raw_fixtures(status='FT')+raw_fixtures(chosen, 'FT'), now=AFTER, path=self.path)
        entries = {e['id']: e for e in journal.list_entries(self.path)}
        self.assertEqual(entries[older['id']]['methods'], older['methods'])
        evaluated = entries[newer['id']]
        self.assertEqual(evaluated['methods']['context_neighbors'], frozen)
        self.assertAlmostEqual(evaluated['evaluations']['context_neighbors']['probability'], expected)
        paired = journal.summarize(path=self.path, required_methods=['clubs','context_neighbors'])
        self.assertEqual({r['method'] for r in paired}, {'clubs','context_neighbors'})
        self.assertEqual({r['tested'] for r in paired}, {1})
        self.assertTrue(all(r['group_ids'] == [newer['id']] for r in paired))

    def test_concurrent_first_records_and_results_do_not_duplicate_entries(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            recorded=list(pool.map(lambda _: self.record(),range(8)))
        self.assertEqual(sum(row['created'] for row in recorded),1)
        self.assertEqual(len(journal.list_entries(self.path)),1)
        with ThreadPoolExecutor(max_workers=4) as pool:
            reconciled=list(pool.map(lambda _:journal.reconcile(raw_fixtures(status='FT'),now=AFTER,path=self.path),range(4)))
        self.assertEqual(sum(row['changed'] for row in reconciled),1)
        self.assertEqual(len(journal.list_entries(self.path)[0]['result_history']),1)

    def test_kickoff_crossed_during_commit_rolls_back_the_prediction(self):
        times=iter([BEFORE, BEFORE, AFTER])
        with patch.object(journal,'_now',side_effect=lambda now=None:next(times)):
            with self.assertRaises(journal.JournalError):self.record()
        self.assertEqual(journal.list_entries(self.path),[])


if __name__ == '__main__': unittest.main()
