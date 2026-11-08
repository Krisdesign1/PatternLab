"""Point-in-time qualification of a frozen proposal-selection policy."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from math import nextafter
from unittest.mock import patch
import unittest

import adaptive_scenario_calibration as calibration
import prospective_models as models

UTC = timezone.utc
START = datetime(2026, 10, 1, tzinfo=UTC)
AS_OF = START + timedelta(days=10)


def method(probabilities=(.7, .2, .1), *, generation=None):
    distribution = models._distribution('independent', 2,
        distributions=[dict(zip(models.VALUES, probabilities)) for _ in range(2)])
    return models._method('calibration-test-v1', distribution, {}, generation=generation)


def forecasts(*, days=9, per_day=10, payload=None):
    payload = payload or method()
    rows = []
    for day in range(days):
        for index in range(per_day):
            identifier = day*per_day+index+1
            kickoff = START+timedelta(days=day,hours=12)
            rows.append(dict(id=f'forecast-{identifier:04}',source='API-Football',size=2,
                created_at=(kickoff-timedelta(hours=4)).isoformat(),
                fixtures=[dict(match_id=str(2*identifier),timestamp=kickoff.isoformat()),
                          dict(match_id=str(2*identifier+1),timestamp=(kickoff+timedelta(hours=1)).isoformat())],
                methods={'clubs':deepcopy(payload)},
                result_history=[dict(checked_at=(kickoff+timedelta(hours=4)).isoformat(),
                                     status='settled',actual=deepcopy(payload['ranking'][0]['sequence']))]))
    return rows


def context(artifact):
    if len(artifact['contexts']) != 1:
        raise AssertionError(f"Expected one context, found {len(artifact['contexts'])}")
    return next(iter(artifact['contexts'].values()))


class AdaptiveScenarioCalibrationTests(unittest.TestCase):
    def test_threshold_is_fitted_on_earlier_days_and_qualified_later(self):
        rows = forecasts(); before=deepcopy(rows)
        artifact = calibration.fit(rows,as_of=AS_OF)
        result = context(artifact)
        self.assertEqual(result['status'],'qualified')
        self.assertEqual((result['tuning_groups'],result['test_groups']),(60,30))
        self.assertEqual((result['tuning_days'],result['test_days']),(6,3))
        self.assertEqual(result['threshold'],rows[0]['methods']['clubs']['ranking'][0]['probability'])
        self.assertEqual(result['test']['proposals'],30)
        self.assertEqual(result['reference_top3']['proposals'],90)
        self.assertEqual(result['test']['hits'],30)
        self.assertEqual(result['test']['precision'],1)
        self.assertEqual(rows,before)
        saved=artifact.pop('sha256')
        self.assertEqual(saved,calibration.digest(artifact))
        self.assertEqual(artifact['protocol']['calendar_timezone'],'Africa/Kinshasa')

    def test_test_outcomes_never_choose_a_different_threshold(self):
        rows=forecasts(); qualified=context(calibration.fit(rows,as_of=AS_OF))
        for row in rows[60:]: row['result_history'][0]['actual']=['2nd','2nd']
        result=context(calibration.fit(rows,as_of=AS_OF))
        self.assertEqual(result['threshold'],qualified['threshold'])
        self.assertEqual(result['tuning'],qualified['tuning'])
        self.assertEqual(result['status'],'not_qualified')
        self.assertEqual(result['test']['hits'],0)

    def test_labels_at_the_first_test_forecast_are_not_available_to_tuning(self):
        rows=forecasts()
        rows[59]['result_history'][0]['checked_at']=rows[60]['created_at']
        artifact=calibration.fit(rows,as_of=AS_OF); result=context(artifact)
        self.assertEqual(result['tuning_groups'],59)
        self.assertEqual(result['test_groups'],30)
        self.assertEqual(artifact['exclusions']['result_unavailable_at_phase_boundary'],1)
        self.assertEqual(result['tuning_labels_before'],rows[60]['created_at'])

    def test_revisions_after_the_boundary_cannot_correct_tuning_truth(self):
        rows=forecasts()
        later=dict(rows[0]['result_history'][0],checked_at=rows[70]['created_at'],actual=['2nd','2nd'])
        rows[0]['result_history'].append(later)
        result=context(calibration.fit(rows,as_of=AS_OF))
        self.assertEqual(result['tuning']['hits'],60)
        self.assertEqual(result['tuning_groups'],60)

    def test_latest_eligible_correction_wins_even_with_identical_checked_time(self):
        row=forecasts()[0]
        row['result_history'].append(dict(row['result_history'][0],status='unresolved',actual=None))
        self.assertIsNone(calibration._observation(row,AS_OF))

    def test_tuning_results_reported_before_kickoff_are_excluded(self):
        rows=forecasts()
        rows[0]['result_history'][0]['checked_at']=rows[0]['created_at']
        artifact=calibration.fit(rows,as_of=AS_OF)
        self.assertEqual(context(artifact)['tuning_groups'],59)
        self.assertEqual(artifact['exclusions']['result_unavailable_at_phase_boundary'],1)

    def test_test_results_at_or_after_artifact_creation_are_excluded(self):
        rows=forecasts()
        rows[-1]['result_history'][0]['checked_at']=AS_OF.isoformat()
        rows[-2]['result_history'][0]['checked_at']=(AS_OF+timedelta(seconds=1)).isoformat()
        result=context(calibration.fit(rows,as_of=AS_OF))
        self.assertEqual(result['test_groups'],28)
        self.assertEqual(result['test']['groups'],28)

    def test_overlap_is_removed_before_selecting_winners_or_available_results(self):
        rows=forecasts()
        duplicate=deepcopy(rows[0]); duplicate['id']='duplicate-winner'
        duplicate['created_at']=(datetime.fromisoformat(rows[0]['created_at'])+timedelta(seconds=1)).isoformat()
        rows[0]['result_history']=[]
        artifact=calibration.fit([duplicate,*rows],as_of=AS_OF)
        result=context(artifact)
        self.assertEqual(result['total_disjoint_groups'],90)
        self.assertEqual(result['tuning_groups'],59)
        self.assertEqual(artifact['exclusions']['overlapping_groups'],1)
        self.assertEqual(artifact['exclusions']['result_unavailable_at_phase_boundary'],1)

    def test_fractional_boolean_negative_noncanonical_and_missing_match_ids_are_rejected(self):
        for value in (True,False,2.0,2.5,0,-1,'02','2.0','-1','',None,'２'):
            with self.subTest(value=value):
                rows=forecasts(); rows[0]['fixtures'][0]['match_id']=value
                artifact=calibration.fit(rows,as_of=AS_OF)
                self.assertEqual(artifact['exclusions']['invalid_forecast'],1)
                self.assertEqual(context(artifact)['total_disjoint_groups'],89)
        self.assertEqual(calibration._match_id(123),'123')
        self.assertEqual(calibration._match_id('123'),'123')

    def test_kinshasa_day_keeps_groups_crossing_utc_midnight(self):
        rows=forecasts()
        for row in rows:
            first=datetime.fromisoformat(row['fixtures'][0]['timestamp']).replace(hour=23,minute=30)
            row['fixtures'][0]['timestamp']=first.isoformat()
            row['fixtures'][1]['timestamp']=(first+timedelta(hours=1)).isoformat()
            row['created_at']=(first-timedelta(hours=4)).isoformat()
            row['result_history'][0]['checked_at']=(first+timedelta(hours=4)).isoformat()
        result=context(calibration.fit(rows,as_of=AS_OF))
        self.assertEqual(result['total_disjoint_groups'],90)
        self.assertEqual(result['test_first_day'],'2026-10-08')
        self.assertEqual(result['status'],'qualified')

    def test_small_history_is_explicitly_unavailable_without_threshold(self):
        result=context(calibration.fit(forecasts(days=4),as_of=AS_OF))
        self.assertEqual(result['status'],'unavailable')
        self.assertNotIn('threshold',result)

    def test_resource_failure_never_publishes_a_prefix_or_partial_qualification(self):
        with patch.object(calibration,'_prepare',side_effect=TimeoutError('Budget dépassé')):
            result=context(calibration.fit(forecasts(),as_of=AS_OF))
        self.assertEqual(result['status'],'unavailable')
        self.assertNotIn('threshold',result)
        self.assertNotIn('test',result)

    def test_all_outcomes_is_an_abstention_instead_of_automatic_success(self):
        row=dict(negative_probabilities=[-1/9]*9,true_probability=1/9,top3=True,universe=9)
        measured=calibration._metric([row],1/9)
        self.assertEqual((measured['hits'],measured['proposals'],measured['abstentions']),(0,0,1))
        self.assertEqual(measured['uninformative_groups'],1)
        self.assertEqual(calibration._metric([row],nextafter(1/9,1))['hits'],0)

    def test_uniform_forecasts_are_not_qualified_by_proposing_every_outcome(self):
        result=context(calibration.fit(forecasts(payload=method((1/3,1/3,1/3))),as_of=AS_OF))
        self.assertEqual(result['candidate_thresholds'],2)
        self.assertEqual(result['status'],'not_qualified')
        self.assertEqual(result['test']['hits'],0)
        self.assertEqual(result['test']['proposals'],0)
        self.assertEqual(result['test']['abstentions'],30)

    def test_forecast_after_kickoff_or_unknown_source_does_not_enter_fit(self):
        rows=forecasts(); rows[0]['created_at']=rows[0]['fixtures'][0]['timestamp']
        rows[1]['source']='Other'
        artifact=calibration.fit(rows,as_of=AS_OF)
        self.assertEqual(artifact['exclusions']['invalid_forecast'],2)
        self.assertEqual(context(artifact)['total_disjoint_groups'],88)

    def test_mixed_context_uses_algorithm_block_sizes_and_leaf_artifacts_not_league_map_hash(self):
        leaf=method(); other=deepcopy(leaf)
        other['league']='different'; other['history_fingerprint']='history-at-another-day'
        def mixed(child,generation,indices):
            return dict(version=models.MIXED_VERSION,generation=generation,
                distribution=dict(type='block_product',components=[dict(indices=indices,league='39',method=child)]))
        original=mixed(leaf,'competition-map-1',[0,1]); similar=mixed(other,'competition-map-2',[1,0])
        self.assertEqual(calibration.context_key('clubs',original,2),calibration.context_key('clubs',similar,2))
        changed=deepcopy(similar); changed['distribution']['components'][0]['method']['model_sha256']='another-model'
        self.assertNotEqual(calibration.context_key('clubs',original,2),calibration.context_key('clubs',changed,2))
        changed=deepcopy(similar); changed['distribution']['components'][0]['indices']=[0]
        self.assertNotEqual(calibration.context_key('clubs',original,2),calibration.context_key('clubs',changed,2))
        changed=deepcopy(similar); changed['distribution']['components'][0]['method']['generation']='another-context-config'
        self.assertNotEqual(calibration.context_key('clubs',original,2),calibration.context_key('clubs',changed,2))

    def test_as_of_must_be_timezone_aware(self):
        for value in ('2026-11-07',datetime(2026,11,7)):
            with self.subTest(value=value),self.assertRaises(ValueError):
                calibration.fit([],as_of=value)


if __name__=='__main__': unittest.main()
