from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import fixture_store
import scenario_features
import causal_feature_workspace as workspace
import causal_feature_artifacts as artifacts
from local_recovery import fingerprint
from test_api_football import fixture


DECISION='2026-10-29T14:00:00+00:00'
OUTCOME='2026-10-30T01:00:00+00:00'


def target(mid=100001,*,status='NS'):
    value=fixture(mid,status=status,season=2026)
    value['fixture']['date']='2026-10-29T16:00:00+00:00'
    return value


def history():
    values=[]
    for index in range(36):
        value=fixture(index+1,season=2026)
        value['fixture']['date']=(datetime(2026,10,1,tzinfo=timezone.utc)+timedelta(days=index//3,hours=12)).isoformat()
        first,final=(((0,0),(1,0)),((1,0),(1,1)),((1,0),(1,0)))[index%3]
        value['score']['halftime']=dict(zip(('home','away'),first))
        value['score']['fulltime']=dict(zip(('home','away'),final))
        values.append(value)
    return values


class CausalWorkspaceTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.path=self.root/'fixtures.json'
        patcher=patch.object(api.ApiFootballClient,'get',side_effect=AssertionError('No API'))
        self.network=patcher.start();self.addCleanup(patcher.stop)
        self.save(history()+[target(),target(100002)],'2026-10-28T12:00:00+00:00')
        fixture_store.migrate_json(self.path)

    def save(self,values,stamp):
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return datetime.fromisoformat(stamp).astimezone(tz)
        with patch.object(api,'datetime',Clock),patch.object(fixture_store,'datetime',Clock):
            api.save_fixtures(values,self.path)

    def prepare(self,requests=None):
        return workspace.prepare_cohort(self.path,requests or [dict(match_id='100001',decision_at=DECISION)])

    def settle(self,*,mid=100001):
        value=target(mid,status='FT')
        value['score']['halftime']={'home':1,'away':0};value['score']['fulltime']={'home':2,'away':2}
        self.save([value],'2026-10-30T00:00:00+00:00')

    def test_inference_and_cohort_preparation_use_identical_source_bound_features(self):
        before=fingerprint(fixture_store.database_path(self.path))
        single,context=workspace.prepare_snapshot(self.path,'100001',decision_at=DECISION)
        cohort,snapshots=self.prepare()
        snapshot=snapshots[cohort['entries'][0]['snapshot_id']]
        self.assertEqual(snapshot,single)
        self.assertEqual(snapshot['features'],scenario_features.feature_for_fixture(snapshot['history_rows'],snapshot['fixture'],before='2026-10-29'))
        self.assertEqual(len(snapshot['source_refs']),36)
        self.assertEqual(snapshot['features']['league_count'],36)
        self.assertEqual(fingerprint(fixture_store.database_path(self.path)),before)
        self.assertFalse(cohort['active_model_changed'])
        self.network.assert_not_called()

    def test_future_corrected_scores_and_target_results_do_not_change_prior_cohort_or_features(self):
        first,snapshots=self.prepare()
        changed=history()[0];changed['score']['halftime']={'home':5,'away':0};changed['score']['fulltime']={'home':5,'away':0}
        self.save([changed],'2026-10-30T00:00:00+00:00');self.settle()
        later,later_snapshots=self.prepare()
        self.assertEqual(later_snapshots,snapshots)
        self.assertEqual(later,first)

    def test_target_receipt_and_status_are_required_not_reconstructed_from_a_final_result(self):
        cohort,snapshots=self.prepare([dict(match_id='100001',decision_at='2026-10-28T10:00:00Z'),
                                        dict(match_id='999999',decision_at=DECISION)])
        self.assertEqual(cohort['included'],0);self.assertFalse(snapshots)
        self.assertTrue(all(item['reason']=='target_availability_unproven' for item in cohort['exclusions']))
        self.settle()
        cohort,_=self.prepare([dict(match_id='100001',decision_at='2026-10-30T00:30:00Z')])
        self.assertEqual(cohort['exclusions'][0]['reason'],'target_not_upcoming')

    def test_duplicate_fixture_and_future_decision_are_refused(self):
        with self.assertRaises(ValueError):
            self.prepare([dict(match_id='100001',decision_at=DECISION)]*2)
        with self.assertRaises(ValueError):
            self.prepare([dict(match_id='100001',decision_at='2099-01-01T00:00:00Z')])
        with self.assertRaises(ValueError):
            workspace.prepare_snapshot(self.path,'100001',decision_at='2099-01-01T00:00:00Z')

    def test_labels_are_joined_later_without_recomputing_or_selecting_feature_inputs(self):
        cohort,snapshots=self.prepare();before=deepcopy((cohort,snapshots))
        pending=workspace.attach_outcomes(self.path,cohort,snapshots,outcome_at=OUTCOME)
        self.assertEqual(pending['evaluated'],0);self.assertEqual(pending['not_evaluable'],1)
        self.settle()
        result=workspace.attach_outcomes(self.path,cohort,snapshots,outcome_at=OUTCOME)
        self.assertEqual(result['evaluated'],1)
        self.assertEqual(result['examples'][0]['label'],'2nd')
        self.assertEqual(result['examples'][0]['features'],next(iter(snapshots.values()))['features'])
        self.assertEqual((cohort,snapshots),before)
        artifacts.validate_outcomes(result,cohort,snapshots)

    def test_labelled_features_are_accepted_in_the_existing_training_pool_schema_without_fit(self):
        import catboost_scenarios as ml
        cohort,snapshots=self.prepare();self.settle()
        result=workspace.attach_outcomes(self.path,cohort,snapshots,outcome_at=OUTCOME)
        snapshot=next(iter(snapshots.values()));schema=snapshot['feature_schema']
        with patch.object(ml,'_fit',side_effect=AssertionError('No model fitting')):
            pool=ml._pool([item['features'] for item in result['examples']],schema['columns'],
                          schema['categorical_columns'],[item['label'] for item in result['examples']])
        self.assertEqual(pool.num_row(),1)
        self.assertEqual(pool.get_feature_names(),schema['columns'])

    def test_schedule_identity_and_missing_halftime_never_manufacture_a_training_label(self):
        cohort,snapshots=self.prepare()
        shifted=target(status='FT');shifted['fixture']['date']='2026-10-29T17:00:00Z'
        self.save([shifted],'2026-10-30T00:00:00+00:00')
        result=workspace.attach_outcomes(self.path,cohort,snapshots,outcome_at=OUTCOME)
        self.assertEqual(result['exclusions'][0]['reason'],'identity_or_schedule_changed')
        self.assertFalse(result['examples'])

    def test_final_observed_before_kickoff_cannot_be_a_later_training_label(self):
        cohort,snapshots=self.prepare()
        value=target(status='FT')
        self.save([value],'2026-10-29T15:00:00+00:00')
        result=workspace.attach_outcomes(self.path,cohort,snapshots,outcome_at=OUTCOME)
        self.assertFalse(result['examples'])
        self.assertEqual(result['exclusions'][0]['reason'],'result_before_kickoff')

    def test_premature_final_is_excluded_from_older_history_and_keeps_other_rows(self):
        value=fixture(700,season=2026);value['fixture']['date']='2026-10-28T16:00:00Z'
        self.save([value],'2026-10-28T15:00:00+00:00')
        snapshot,context=workspace.prepare_snapshot(self.path,'100001',decision_at=DECISION)
        self.assertEqual(len(snapshot['history_rows']),36)
        self.assertIn(dict(match_id='700',reason='premature_final_observation'),context['history_quality_exclusions'])

    def test_resealed_manifest_cannot_swap_decisions_or_snapshot_ids_before_labels(self):
        cohort,snapshots=self.prepare();cohort['entries'][0]['decision_at']='2026-10-29T13:00:00Z'
        cohort['id']=workspace.digest({key:value for key,value in cohort.items() if key!='id'})
        with self.assertRaises(ValueError):workspace.attach_outcomes(self.path,cohort,snapshots,outcome_at=OUTCOME)

    def test_resealed_target_metadata_cannot_detach_the_saved_raw_observation(self):
        from prematch_feature_snapshot import validate
        cohort,snapshots=self.prepare();snapshot=deepcopy(next(iter(snapshots.values())))
        self.assertIn('target_observation',snapshot)
        snapshot['fixture']['home']='Different club name'
        snapshot['features']['home_name']='Different club name'
        snapshot['id']=workspace.digest({key:value for key,value in snapshot.items() if key!='id'})
        with self.assertRaises(ValueError):validate(snapshot)

    def test_target_raw_scores_are_evidence_only_and_never_become_features(self):
        first,_=workspace.prepare_snapshot(self.path,'100001',decision_at=DECISION)
        changed=target();changed['score']['halftime']={'home':99,'away':99}
        changed['score']['fulltime']={'home':199,'away':199}
        self.save([changed],'2026-10-28T13:00:00+00:00')
        second,_=workspace.prepare_snapshot(self.path,'100001',decision_at=DECISION)
        self.assertEqual(first['features'],second['features'])
        self.assertEqual(first['references'],second['references'])
        self.assertNotEqual(first['target_ref'],second['target_ref'])

    def test_bundle_is_exclusive_self_contained_and_detects_resealed_variable_changes(self):
        cohort,snapshots=self.prepare();self.settle()
        result=workspace.attach_outcomes(self.path,cohort,snapshots,outcome_at=OUTCOME)
        directory=self.root/'bundle';artifacts.write_bundle(directory,cohort,snapshots,result)
        self.path.unlink();fixture_store.database_path(self.path).unlink()
        self.assertEqual(artifacts.load_bundle(directory)['manifest'],cohort)
        with self.assertRaises(FileExistsError):
            artifacts.write_bundle(directory,cohort,snapshots,result)
        sid=next(iter(snapshots));corrupt=deepcopy(snapshots[sid]);corrupt['features']['league_count']=999
        corrupt['id']=workspace.digest({key:value for key,value in corrupt.items() if key!='id'})
        with self.assertRaises(ValueError):
            artifacts.validate_cohort(cohort,{sid:corrupt})
        (directory/'snapshots'/f'{sid}.json').write_text('{}')
        with self.assertRaises(ValueError):artifacts.load_bundle(directory)

    def test_incomplete_bundle_and_duplicate_label_denominators_are_refused(self):
        directory=self.root/'incomplete';directory.mkdir()
        with self.assertRaises(OSError):artifacts.load_bundle(directory)
        cohort,snapshots=self.prepare();self.settle()
        result=workspace.attach_outcomes(self.path,cohort,snapshots,outcome_at=OUTCOME)
        result['examples']*=2;result['evaluated']=2
        result['id']=workspace.digest({key:value for key,value in result.items() if key!='id'})
        with self.assertRaises(ValueError):artifacts.validate_outcomes(result,cohort,snapshots)

    def test_resealed_version_quality_and_fixture_counts_cannot_upgrade_the_cohort(self):
        cohort,snapshots=self.prepare()
        for key,value in (('version','unknown-v99'),('temporal_quality','prospective_proven'),('unique_fixtures',99)):
            changed=deepcopy(cohort);changed[key]=value
            changed['id']=workspace.digest({name:item for name,item in changed.items() if name!='id'})
            with self.subTest(key=key),self.assertRaises(ValueError):artifacts.validate_cohort(changed,snapshots)

    def test_resealed_outcomes_cannot_change_provider_or_time_quality(self):
        cohort,snapshots=self.prepare();self.settle()
        original=workspace.attach_outcomes(self.path,cohort,snapshots,outcome_at=OUTCOME)
        for key,value in (('version','unknown-v99'),('temporal_quality','prospective_proven')):
            changed=deepcopy(original);changed[key]=value
            changed['id']=workspace.digest({name:item for name,item in changed.items() if name!='id'})
            with self.subTest(key=key),self.assertRaises(ValueError):artifacts.validate_outcomes(changed,cohort,snapshots)
        changed=deepcopy(original);row=changed['examples'][0];row['outcome_row']['provider']='other'
        row['outcome_source']['normalized_sha256']=workspace.digest(row['outcome_row'])
        changed['id']=workspace.digest({name:item for name,item in changed.items() if name!='id'})
        with self.assertRaises(ValueError):artifacts.validate_outcomes(changed,cohort,snapshots)


if __name__=='__main__':unittest.main()
