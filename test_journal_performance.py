"""Prospective summaries never mix group sizes, model versions, or cohorts."""
import copy
from datetime import date
import unittest

import journal_performance as performance


def entry(identifier, *, size=4, league='39', status='settled', automatic=False,
          methods=('clubs', 'combined'), generation='', day='2026-10-18'):
    payloads = {name: dict(version='version-1', generation=generation) for name in methods}
    evaluations = {name: dict(top1=False, top3=True, loss=4.0) for name in methods} if status == 'settled' else {}
    return dict(id=identifier, league=league, size=size, status=status,
                created_at=f'{day}T12:00:00+00:00', methods=payloads, evaluations=evaluations,
                metadata={'selected_engine': 'automation' if automatic else 'statistical'},
                fixtures=[dict(match_id=str(i+1)) for i in range(size)],
                observed=[dict(match_id=str(i+1)) for i in range(size if status == 'settled' else 1)])


class JournalPerformanceTests(unittest.TestCase):
    def test_local_day_uses_every_kickoff_and_never_prediction_creation_time(self):
        rows=[]
        for identity,stamps in (
            ('today',['2026-10-19T23:00:00Z','2026-10-20T22:59:59+00:00']),
            ('tomorrow',['2026-10-20T23:00:00Z']*2),
            ('yesterday',['2026-10-19T22:59:59']*2),
            ('mixed',['2026-10-20T12:00:00','2026-10-20T23:30:00']),
            ('offset',['2026-10-20T00:00:00+01:00']*2),
            ('invalid',['bad','2026-10-20T12:00:00Z']),
            ('empty',[])):
            current=entry(identity,size=2,day='2026-10-18')
            current['fixtures']=[dict(match_id=str(i),timestamp=stamp) for i,stamp in enumerate(stamps)]
            rows.append(current)
        before=copy.deepcopy(rows)
        selected=performance.entries_on_day(rows,date(2026,10,20))
        self.assertEqual([row['id'] for row in selected],['today','offset'])
        self.assertEqual(rows,before)
        with self.assertRaises(ValueError):performance.entries_on_day(rows,'2026-10-20')

    def test_filters_isolate_league_size_and_manual_automatic_origin(self):
        rows = [entry('one'), entry('two', automatic=True), entry('three', size=2), entry('four', league='61')]
        before = copy.deepcopy(rows)
        self.assertEqual([r['id'] for r in performance.filter_entries(rows, '39', 4)], ['one', 'two'])
        self.assertEqual([r['id'] for r in performance.filter_entries(rows, '39', 4, 'manual')], ['one'])
        self.assertEqual([r['id'] for r in performance.filter_entries(rows, '39', 4, 'automatic')], ['two'])
        self.assertEqual(rows, before)
        historical = entry('old')
        historical['metadata'] = {}
        self.assertEqual(performance.origin(historical), 'manual')
        historical['metadata'] = {'automation_policy': 'daily-blocks-v1'}
        self.assertEqual(performance.origin(historical), 'automatic')

    def test_pending_void_and_unresolved_are_visible_without_fake_zero_percent(self):
        rows = [entry('pending', status='pending'), entry('void', status='void'), entry('unclear', status='unresolved')]
        result = performance.comparison(rows, ('clubs', 'combined'))
        self.assertEqual(result['counts'], dict(total=3, settled=0, pending=1, void=1, unresolved=1,
                                               observed_matches=3, expected_matches=12))
        self.assertEqual(result['summary'], [])
        self.assertEqual(result['trend'], [])
        self.assertEqual(result['settled'], [])

    def test_missing_methods_are_excluded_and_every_method_uses_identical_group_ids(self):
        rows = [entry('both'), entry('clubs-only', methods=('clubs',)), entry('awaiting', status='pending')]
        result = performance.comparison(rows, ('clubs', 'combined'))
        self.assertEqual(result['missing_methods'], 1)
        self.assertEqual(result['counts']['pending'], 1)
        self.assertEqual({tuple(r['group_ids']) for r in result['summary']}, {('both',)})
        self.assertEqual({r['tested'] for r in result['summary']}, {1})
        self.assertEqual({r['top3_rate'] for r in result['summary']}, {1.0})

    def test_distinct_generations_require_an_explicit_cohort_and_never_pool_scores(self):
        rows = [entry('first', generation='generation-a'), entry('second', generation='generation-b')]
        with self.assertRaisesRegex(ValueError, 'versions'):
            performance.comparison(rows, ('clubs', 'combined'))
        versions = performance.signatures(rows, ('combined', 'clubs'))
        result = performance.comparison(rows, ('clubs', 'combined'), versions[0])
        self.assertEqual(result['other_versions'], 1)
        self.assertEqual({r['tested'] for r in result['summary']}, {1})
        self.assertEqual({r['generation'] for r in result['summary']}, {'generation-a'})

    def test_partial_results_and_corrections_change_only_current_counts_and_scores(self):
        rows = [entry('one', status='pending')]
        rows[0]['observed'] += [dict(match_id='1'), dict(match_id='2'), dict(match_id='outside')]
        self.assertEqual(performance.counts(rows)['observed_matches'], 2)
        self.assertEqual(performance.comparison(rows, ('clubs',))['summary'], [])
        rows[0]['status'] = 'settled'
        rows[0]['evaluations'] = {'clubs': dict(top1=False, top3=False, loss=6.0),
                                  'combined': dict(top1=True, top3=True, loss=1.0)}
        result = performance.comparison(rows, ('clubs', 'combined'))
        scores = {r['method']: r for r in result['summary']}
        self.assertEqual(scores['clubs']['loss'], 6)
        self.assertEqual(scores['combined']['top1_rate'], 1)

    def test_trend_is_cumulative_over_same_groups_and_only_appears_across_distinct_days(self):
        rows = [entry('first', day='2026-10-17'), entry('second', day='2026-10-18')]
        rows[1]['evaluations']['clubs'] = dict(top1=False, top3=False, loss=6.0)
        result = performance.comparison(rows, ('clubs', 'combined'))
        last = {r['method']: r for r in result['trend'] if r['day'] == '2026-10-18'}
        self.assertEqual({r['tested'] for r in last.values()}, {2})
        self.assertEqual(last['clubs']['top3_rate'], .5)
        self.assertEqual(last['clubs']['loss'], 5)
        self.assertEqual(last['combined']['top3_rate'], 1)
        same_day = [entry('one'), entry('two')]
        self.assertEqual(performance.comparison(same_day, ('clubs',))['trend'], [])

    def test_invalid_comparisons_reject_duplicate_groups_and_mixed_sizes(self):
        for rows in ([entry('duplicate'), entry('duplicate')], [entry('one'), entry('two', size=2)],
                     [entry('one'), entry('two', league='61')]):
            with self.assertRaises(ValueError): performance.comparison(rows, ('clubs',))
        with self.assertRaises(ValueError): performance.comparison([entry('one')], ('clubs', 'clubs'))
        with self.assertRaises(ValueError): performance.filter_entries([], '39', 4, 'unknown')
        self.assertEqual(performance.comparison([entry('one')], ())['summary'], [])


if __name__ == '__main__':
    unittest.main()
