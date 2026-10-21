import copy
from contextlib import ExitStack
import unittest
from unittest.mock import patch

import daily_evaluation_live as live
import daily_analysis_worker as worker
import api_football as api
import prospective_models as models
import prediction_journal as journal
from test_daily_analysis_panel import entry


def assessed(identifier, size=3, **kwargs):
    value=entry(identifier,size=size,status='settled',**kwargs)
    value['evaluations']={'clubs':dict(top1=True,top3=True,loss=2.4,probability=.09)}
    return value


class DailyLiveTests(unittest.TestCase):
    def test_pending_is_not_failure_and_sizes_and_generations_remain_separate(self):
        old=assessed('old');new=assessed('new');new['methods']['clubs']['generation']='v2'
        other=assessed('other',size=8)
        values=[old,new,other,entry('waiting'),entry('void',status='void')]
        before=copy.deepcopy(values)
        report=live.summarize(values)
        self.assertEqual(report['counts']['settled'],3)
        self.assertEqual(report['counts']['pending'],1)
        self.assertEqual(report['counts']['void'],1)
        self.assertEqual(len(report['by_method_size']),3)
        first=next(row for row in report['by_method_size'] if row['size']==3 and row['generation']=='')
        self.assertEqual((first['evaluated'],first['pending'],first['excluded']),(1,1,1))
        self.assertEqual(first['top1_rate'],1)
        self.assertAlmostEqual(first['loss_per_match'],.8)
        self.assertEqual(values,before)
        self.assertEqual(report['unique_matches'],8)

    def test_post_kickoff_forecasts_and_missing_evaluations_are_not_counted_as_success(self):
        late=assessed('late');late['created_at']='2026-10-21T14:00:00Z'
        missing=entry('missing',status='settled')
        report=live.summarize([late,missing])
        self.assertEqual(report['counts']['invalid'],1)
        self.assertEqual(report['by_method_size'][0]['evaluated'],0)
        self.assertIsNone(report['by_method_size'][0]['top3_rate'])
        self.assertEqual(report['by_method_size'][0]['missing_evaluations'],1)

    def test_only_saved_selections_are_included_and_no_io_or_prediction_runs(self):
        legacy=assessed('legacy');legacy['metadata']={}
        mapped=dict(entry_id='legacy',selection={'policy':'old-policy','data_support_index':70},competitions={})
        with ExitStack() as stack:
            for target,name in [(api,'load_fixtures'),(api,'load_key'),(api.ApiFootballClient,'get'),
                                (models,'build_mixed_methods'),(journal,'record_mixed'),(journal,'reconcile'),
                                (worker,'run_now')]:
                stack.enter_context(patch.object(target,name,side_effect=AssertionError(name)))
            self.assertEqual(live.summarize([legacy])['counts']['total'],0)
            report=live.summarize([legacy],[mapped])
        self.assertEqual(report['counts']['settled'],1)
        self.assertEqual(report['by_method_size'][0]['policy'],'old-policy')


if __name__=='__main__':
    unittest.main()
