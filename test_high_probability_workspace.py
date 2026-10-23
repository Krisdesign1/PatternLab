"""Probability search preserves displayed values and pre-match journal guards."""
from copy import deepcopy
from datetime import timedelta
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import daily_analysis_data as daily
import high_probability_engine as engine
import high_probability_workspace as workspace
import prediction_journal as journal
import prospective_models as models
import probability_scenario_store as scenario_store
from test_high_probability_engine import fixture, history, NOW


def raw_targets(leagues=(39, 61)):
    return [fixture(100000+index, league=league,
                    stamp=f'2026-10-22T{15-index:02d}:00:00+00:00')
            for index, league in enumerate(leagues)]


def scan(leagues=(39, 61)):
    raw = [item for index, league in enumerate(sorted(set(leagues)))
           for item in history(league=league, start=1000*(index+1), equal=bool(index % 2))]
    return engine.scan(raw+raw_targets(leagues), NOW)


class HighProbabilityWorkspaceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root/'predictions.sqlite3'
        workspace._cached_scan.cache_clear()
        self.addCleanup(workspace._cached_scan.cache_clear)

    def prepare(self, leagues=(39, 61)):
        report = scan(leagues)
        return workspace.prepare_group(report, [row['fixture']['match_id'] for row in report['rows']], NOW)

    def test_local_scan_cache_reads_only_and_expires_exactly_at_kickoff(self):
        raw = history()+[fixture(100001, stamp='2026-10-22T09:00:30+00:00')]
        api.save_fixtures(raw, self.root/'fixtures.json')
        original = (self.root/'fixtures.json').read_bytes()
        with patch.object(api.ApiFootballClient, 'get', side_effect=AssertionError('No API')), \
                patch.object(journal, 'record_mixed', side_effect=AssertionError('No journal')), \
                patch.object(engine, 'scan', wraps=engine.scan) as calculate:
            report = workspace.load_scan(self.root, NOW)
            report['rows'][0]['probabilities']['2nd'] = 0
            again = workspace.load_scan(self.root, NOW+timedelta(seconds=10))
            self.assertGreater(again['rows'][0]['probabilities']['2nd'], 0)
            self.assertEqual(calculate.call_count, 1)
            expired = workspace.load_scan(self.root, NOW+timedelta(seconds=30))
            self.assertEqual(expired['rows'], [])
            self.assertEqual(calculate.call_count, 1)
        self.assertEqual((self.root/'fixtures.json').read_bytes(), original)
        self.assertFalse(self.path.exists())

    def test_group_reuses_exact_probabilities_and_common_cutoff_with_no_reestimation(self):
        report = scan()
        original = deepcopy(report)
        ids = [row['fixture']['match_id'] for row in report['rows']]
        with patch.object(engine, 'scan', side_effect=AssertionError('No recalculation')), \
                patch.object(models, 'build_mixed_methods', side_effect=AssertionError('No other models')):
            prepared = workspace.prepare_group(report, ids, NOW)
        self.assertEqual(report, original)
        self.assertEqual([row['match_id'] for row in prepared['fixtures']], ['100001', '100000'])
        method = prepared['methods']['clubs']
        models.validate_method(method, 2)
        by_id = {row['fixture']['match_id']: row for row in report['rows']}
        for component in method['distribution']['components']:
            for index, probabilities in zip(component['indices'], component['method']['distribution']['distributions']):
                self.assertEqual(probabilities, by_id[prepared['fixtures'][index]['match_id']]['probabilities'])
            self.assertEqual(component['method']['history_cutoff_exclusive'], report['before'])
        sequence = method['ranking'][0]['sequence']
        expected = math.prod(by_id[row['match_id']]['probabilities'][value]
                             for row, value in zip(prepared['fixtures'], sequence))
        self.assertAlmostEqual(method['ranking'][0]['probability'], expected)
        self.assertLess(sum(row['probability'] for row in method['ranking']), 1)
        self.assertFalse(self.path.exists())

    def test_group_validation_rejects_old_days_expired_unknown_duplicate_and_oversized(self):
        report = scan()
        ids = [row['fixture']['match_id'] for row in report['rows']]
        cases = [(report, [ids[0]], NOW), (report, ids+ids, NOW), (report, ids+['999'], NOW),
                 (report, ids, NOW+timedelta(days=1)), (report, ids, NOW+timedelta(hours=8))]
        invalid = deepcopy(report)
        invalid['rows'][0]['personalized'] = False
        cases.append((invalid, ids, NOW))
        for value, chosen, now in cases:
            with self.subTest(chosen=chosen, now=now), self.assertRaises(ValueError):
                workspace.prepare_group(value, chosen, now)

    def test_selected_outcome_and_threshold_are_saved_without_changing_the_model_top_three(self):
        report = scan()
        for row in report['rows']:
            row.update(probabilities={'1st': .2, 'Égal': .1, '2nd': .7},
                       choice='2nd', max_probability=.7)
        ids = [row['fixture']['match_id'] for row in report['rows']]
        prepared = workspace.prepare_group(report, ids, NOW, outcome='1st', minimum=.19)
        requested = prepared['requested_scenario']
        self.assertEqual(requested['sequence'], ['1st', '1st'])
        self.assertAlmostEqual(requested['probability'], .04)
        self.assertNotIn(requested['sequence'], [row['sequence'] for row in prepared['methods']['clubs']['ranking']])
        selection = prepared['metadata']['selection']
        self.assertEqual(selection['searched_outcome'], '1st')
        self.assertEqual(selection['search'], {'outcome': '1st', 'minimum': .19})
        self.assertEqual(selection['minimum_match_probability'], .2)
        self.assertTrue(all(row['choice'] == '2nd' and row['search_probability'] == .2
                            for row in selection['match_probabilities']))
        with self.assertRaises(ValueError):
            workspace.prepare_group(report, ids, NOW, outcome='1st', minimum=.21)
        with self.assertRaises(ValueError):
            workspace.prepare_group(report, ids, NOW, outcome='unknown')
        entry = workspace.record_group(prepared, self.root, clock=lambda: NOW, fetch=lambda **_: raw_targets())
        self.assertEqual(entry['metadata']['selection']['requested_scenario'], requested)
        self.assertEqual(daily.project([entry])[0]['searched_scenario']['state'], 'pending')
        finished = raw_targets()
        for row in finished:
            row['fixture']['status']['short'] = 'FT'
            row['score']['halftime'] = {'home': 2, 'away': 0}
            row['score']['fulltime'] = {'home': 2, 'away': 0}
        journal.reconcile(finished, now=NOW+timedelta(hours=8), path=self.path)
        row = daily.project(journal.list_entries(self.path))[0]
        self.assertEqual(row['searched_scenario']['state'], 'hit')
        self.assertEqual(row['scenario_outcome']['state'], 'miss')
        unchanged = journal.list_entries(self.path)[0]
        self.assertEqual(unchanged['methods'], entry['methods'])

    def test_requested_scenario_never_inherits_pending_stale_success_or_accepts_changed_probability(self):
        prepared = workspace.prepare_group(scan(), ['100000', '100001'], NOW, outcome='Égal')
        entry = workspace.record_group(prepared, self.root, clock=lambda: NOW, fetch=lambda **_: raw_targets())
        selection = entry['metadata']['selection']
        entry['actual'] = selection['requested_scenario']['sequence'][:]
        self.assertEqual(daily.searched_scenario(entry, selection)['state'], 'pending')
        entry['status'] = 'settled'
        entry['actual'] = ['1st', '2nd']
        self.assertEqual(daily.searched_scenario(entry, selection)['state'], 'miss')
        altered = deepcopy(selection)
        altered['requested_scenario']['probability'] = .99
        self.assertEqual(daily.searched_scenario(entry, altered)['state'], 'unavailable')
        entry['status'] = 'void'
        self.assertEqual(daily.searched_scenario(entry, selection)['state'], 'void')

    def test_custom_choices_remain_attached_to_match_ids_after_chronological_ordering(self):
        report = scan((39, 61, 140))
        choices = {'100000': '1st', '100001': 'Égal', '100002': '2nd'}
        prepared = workspace.prepare_group(report, ['100001', '100000', '100002'], NOW, choices=choices)
        self.assertEqual([row['match_id'] for row in prepared['fixtures']], ['100002', '100001', '100000'])
        self.assertEqual(prepared['requested_scenario']['sequence'], ['2nd', 'Égal', '1st'])
        rows = prepared['metadata']['selection']['match_probabilities']
        self.assertAlmostEqual(prepared['requested_scenario']['probability'],
                               math.prod(row['probabilities'][choices[row['match_id']]] for row in rows))
        for row in rows:
            label = choices[row['match_id']]
            self.assertEqual(row['search_outcome'], label)
            self.assertAlmostEqual(row['delta_points'], 100*(row['probabilities'][label]-row['baseline'][label]))
        for invalid in ({'100000': '1st'}, dict(choices, extra='2nd'), dict(choices, **{'100000': 'invalid'})):
            with self.assertRaises(ValueError):
                workspace.prepare_group(report, list(choices), NOW, choices=invalid)
        with self.assertRaises(ValueError):
            workspace.prepare_group(report, list(choices), NOW, outcome='1st', choices=choices)

    def test_different_scenarios_on_the_same_group_share_one_forecast_and_keep_separate_history(self):
        report = scan()
        ids = ['100000', '100001']
        first_prepared = workspace.prepare_group(report, ids, NOW, choices=dict(zip(ids, ['1st', 'Égal'])))
        first = workspace.record_group(first_prepared, self.root, clock=lambda: NOW, fetch=lambda **_: raw_targets())
        self.assertTrue(first['forecast_created'])
        first_methods = deepcopy(first['methods'])
        later_report = deepcopy(report)
        for row in later_report['rows']:
            row['probabilities'] = {'1st': .8, 'Égal': .1, '2nd': .1}
            row['baseline'] = {'1st': .9, 'Égal': .05, '2nd': .05}
            row.update(choice='1st', max_probability=.8)
        second_prepared = workspace.prepare_group(later_report, ids, NOW, choices=dict(zip(ids, ['2nd', '1st'])))
        second = workspace.record_group(second_prepared, self.root, clock=lambda: NOW, fetch=lambda **_: raw_targets())
        self.assertEqual(second['id'], first['id'])
        self.assertFalse(second['forecast_created'])
        self.assertEqual(second['methods'], first_methods)
        self.assertNotEqual(second['scenario_intent']['id'], first['scenario_intent']['id'])
        selected = second['scenario_intent']['selection']
        self.assertAlmostEqual(selected['requested_scenario']['probability'],
                               models.probability_for_sequence(first_methods['clubs'], selected['requested_scenario']['sequence']))
        self.assertNotEqual(selected['requested_scenario']['probability'], second_prepared['requested_scenario']['probability'])
        self.assertNotEqual(selected['match_probabilities'][0]['baseline'], later_report['rows'][0]['baseline'])
        entries = journal.list_entries(self.path)
        intents = scenario_store.list_intents(self.root/'probability_scenarios.sqlite3')
        self.assertEqual(len(entries), 1)
        self.assertEqual(len(intents), 2)
        rows = daily.project(entries, scenario_intents=intents)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row['id'] for row in rows}, {'intent:'+intent['id'] for intent in intents})
        self.assertTrue(all(row['searched_scenario']['state'] == 'pending' for row in rows))
        with self.assertRaises(ValueError):
            workspace.record_group(second_prepared, self.root, clock=lambda: NOW+timedelta(hours=8),
                                   fetch=lambda **_: self.fail('Late custom choice must not fetch'))
        self.assertEqual(journal.list_entries(self.path)[0]['methods'], first_methods)
        self.assertEqual(len(scenario_store.list_intents(self.root/'probability_scenarios.sqlite3')), 2)

    def test_explicit_registration_preserves_snapshot_and_reuses_earliest_forecast(self):
        prepared = self.prepare()
        original = deepcopy(prepared)
        calls = []
        def fetch(**params):
            calls.append(params)
            return raw_targets()
        entry = workspace.record_group(prepared, self.root, clock=lambda: NOW, fetch=fetch)
        self.assertTrue(entry['created'])
        self.assertEqual(calls, [{'ids': '100001-100000', 'timezone': 'UTC'}])
        self.assertEqual(entry['methods'], prepared['methods'])
        self.assertEqual(prepared, original)
        self.assertEqual(entry['status'], 'pending')
        self.assertEqual(entry['metadata']['daily_policy'], workspace.VERSION)
        old = deepcopy(entry)
        changed = deepcopy(prepared)
        changed['methods']['clubs']['generation'] = 'different-later-calculation'
        reused = workspace.record_group(changed, self.root, clock=lambda: NOW+timedelta(days=1),
                                        fetch=lambda **_: self.fail('No API for locked forecast'))
        self.assertFalse(reused['created'])
        self.assertEqual(reused['methods'], old['methods'])
        self.assertEqual(len(journal.list_entries(self.path)), 1)
        projected = daily.project([entry])
        self.assertEqual(projected[0]['selection']['policy'], workspace.VERSION)
        # The same group may later be reused by the historical-support planner.
        sidecar = dict(entry_id=entry['id'], selection={'data_support_index': 90}, competitions={})
        self.assertEqual(daily.project([entry], [sidecar])[0]['selection'], entry['metadata']['selection'])

    def test_same_competition_group_reuses_original_legacy_prediction_without_relabeling(self):
        prepared = self.prepare((39, 39))
        fixtures = prepared['fixtures']
        positions = [{'1st': .6, 'Égal': .1, '2nd': .3} for _ in fixtures]
        method = models._method('original-clubs-version', models._distribution('independent', 2, distributions=positions),
                                dict(source='API-Football', league='39', training_before='2026-10-22'))
        old = journal.record(fixtures, {'clubs': method}, now=NOW, path=self.path,
                             raw_fixtures=raw_targets((39, 39)))
        result = workspace.record_group(prepared, self.root, clock=lambda: NOW,
                                       fetch=lambda **_: self.fail('No duplicate request'))
        self.assertFalse(result['created'])
        self.assertEqual(result['methods'], old['methods'])
        self.assertNotIn('group_kind', result)
        self.assertNotIn('daily_policy', result['metadata'])

    def test_invalid_preflight_cannot_create_or_alter_forecasts(self):
        prepared = self.prepare()
        for issue in ('missing', 'extra', 'started', 'club', 'name', 'league', 'time', 'not_dict'):
            fresh = raw_targets()
            if issue == 'missing':
                fresh.pop()
            elif issue == 'extra':
                fresh.append(fixture(99999))
            elif issue == 'started':
                fresh[0]['fixture']['status']['short'] = '1H'
            elif issue == 'club':
                fresh[0]['teams']['home']['id'] = 99999
            elif issue == 'name':
                fresh[0]['teams']['home']['name'] = 'Renamed club'
            elif issue == 'league':
                fresh[0]['league']['id'] = 99
            elif issue == 'time':
                fresh[0]['fixture']['date'] = '2026-10-22T19:00:00Z'
            else:
                fresh[0] = None
            with self.subTest(issue=issue), self.assertRaises(ValueError):
                workspace.record_group(prepared, self.root, clock=lambda: NOW, fetch=lambda **_: fresh)
            self.assertFalse(self.path.exists())
        clocks = iter([NOW, NOW+timedelta(hours=8)])
        with self.assertRaises(ValueError):
            workspace.record_group(prepared, self.root, clock=lambda: next(clocks), fetch=lambda **_: raw_targets())
        self.assertFalse(self.path.exists())

    def test_saved_probabilities_settle_locally_without_current_model_or_rescan(self):
        prepared = self.prepare()
        first = workspace.record_group(prepared, self.root, clock=lambda: NOW, fetch=lambda **_: raw_targets())
        finished = raw_targets()
        for value in finished:
            value['fixture']['status']['short'] = 'FT'
            value['score']['halftime'] = {'home': 0, 'away': 0}
            value['score']['fulltime'] = {'home': 1, 'away': 0}
        with patch.object(engine, 'scan', side_effect=AssertionError('No future scan')), \
                patch.object(models, 'build_mixed_methods', side_effect=AssertionError('No future model')):
            journal.reconcile(finished, now=NOW+timedelta(hours=8), path=self.path)
        entry = journal.list_entries(self.path)[0]
        self.assertEqual(entry['status'], 'settled')
        self.assertEqual(entry['methods'], first['methods'])
        self.assertAlmostEqual(entry['evaluations']['clubs']['probability'],
                               models.probability_for_sequence(first['methods']['clubs'], ['2nd', '2nd']))

    def test_refresh_is_single_explicit_calendar_action(self):
        with patch.object(workspace.calendar, 'refresh_day', return_value={}) as refresh, \
                patch.object(workspace, 'load_scan', return_value={'day': '2026-10-22'}) as load:
            workspace.refresh_scan(self.root, NOW)
        refresh.assert_called_once()
        self.assertEqual(refresh.call_args.args[0].isoformat(), '2026-10-22')
        self.assertEqual(refresh.call_args.kwargs['timezone_name'], 'Africa/Kinshasa')
        load.assert_called_once_with(root=self.root, now=NOW)


if __name__ == '__main__':
    unittest.main()
