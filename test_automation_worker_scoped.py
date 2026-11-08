"""Qualification of the explicit scoped-I/O worker against its reference.

Only test-module bindings are temporarily redirected. The reference scheduler,
its tests, inference sources and existing prediction protocols stay untouched.
All providers and stores below are synthetic and isolated from production.
"""
import ast
from contextlib import ExitStack
from copy import deepcopy
from datetime import timedelta
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import automation_worker as original
import automation_worker_scoped as scoped
import fixture_store
import prediction_journal as journal
import test_automation_worker as reference_tests
import test_day_refresh_clocks as clock_tests
import test_result_priority_checkpoint as checkpoint_tests


class _UseScoped:
    """Replay an existing suite against the new explicit module, test-only."""
    reference_suite = None

    def setUp(self):
        bindings = ExitStack()
        self.addCleanup(bindings.close)
        bindings.enter_context(patch.object(self.reference_suite, 'worker', scoped))
        # The new daily adapter preserves the old public helpers. Its own
        # caller is explicit; inherited tests must mock that same boundary.
        try:
            daily = importlib.import_module('daily_analysis_worker_io')
        except ModuleNotFoundError:
            daily = importlib.import_module('daily_analysis_worker')
        for name in ('daily_analysis_worker', 'daily'):
            if hasattr(self.reference_suite, name):
                bindings.enter_context(patch.object(self.reference_suite, name, daily))
        super().setUp()


class ScopedWorkerRegressionTests(_UseScoped, reference_tests.WorkerTests):
    reference_suite = reference_tests

    def test_host_deadline_does_not_postpone_an_unattempted_history_import(self):
        # The equivalent expiration is now injected at the streaming inventory
        # boundary; _inventory(raw) is deliberately no longer called there.
        self.config.update(history_enabled=True, history_seasons=[2024,2025], sizes=[20])
        scoped._write(self.root/'automation_state.json',dict(version=scoped.VERSION,next_phase='history'))
        elapsed = [0]
        inventory = scoped.worker_io.inventory
        def expire(*args, **kwargs):
            value = inventory(*args, **kwargs)
            elapsed[0] = 11
            return value
        with patch.object(scoped.time, 'monotonic', side_effect=lambda:elapsed[0]), \
             patch.object(scoped.worker_io, 'inventory', side_effect=expire):
            report = self.run_cycle(max_duration_seconds=10)
        self.assertEqual(report['status'], 'paused')
        self.assertEqual(report['requests'], 1)
        self.assertIn('hébergeur', report['issues'][0])
        state = json.loads((self.root/'automation_state.json').read_text())
        self.assertEqual(state['history_attempts'], {})
        self.assertEqual(self.run_cycle(now=reference_tests.BEFORE+timedelta(minutes=15))['history_imports'], 2)


class ScopedDayRefreshClockTests(_UseScoped, clock_tests.DayRefreshClockTests):
    reference_suite = clock_tests


class ScopedResultPriorityCheckpointTests(_UseScoped, checkpoint_tests.ResultPriorityCheckpointTests):
    reference_suite = checkpoint_tests


class ScopedSourceQualificationTests(unittest.TestCase):
    def test_unchanged_scheduler_functions_and_classes_have_exact_ast_parity(self):
        def definitions(module):
            tree = ast.parse(Path(module.__file__).read_text(encoding='utf-8'))
            return {node.name: node for node in tree.body
                    if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
        before, after = definitions(original), definitions(scoped)
        aliases = {'AutomationError', '_Pause'}
        allowed = {'_history', '_forecast', '_track_candidates', '_reconcile',
                   '_execute_cycle', 'run_cycle'}
        self.assertEqual(set(before)-set(after), aliases)
        self.assertEqual(set(after)-set(before), set())
        changed = {name for name in set(before)&set(after)
                   if ast.dump(before[name], include_attributes=False)
                   != ast.dump(after[name], include_attributes=False)}
        self.assertEqual(changed, allowed)
        self.assertEqual(scoped.VERSION, original.VERSION)
        self.assertEqual(scoped.DEFAULT_CONFIG, original.DEFAULT_CONFIG)
        self.assertEqual(scoped.TIMEZONE, original.TIMEZONE)
        self.assertIs(scoped.AutomationError, original.AutomationError)
        self.assertIs(scoped._Pause, original._Pause)

    def test_execution_revision_is_explicit_and_separate_from_prediction_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            result = scoped.run_cycle(root=Path(folder), now=reference_tests.BEFORE,
                                      config=dict(scoped.DEFAULT_CONFIG, enabled=False))
        self.assertEqual(result['version'], original.VERSION)
        self.assertEqual(result['execution_io'], {'version': scoped.EXECUTION_IO_VERSION})
        self.assertNotEqual(scoped.EXECUTION_IO_VERSION, original.VERSION)


class ScopedEndToEndDifferentialTests(unittest.TestCase):
    def test_same_cycles_keep_predictions_counters_api_order_and_settlement(self):
        """Real frozen inference and real journals; only provider/network is fake."""
        with tempfile.TemporaryDirectory() as folder, ExitStack() as patches:
            root = Path(folder)
            patches.enter_context(patch.object(original.cloud_storage, 'configured', return_value=False))
            patches.enter_context(patch.object(api, 'load_key', side_effect=api.ApiFootballError('No credentials')))
            patches.enter_context(patch.object(api.ApiFootballClient, 'get', side_effect=AssertionError('No network')))
            patches.enter_context(patch.object(reference_tests.ml, 'load_report', return_value=None))
            patches.enter_context(patch.object(reference_tests.models, '_context_neighbors',
                side_effect=reference_tests.models.SnapshotError('Configuration absente.')))
            for name in ('daily_analysis_worker', 'daily_analysis_worker_io'):
                try:
                    daily = importlib.import_module(name)
                except ModuleNotFoundError:
                    continue
                patches.enter_context(patch.object(daily, 'run', return_value=dict(
                    status='completed', candidates=0, created=0, remaining=0)))
            history = []
            for row in reference_tests.sample():
                item = reference_tests.raw_fixtures([row], status='FT')[0]
                item['score'] = {part:dict(zip(('home', 'away'), map(int, row[key].split('-'))))
                    for part,key in (('halftime','ht'), ('fulltime','ft'))}
                history.append(item)
            upcoming = reference_tests.raw_fixtures()
            finals = reference_tests.raw_fixtures(status='FT')
            outputs = []
            for worker, name in ((original, 'reference'), (scoped, 'scoped')):
                target = root/name
                api.save_fixtures(deepcopy(history+upcoming), target/'fixtures.json')
                fixture_store.migrate_json(target/'fixtures.json')
                client = reference_tests.Client(upcoming)
                config = dict(worker.DEFAULT_CONFIG, sizes=[2,4], history_enabled=True,
                              history_seasons=[2024])
                reports, entries = [], []
                for current, matches in (
                        (reference_tests.BEFORE, upcoming),
                        (reference_tests.BEFORE+timedelta(minutes=15), upcoming),
                        (reference_tests.AFTER, finals)):
                    client.matches = deepcopy(matches)
                    report = worker.run_cycle(root=target, config=config, client=client, now=current)
                    self.assertEqual(report['status'], 'completed', report)
                    # Measured wall duration and execution receipt deliberately
                    # differ; every scheduling/result counter remains compared.
                    reports.append({key:value for key,value in report.items()
                                    if key not in {'duration_seconds','execution_io'}})
                    entries.append(journal.list_entries(target/'predictions.sqlite3'))
                state = json.loads((target/'automation_state.json').read_text(encoding='utf-8'))
                outputs.append(dict(reports=reports, entries=entries,
                                    state=state, calls=client.calls,
                                    fixtures=api.load_fixtures(target/'fixtures.json'),
                                    # Import receipt uses the real local wall
                                    # clock; compare contents without inventing
                                    # equal commit times for sequential runs.
                                    imports=[{key:value for key,value in row.items() if key!='imported_at'}
                                             for row in api.load_season_imports(target/'fixtures.json')]))
            self.assertEqual(outputs[0], outputs[1])
            self.assertEqual(outputs[1]['reports'][0]['created'], 3)
            self.assertEqual(outputs[1]['reports'][1]['created'], 0)
            self.assertEqual(outputs[1]['reports'][2]['settled'], 3)
            self.assertTrue(all(row['status']=='settled' for row in outputs[1]['entries'][-1]))


if __name__ == '__main__':
    unittest.main()
