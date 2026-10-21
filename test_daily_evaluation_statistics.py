"""Pure replay statistics keep paired groups, denominators and day clusters."""
import copy
from datetime import date, timedelta
import math
import random
import unittest

import daily_evaluation_statistics as stats


def record(day='2025-01-06',size=4,groups=10,*,support=4,reference=3,loss=2):
    return dict(day=day,size=size,groups=groups,support_top1=min(1,support),reference_top1=min(1,reference),
                support_top3=support,reference_top3=reference,
                support_loss_sum=loss*groups*size,reference_loss_sum=loss*groups*size,
                member_sets_equal=True)


class DailyEvaluationStatisticsTests(unittest.TestCase):
    def test_sizes_separate_and_loss_uses_matches_not_group_count(self):
        values=[record(size=2,groups=10,support=4,reference=2),
                record(size=20,groups=5,support=1,reference=0,loss=3)]
        result=stats.summarize_paired(values)
        self.assertEqual([row['size'] for row in result['by_size']],[2,20])
        small,large=result['by_size']
        self.assertEqual(small['matches'],20)
        self.assertEqual(large['matches'],100)
        self.assertEqual(small['support_loss_per_match'],2)
        self.assertEqual(large['support_loss_per_match'],3)
        self.assertEqual(small['delta_top3_pp'],20)
        self.assertEqual(large['delta_top3_pp'],20)
        self.assertNotIn('overall',result)
        self.assertNotIn('top3_rate',result)

    def test_shared_day_groups_stay_together_and_four_weeks_are_required(self):
        rows=[record('2025-01-06'),record('2025-01-07'),record('2025-01-13'),record('2025-01-20')]
        result=stats.summarize_paired(rows)['by_size'][0]
        self.assertEqual((result['days'],result['weeks'],result['groups']),(4,3,40))
        self.assertIsNone(result['interval_top3_pp'])
        self.assertEqual(result['uncertainty_status'],'insufficient')

    def test_calendar_week_boundary_sunday_monday_is_explicit(self):
        rows=[record('2025-01-05'),record('2025-01-06')]
        result=stats.summarize_paired(rows)
        self.assertEqual(result['by_size'][0]['weeks'],2)
        self.assertEqual(result['protocol']['block'],'calendar_week_of_kinshasa_day')

    def test_bootstrap_is_paired_deterministic_and_input_order_independent(self):
        rows=[record((date(2025,1,6)+timedelta(weeks=i)).isoformat(),groups=10,
                     support=2+i%5,reference=4) for i in range(12)]
        before=copy.deepcopy(rows)
        first=stats.summarize_paired(rows)
        random.Random(31).shuffle(rows)
        self.assertEqual(first,stats.summarize_paired(rows))
        self.assertEqual(sorted(rows,key=lambda row:row['day']),before)
        result=first['by_size'][0]
        self.assertEqual(result['uncertainty_status'],'descriptive')
        interval=result['interval_top3_pp']
        self.assertLess(interval['lower'],interval['upper'])
        self.assertLess(interval['lower'],result['delta_top3_pp'])
        self.assertGreater(interval['upper'],result['delta_top3_pp'])

    def test_group_weighted_ratio_does_not_average_weekly_percentages(self):
        rows=[record((date(2025,1,6)+timedelta(weeks=i)).isoformat(),groups=1,support=1,reference=0)
              for i in range(3)]
        rows.append(record('2025-01-27',groups=100,support=0,reference=100))
        result=stats.summarize_paired(rows)['by_size'][0]
        self.assertAlmostEqual(result['delta_top3_pp'],100*(3-100)/103)
        self.assertNotAlmostEqual(result['delta_top3_pp'],50)
        interval=result['interval_top3_pp']
        self.assertIsNotNone(interval)
        self.assertEqual(interval['upper'],100)
        self.assertGreaterEqual(interval['lower'],-100)

    def test_identical_weekly_gains_do_not_produce_degenerate_precision(self):
        rows=[record((date(2025,1,6)+timedelta(weeks=i)).isoformat(),support=4,reference=4)
              for i in range(4)]
        result=stats.summarize_paired(rows)['by_size'][0]
        self.assertEqual(result['delta_top3_pp'],0)
        self.assertIsNone(result['interval_top3_pp'])
        self.assertEqual(result['uncertainty_status'],'no_variation')

    def test_nll_identity_checks_every_day_so_opposite_errors_cannot_cancel(self):
        rows=[record('2025-01-06'),record('2025-01-13')]
        rows[0]['reference_loss_sum']+=1
        rows[1]['support_loss_sum']+=1
        result=stats.summarize_paired(rows)
        summary=result['by_size'][0]
        self.assertEqual(summary['support_loss_per_match'],summary['reference_loss_per_match'])
        self.assertFalse(summary['nll_identity'])
        self.assertAlmostEqual(summary['nll_max_abs_diff_per_match'],1/40)
        self.assertIn('invariance',result['warnings'][0])

    def test_nll_scaled_tolerance_accepts_rounding_only(self):
        rows=[record()]
        rows[0]['reference_loss_sum']+=1e-10
        self.assertTrue(stats.summarize_paired(rows)['by_size'][0]['nll_identity'])
        rows[0]['reference_loss_sum']+=.001
        self.assertFalse(stats.summarize_paired(rows)['by_size'][0]['nll_identity'])

    def test_invalid_membership_duplicate_cohorts_counts_and_losses_rejected(self):
        for mutate in (
            lambda row:row.update(member_sets_equal=False),
            lambda row:row.update(member_sets_equal=1),
            lambda row:row.update(groups=0),lambda row:row.update(groups=True),
            lambda row:row.update(size=1),lambda row:row.update(size=21),
            lambda row:row.update(size=True),lambda row:row.update(support_top3=11),
            lambda row:row.update(support_top3=-1),lambda row:row.update(support_top1=5),
            lambda row:row.update(reference_top1=True),lambda row:row.update(support_loss_sum=math.nan),
            lambda row:row.update(reference_loss_sum=math.inf),lambda row:row.update(support_loss_sum=-1),
            lambda row:row.update(reference_loss_sum=True),lambda row:row.update(day='2025-01-06T00:00:00Z'),
            lambda row:row.pop('reference_top3')):
            value=record();mutate(value)
            with self.subTest(value=value),self.assertRaises(ValueError):stats.summarize_paired([value])
        with self.assertRaisesRegex(ValueError,'deux fois'):stats.summarize_paired([record(),record()])

    def test_empty_result_has_no_fake_rate_or_confidence(self):
        result=stats.summarize_paired([])
        self.assertEqual(result['by_size'],[])
        self.assertEqual(result['protocol']['bootstrap_samples'],1000)
        self.assertEqual(result['protocol']['seed'],20261021)
        self.assertEqual(result['protocol']['verdict'],'descriptive_only_no_model_promotion')
        self.assertTrue(any('19 comparaisons' in warning for warning in result['warnings']))


if __name__=='__main__':unittest.main()
