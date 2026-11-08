"""Independent causal and identity checks for the enriched research pipeline."""
from datetime import datetime, timedelta, timezone
import copy
import math
import unittest

import halftime_research_features as features
from match_predictor import estimate


def row(mid, day, *, home='10', away='20', league='39', hour=12, first=(1,0), full=(2,1)):
    return dict(match_id=str(mid), league=str(league), provider='api-football',
        timestamp=f'{day}T{hour:02d}:00:00', day=day,
        home='Club '+str(home), away='Club '+str(away), home_id=str(home), away_id=str(away),
        competition='League '+str(league), country='Example', season=int(day[:4]),
        ht=f'{first[0]}-{first[1]}', ft=f'{full[0]}-{full[1]}',
        value='1st' if 2*sum(first)>sum(full) else '2nd' if 2*sum(first)<sum(full) else 'Égal')


def statistics(item, *, observed='2026-10-26T10:00:00+00:00', value=8):
    return dict(observed_at=datetime.fromisoformat(observed).timestamp(),
        home_id=item['home_id'], away_id=item['away_id'],
        periods={
            'first':{'Total Shots':[value,3], 'Shots on Goal':[2,1],
                     'Shots insidebox':[6,2], 'expected_goals':[.4,.2]},
            'second':{'Total Shots':[12,6], 'Shots on Goal':[3,2],
                      'Shots insidebox':[8,4], 'expected_goals':[.7,.4]},
        })


def output(rows, stats=None, **kwargs):
    return list(features.generate(rows, stats or {}, **kwargs))


def index(rows):
    return {item['match_id']:item for item in rows}


def signature(example):
    return {key:example[key] for key in ('frequency','clubs','core','rich','support','history_ids','personalized')}


def leaves(value):
    if isinstance(value,dict):
        return [leaf for item in value.values() for leaf in leaves(item)]
    if isinstance(value,(tuple,list)):
        return [leaf for item in value for leaf in leaves(item)]
    return [value]


class EnrichedFeatureCausalityTests(unittest.TestCase):
    def test_target_scores_labels_and_statistics_do_not_enter_own_features(self):
        past=row(1,'2024-01-01')
        target=row(2,'2024-01-03')
        baseline=index(output([past,target],{'1':statistics(past),'2':statistics(target)}))['2']
        changed=copy.deepcopy(target)
        changed.update(ht='20-0',ft='20-0',value='1st')
        stats={'1':statistics(past),'2':statistics(target,value=999)}
        result=index(output([past,changed],stats))['2']
        self.assertEqual(signature(baseline),signature(result))

    def test_whole_day_emits_before_any_same_day_result_or_rich_update(self):
        prior=row(1,'2024-01-01')
        first=row(2,'2024-01-03',hour=9)
        second=row(3,'2024-01-03',hour=21)
        original=index(output([prior,first,second],{'1':statistics(prior),'2':statistics(first)}))
        mutation=copy.deepcopy(first)
        mutation.update(ht='15-0',ft='15-0',value='1st')
        changed=index(output([prior,mutation,second],{'1':statistics(prior),'2':statistics(first,value=999)}))
        self.assertEqual(signature(original['3']),signature(changed['3']))
        self.assertNotIn('2',leaves(original['3']['history_ids']))

    def test_future_append_does_not_change_earlier_features(self):
        past=row(1,'2024-01-01')
        target=row(2,'2024-01-03')
        future=row(3,'2027-01-01',first=(50,0),full=(50,0))
        a=index(output([past,target],{'1':statistics(past)}))
        b=index(output([future,target,past],{'1':statistics(past),'3':statistics(future,value=1000)}))
        self.assertEqual(signature(a['2']),signature(b['2']))

    def test_explicit_zero_and_missing_have_different_support_information(self):
        past=row(1,'2024-01-01')
        target=row(2,'2024-01-03')
        zero=statistics(past)
        for block in zero['periods'].values():
            for metric in block:block[metric]=[0,0]
        a=index(output([past,target],{}))['2']
        b=index(output([past,target],{'1':zero}))['2']
        self.assertNotEqual(a['rich'],b['rich'])
        self.assertEqual(a['core'],b['core'])

    def test_statistics_from_another_competition_do_not_leak_into_scoped_profile(self):
        prior=row(1,'2024-01-01')
        other=row(3,'2024-01-02',league='61')
        target=row(2,'2024-01-04')
        a=index(output([prior,target],{'1':statistics(prior)}))['2']
        b=index(output([prior,other,target],{'1':statistics(prior),'3':statistics(other,value=999)}))['2']
        self.assertEqual(signature(a),signature(b))

    def test_as_observed_mode_uses_target_specific_information_time(self):
        past=row(1,'2024-01-01')
        target=row(2,'2024-01-03')
        later_stats={'1':statistics(past,observed='2026-10-26T10:00:00+00:00')}
        strict=index(output([past,target],later_stats,mode='stats_as_observed'))['2']
        empty=index(output([past,target],{},mode='stats_as_observed'))['2']
        latest=index(output([past,target],later_stats,mode='retrospective_latest'))['2']
        self.assertEqual(strict['rich'],empty['rich'])
        self.assertNotEqual(strict['rich'],latest['rich'])

    def test_as_observed_allows_measurement_only_after_observation(self):
        past=row(1,'2024-01-01')
        target=row(2,'2024-01-03')
        stats={'1':statistics(past,observed='2024-01-02T00:00:00+00:00')}
        strict=index(output([past,target],stats,mode='stats_as_observed'))['2']
        latest=index(output([past,target],stats,mode='retrospective_latest'))['2']
        self.assertEqual(strict['rich'],latest['rich'])

    def test_recent_window_counts_missing_matches_instead_of_reaching_further_back(self):
        start=datetime(2024,1,1)
        history=[row(i+1,(start+timedelta(days=i)).date().isoformat()) for i in range(11)]
        target=row(12,'2024-01-15')
        first_only={'1':statistics(history[0],value=999)}
        with_old=index(output(history+[target],first_only))['12']
        without=index(output(history+[target],{}))['12']
        self.assertEqual(with_old['rich'],without['rich'])
        self.assertNotIn('1',leaves(with_old['history_ids']))

    def test_conservative_six_hour_delay_prevents_late_previous_day_result(self):
        old=row(1,'2024-01-01',hour=12)
        late=row(2,'2024-01-01',hour=23)
        target=row(3,'2024-01-02',hour=12)
        a=index(output([old,target],{'1':statistics(old)}))['3']
        b=index(output([old,late,target],{'1':statistics(old),'2':statistics(late,value=999)}))['3']
        self.assertEqual(signature(a),signature(b))
        self.assertNotIn('2',leaves(b['history_ids']))

    def test_input_objects_are_not_modified(self):
        rows=[row(1,'2024-01-01'),row(2,'2024-01-03')]
        stats={'1':statistics(rows[0])}
        before=copy.deepcopy((rows,stats))
        output(rows,stats)
        self.assertEqual((rows,stats),before)

    def test_current_clubs_formula_matches_pure_reference_on_causal_history(self):
        start=datetime(2024,1,1)
        rows=[]
        for i in range(45):
            first=(i%3,(i//3)%2)
            full=(first[0]+(i//2)%3,first[1]+(i//4)%2)
            rows.append(row(i+1,(start+timedelta(days=i)).date().isoformat(),
                            home='10' if i%2==0 else '20',away='20' if i%2==0 else '10',
                            first=first,full=full))
        generated=output(rows)
        for example in generated:
            target=next(item for item in rows if item['match_id']==example['match_id'])
            expected=estimate(rows,'39',target['home'],target['away'],target['day'],
                home_id=target['home_id'],away_id=target['away_id'],provider='api-football')
            probabilities=example['clubs']
            if not isinstance(probabilities,dict):
                probabilities=dict(zip(('1st','Égal','2nd'),probabilities))
            for label in ('1st','Égal','2nd'):
                self.assertAlmostEqual(probabilities[label],expected['probabilities'][label],places=11)
            self.assertEqual(example['personalized'],expected['personalized'])

    def test_reversed_roles_preserve_for_against_and_role_specific_missingness(self):
        past=row(1,'2024-01-01')
        target=row(2,'2024-01-03',home='20',away='10')
        value=index(output([past,target],{'1':statistics(past)}))['2']
        self.assertEqual(value['rich']['home_all_first_total_shots_for_mean'],3)
        self.assertEqual(value['rich']['home_all_first_total_shots_against_mean'],8)
        self.assertEqual(value['rich']['away_all_first_total_shots_for_mean'],8)
        self.assertEqual(value['rich']['away_all_first_total_shots_against_mean'],3)
        self.assertEqual(value['rich']['home_home_first_total_shots_for_count'],0)
        self.assertEqual(value['rich']['away_away_first_total_shots_for_count'],0)

    def test_team_mismatch_is_rejected_instead_of_joining_by_match_id_alone(self):
        past=row(1,'2024-01-01')
        target=row(2,'2024-01-03')
        bad=statistics(past)
        bad['home_id']='999'
        with self.assertRaises(ValueError):
            output([past,target],{'1':bad})

    def test_global_observation_cutoff_also_applies_in_latest_mode(self):
        past=row(1,'2024-01-01')
        target=row(2,'2024-01-03')
        stats={'1':statistics(past,observed='2024-01-02T00:00:00+00:00')}
        cutoff=datetime(2024,1,1,tzinfo=timezone.utc)
        result=index(output([past,target],stats,observed_before=cutoff))['2']
        expected=index(output([past,target],{}))['2']
        self.assertEqual(result['rich'],expected['rich'])

    def test_target_does_not_require_own_statistics_for_cohort_membership(self):
        past=row(1,'2024-01-01')
        target=row(2,'2024-01-03')
        generated=index(output([past,target],{'1':statistics(past)}))
        self.assertIn('2',generated)
        self.assertGreater(generated['2']['support']['home_all'],0)

    def test_duplicate_ids_are_deduplicated_and_conflicts_rejected(self):
        past=row(1,'2024-01-01')
        target=row(2,'2024-01-03')
        once=index(output([past,target]))
        twice=index(output([past,copy.deepcopy(past),target]))
        self.assertEqual(once,twice)
        conflicting=copy.deepcopy(past)
        conflicting.update(ht='2-0',ft='2-0',value='1st')
        with self.assertRaises(ValueError):
            output([past,conflicting,target])

    def test_stable_schema_is_numeric_finite_and_has_no_target_id(self):
        rows=[row(1,'2024-01-01'),row(2,'2024-01-03')]
        for example in output(rows,{'1':statistics(rows[0])}):
            self.assertEqual(set(example['core']),set(features.CORE_COLUMNS))
            self.assertEqual(set(example['rich']),set(features.RICH_COLUMNS))
            self.assertNotIn('match_id',example['core'])
            self.assertNotIn('actual',example['rich'])
            self.assertTrue(all(math.isfinite(float(v)) for mapping in (example['core'],example['rich']) for v in mapping.values()))


if __name__=='__main__':unittest.main()
