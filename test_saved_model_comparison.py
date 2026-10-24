from copy import deepcopy
from datetime import datetime, timezone
from itertools import product
from math import fsum, log
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from league_model import VALUES
import journal_protocol
import prospective_models as models
import saved_model_comparison as comparison
from scenario_predictor import fit_sequences
from test_probability_reliability import entry as original_entry


def entry(**kwargs):
    value = original_entry(**kwargs)
    value['size'] = 2
    provenance = dict(source='API-Football', league='39', training_before='2026-10-20')
    distribution = value['methods']['clubs']['distribution']
    value['methods'] = {
        'clubs': models._method('clubs-v2', distribution, provenance),
        'frequency': models._method('frequency-v1', models._distribution('frequency', 2,
            fit=fit_sequences([('1st','2nd'), ('2nd','2nd'), ('Égal','1st')], 2)), provenance),
        'catboost': models._method('catboost-v1', dict(distribution,
            distributions=[dict(zip(VALUES, (.1,.2,.7))), dict(zip(VALUES,(.8,.1,.1)))]),
            provenance, generation='fixed-model', trained_through='2026-10-19'),
    }
    return value


class SavedComparisonTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        # The custom isolated path is required for deterministic registration.
        with patch('cloud_storage.configured', return_value=False):
            self.protocol = journal_protocol.register_protocol(Path(temporary.name)/'protocol.json',
                now=datetime(2026,10,20,9,tzinfo=timezone.utc))

    def build(self, entries):
        with patch('api_football.ApiFootballClient', side_effect=AssertionError('No API')), \
                patch('catboost_scenarios.forecast', side_effect=AssertionError('No inference')):
            return comparison.build(entries, self.protocol)

    def test_all_methods_use_identical_matches_and_expected_losses(self):
        value = entry(); before = deepcopy(value)
        cohort = self.build([value])['cohorts'][0]
        self.assertEqual(cohort['evaluated'],2)
        self.assertEqual(cohort['match_ids'], ['1','2'])
        self.assertAlmostEqual(cohort['scores']['clubs']['logloss'],(-log(.5)-log(.6))/2)
        self.assertAlmostEqual(cohort['scores']['catboost']['logloss'],(-log(.7)-log(.8))/2)
        self.assertGreater(cohort['loss_gain_vs_clubs']['catboost'],0)
        for method in comparison.METHODS:
            self.assertEqual(cohort['scores'][method]['evaluated'],2)
            self.assertEqual([row['label'] for row in cohort['scores'][method]['categories']],list(VALUES))
        self.assertEqual(value,before)

    def test_marginals_match_exhaustive_joint_for_frequency_corrected_and_independent(self):
        fit = fit_sequences([('1st','2nd'),('2nd','Égal'),('2nd','2nd')],2)
        base = [dict(zip(VALUES,(.3,.2,.5))),dict(zip(VALUES,(.1,.6,.3)))]
        _,_,normalization = models._normalization(fit,base,models.PRIOR_WEIGHT,.5)
        distributions = [models._distribution('frequency',2,fit=fit),
            models._distribution('independent',2,distributions=base),
            models._distribution('corrected',2,fit=fit,distributions=base,alpha=.5,normalization=normalization),
            dict(schema=models.DISTRIBUTION_VERSION,type='context_mixture',size=2,baseline=base,
                 eta=.25,local_mass=[dict(sequence=['1st','2nd'],probability=1)])]
        for distribution in distributions:
            with self.subTest(kind=distribution['type']):
                actual = comparison.marginals(models._method('test-v1',distribution,{}))
                for index in range(2):
                    for label in VALUES:
                        expected = fsum(models.probability(distribution,sequence)
                                        for sequence in product(VALUES,repeat=2) if sequence[index] == label)
                        self.assertAlmostEqual(actual[index][label],expected)

    def test_twenty_match_marginals_do_not_enumerate_the_joint_outcomes(self):
        base = [dict(zip(VALUES,(.2,.3,.5))) for _ in range(20)]
        method = models._method('test-v1',models._distribution('independent',20,distributions=base),{})
        with patch.object(models,'_rank',return_value=method['ranking']), \
                patch.object(models,'probability',side_effect=AssertionError('No 3**20 enumeration')):
            self.assertEqual(comparison.marginals(method),base)

    def test_overlapping_groups_deduplicate_and_first_pending_is_retained(self):
        first = entry(identity='a',status='pending'); first['observed']=[]
        later = entry(identity='b',created='2026-10-20T11:00:00Z')
        report = self.build([later,first])
        self.assertEqual(report['duplicates_removed'],2)
        self.assertEqual(report['cohorts'][0]['pending'],2)
        self.assertIsNone(report['cohorts'][0]['scores']['clubs']['logloss'])

    def test_missing_catboost_is_explicit_and_never_fabricated(self):
        value = entry(); del value['methods']['catboost']
        report = self.build([value])
        self.assertEqual(report['cohorts'],[])
        self.assertEqual(report['snapshot_exclusions']['missing_catboost'],1)

    def test_before_protocol_postkickoff_foreign_source_and_tampering_are_excluded(self):
        for mutate,reason in (
                (lambda row:row.update(created_at='2026-10-20T08:00:00Z'),'before_protocol'),
                (lambda row:row.update(created_at='2026-10-20T12:00:00Z'),'invalid_snapshot'),
                (lambda row:row.update(source='other'),'invalid_snapshot'),
                (lambda row:row['methods']['catboost'].update(trained_through='2026-10-20'),'invalid_snapshot'),
                (lambda row:row['methods']['clubs']['ranking'][0].update(probability=.999),'invalid_snapshot')):
            value = entry(); mutate(value)
            result = self.build([value])
            self.assertEqual(result['cohorts'],[])
            self.assertEqual(result['snapshot_exclusions'][reason],1)

    def test_versions_generations_sizes_and_competitions_are_never_pooled(self):
        first=entry(); second=entry(identity='new')
        second['methods']['catboost']['generation']='next-model'
        result=self.build([first,second])
        self.assertEqual(len(result['cohorts']),2)
        self.assertEqual(result['duplicates_removed'],0)

    def test_mixed_components_keep_their_real_generations_and_exact_marginals(self):
        value=entry(); value['fixtures'][1]['league']='61'
        base=[dict(zip(VALUES,(.2,.3,.5))),dict(zip(VALUES,(.6,.3,.1)))]
        for method in comparison.METHODS:
            blocks=[]
            for index,fixture in enumerate(value['fixtures']):
                component=models._method(method+'-v1',models._distribution('independent',1,
                    distributions=[base[index]]),dict(source='API-Football',league=fixture['league'],
                    training_before='2026-10-20'),_component=True,generation='first-generation')
                blocks.append(dict(indices=[index],league=fixture['league'],method=component))
            distribution=dict(schema=models.DISTRIBUTION_VERSION,type='block_product',size=2,components=blocks)
            value['methods'][method]=models._method('mixed-container-v1',distribution,
                dict(source='API-Football',provider='api-football',league='mixed',
                     assumes_block_independence=True,fixtures=deepcopy(value['fixtures']),training_before='2026-10-20'))
            self.assertEqual(comparison.marginals(value['methods'][method]),base)
        changed=deepcopy(value); changed['id']='another-generation'
        changed['methods']['catboost']['distribution']['components'][0]['method']['generation']='second-generation'
        report=self.build([value,changed])
        self.assertEqual(len(report['cohorts']),4)
        self.assertEqual(report['duplicates_removed'],0)

    def test_invalid_or_missing_results_do_not_become_failures(self):
        value=entry(status='unresolved'); value['observed'].pop()
        cohort=self.build([value])['cohorts'][0]
        self.assertEqual(cohort['evaluated'],1)
        self.assertEqual(cohort['excluded'],1)
        self.assertEqual(cohort['scores']['catboost']['accuracy'],1)


class SavedFrequencyPairsTests(unittest.TestCase):
    """Supplementary pairs keep frozen forecasts and protocol distinct."""
    setUp = SavedComparisonTests.setUp
    def paired(self, entries, now=None):
        with patch('api_football.ApiFootballClient',side_effect=AssertionError('No API')), \
                patch('catboost_scenarios.forecast',side_effect=AssertionError('No inference')):
            return comparison.build_pairs(entries,self.protocol,
                now=now or datetime(2026,10,24,tzinfo=timezone.utc))

    def weekly_entry(self, index):
        from datetime import timedelta
        target = datetime(2026,10,20,tzinfo=timezone.utc)+timedelta(weeks=index)
        value = entry(identity=f'week-{index}',created=target.replace(hour=10).isoformat())
        for position,fixture in enumerate(value['fixtures']):
            fixture.update(match_id=str(index*2+position+1),
                           timestamp=target.replace(hour=12+position).isoformat())
            value['observed'][position]['match_id']=fixture['match_id']
        value['checked_at']=target.replace(hour=16).isoformat()
        for name,method in value['methods'].items():
            method['training_before']=target.date().isoformat()
            if 'trained_through' in method:
                method['trained_through']=(target-timedelta(days=1)).date().isoformat()
        distributions = [dict(zip(VALUES,(.2,.3,.5))) if index%2 else dict(zip(VALUES,(.7,.2,.1))),
                         dict(zip(VALUES,(.4,.3,.3)))]
        value['methods']['clubs']=models._method('clubs-v2',models._distribution('independent',2,
            distributions=distributions),dict(source='API-Football',league='39',
            training_before=target.date().isoformat()))
        return value

    def test_missing_catboost_does_not_remove_clubs_or_require_all_methods(self):
        value=entry(); del value['methods']['catboost']
        value['methods']['combined']=deepcopy(value['methods']['clubs'])
        value['methods']['combined']['version']='combined-v1'
        before=deepcopy(value); protocol_before=deepcopy(self.protocol)
        report=self.paired([value])
        self.assertEqual({row['challenger'] for row in report['cohorts']},{'clubs','combined'})
        self.assertTrue(all(row['evaluated']==2 for row in report['cohorts']))
        self.assertEqual(report['snapshot_exclusions']['missing_catboost'],1)
        self.assertEqual(value,before); self.assertEqual(self.protocol,protocol_before)
        self.assertEqual(self.protocol['baseline'],'clubs')

    def test_baseline_is_identical_and_calibration_includes_nonfavorites(self):
        cohort=next(row for row in self.paired([entry()])['cohorts'] if row['challenger']=='clubs')
        self.assertEqual(cohort['scores']['frequency']['evaluated'],cohort['scores']['clubs']['evaluated'])
        for method in ('frequency','clubs'):
            for label in VALUES:
                self.assertEqual(sum(row['n'] for row in cohort['calibration'][method][label]),2)
        self.assertEqual(cohort['uncertainty']['status'],'insufficient')
        self.assertEqual(cohort['uncertainty']['blocks'],0)
        self.assertTrue(all(row['lower'] is None for row in cohort['uncertainty']['gains'].values()))

    def test_first_common_pending_is_retained_without_cherry_picking_later_result(self):
        first=entry(identity='a',status='pending'); first['observed']=[]
        later=entry(identity='b',created='2026-10-20T11:00:00Z')
        report=self.paired([later,first])
        self.assertEqual(report['duplicates_removed'],4)
        self.assertTrue(all(row['pending']==2 and row['evaluated']==0 for row in report['cohorts']))

    def test_bad_challenger_does_not_invalidate_other_paired_method(self):
        value=entry(); value['methods']['catboost']['ranking'][0]['probability']=.999
        report=self.paired([value])
        self.assertEqual([row['challenger'] for row in report['cohorts']],['clubs'])
        self.assertEqual(report['snapshot_exclusions']['invalid_catboost'],1)

    def test_time_cutoff_forecast_creation_and_baseline_kind_are_enforced(self):
        mutations=[lambda row:row.update(created_at='2026-10-20T08:00:00Z'),
                   lambda row:row.update(created_at='2026-10-20T12:00:00Z'),
                   lambda row:row.update(created_at='2026-10-25T10:00:00Z'),
                   lambda row:row['methods']['frequency'].update(training_before='2026-10-21'),
                   lambda row:row['methods'].update(frequency=deepcopy(row['methods']['clubs']))]
        for mutate in mutations:
            value=entry(); mutate(value)
            self.assertFalse(self.paired([value])['cohorts'])
        value=entry(); value['checked_at']='2026-10-25T16:00:00Z'
        self.assertTrue(all(row['evaluated']==0 and row['excluded']==2
                            for row in self.paired([value])['cohorts']))

    def test_pair_versions_and_generations_are_separate_without_irrelevant_catboost_fragmentation(self):
        first=entry(); second=entry(identity='other')
        second['methods']['catboost']['generation']='next-generation'
        report=self.paired([second,first])
        self.assertEqual(len(report['cohorts']),3)
        self.assertEqual(len([row for row in report['cohorts'] if row['challenger']=='clubs']),1)
        self.assertEqual(report['duplicates_removed'],2)

    def test_coverage_counts_unique_matches_and_groups_without_multiplying_model_pairs(self):
        first=entry(identity='a',status='pending'); first['observed']=[]
        later=entry(identity='b',created='2026-10-20T11:00:00Z')
        later['methods']['catboost']['generation']='other-generation'
        report=self.paired([later,first])
        coverage=report['coverage']
        self.assertEqual(coverage['unique_matches'],2)
        self.assertEqual(coverage['comparable_groups'],2)
        self.assertEqual(coverage['group_match_appearances'],4)
        self.assertEqual(coverage['repeated_match_appearances'],2)
        self.assertEqual(coverage['overlapping_matches'],2)
        self.assertEqual(coverage['overlapping_groups'],2)
        self.assertEqual(coverage['cohort_match_observations'],6)
        self.assertEqual(coverage['pending_unique_matches'],2)
        self.assertEqual(coverage['evaluated_unique_matches'],0)
        self.assertEqual(coverage['mature_cohorts'],0)
        self.assertEqual(coverage['max_closed_blocks'],0)

    def test_coverage_does_not_count_excluded_groups_or_missing_methods_as_failures(self):
        missing=entry(identity='missing'); missing['methods'].pop('frequency')
        old=entry(identity='old',created='2026-10-20T08:00:00Z')
        report=self.paired([entry(),missing,old])
        self.assertEqual(report['coverage']['comparable_groups'],1)
        self.assertEqual(report['coverage']['unique_matches'],2)
        self.assertEqual(report['coverage']['evaluated_unique_matches'],2)
        self.assertEqual(report['coverage']['excluded_unique_matches'],0)

    def test_closed_weeks_use_paired_cluster_bootstrap_not_iid_match_intervals(self):
        from datetime import timedelta
        values=[self.weekly_entry(i) for i in range(20)]
        current=datetime(2026,10,20,tzinfo=timezone.utc)+timedelta(weeks=21)
        report=self.paired(values,now=current)
        cohort=next(row for row in report['cohorts'] if row['challenger']=='clubs')
        estimate=cohort['uncertainty']
        self.assertEqual(estimate['blocks'],20)
        self.assertEqual(estimate['matches'],40)
        self.assertEqual(estimate['status'],'descriptive')
        self.assertIsNotNone(estimate['gains']['logloss']['lower'])
        self.assertAlmostEqual(estimate['gains']['logloss']['estimate'],
            cohort['scores']['frequency']['logloss']-cohort['scores']['clubs']['logloss'])
        repeated=next(row for row in self.paired(values,now=current)['cohorts'] if row['challenger']=='clubs')
        self.assertEqual(estimate,repeated['uncertainty'])

    def test_twenty_matches_in_one_week_are_one_block_and_pending_week_is_not_ignored(self):
        from datetime import timedelta
        values=[]
        for i in range(10):
            row=self.weekly_entry(0); row['id']=f'same-week-{i}'
            for position,fixture in enumerate(row['fixtures']):
                fixture['match_id']=str(i*2+position+1)
                row['observed'][position]['match_id']=fixture['match_id']
            values.append(row)
        current=datetime(2026,10,27,tzinfo=timezone.utc)
        cohort=self.paired(values,now=current)['cohorts'][0]
        self.assertEqual(cohort['uncertainty']['blocks'],1)
        values[-1].update(status='pending',observed=[])
        cohort=self.paired(values,now=current)['cohorts'][0]
        self.assertEqual(cohort['evaluated'],18)
        self.assertEqual(cohort['uncertainty']['blocks'],0)
        self.assertEqual(cohort['uncertainty']['excluded']['incomplete_block'],20)


if __name__=='__main__':
    unittest.main()
