"""Cloud execution is explicit, bounded, secret-safe and visitor independent."""
from contextlib import contextmanager
import copy
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import cloud_workspace as cli
import automation_worker as worker


class CloudSchedulerTests(unittest.TestCase):
    def storage(self, initialized=True):
        return SimpleNamespace(configured=Mock(return_value=True),
            status=Mock(return_value=dict(initialized=initialized, revision=int(initialized))),
            seed=Mock(return_value=dict(initialized=True, revision=1)),
            transaction=self.transaction, assert_lease=Mock(), checkpoint=Mock())

    @contextmanager
    def transaction(self):
        yield

    def test_missing_config_refuses_run_before_loading_api_client(self):
        store = self.storage()
        store.configured.return_value = False
        with patch.object(cli, '_storage', return_value=store), patch.object(cli, '_worker') as load:
            with self.assertRaises(cli.CloudCommandError):
                cli.run_workspace()
            load.assert_not_called()

    def test_unseeded_storage_never_runs_worker(self):
        with patch.object(cli, '_storage', return_value=self.storage(False)), patch.object(cli.service, 'run_once') as run:
            with self.assertRaises(cli.CloudCommandError):
                cli.run_workspace()
            run.assert_not_called()

    def test_runtime_caps_keep_saved_configuration_unchanged(self):
        config = copy.deepcopy(worker.DEFAULT_CONFIG)
        original = copy.deepcopy(config)
        bounded = cli.bounded_config(config)
        self.assertEqual(config, original)
        self.assertEqual(bounded['cycle_request_budget'], 6)
        self.assertEqual(bounded['daily_request_budget'], 60)
        self.assertFalse(bounded['models_enabled'])
        self.assertEqual(bounded['model_jobs_per_cycle'], 0)
        smaller = cli.bounded_config({**config, 'cycle_request_budget':2, 'daily_request_budget':4})
        self.assertEqual((smaller['cycle_request_budget'], smaller['daily_request_budget']), (2,4))

    def test_run_is_fenced_and_skips_models_by_default(self):
        store = self.storage()
        config = copy.deepcopy(worker.DEFAULT_CONFIG)
        with patch.object(cli, '_storage', return_value=store), patch('api_football.load_key', return_value='private'), \
                patch.object(worker, 'load_config', return_value=config), \
                patch.object(cli.service, 'run_once', return_value=dict(status='completed', requests=2)) as run:
            self.assertEqual(cli.run_workspace()['requests'], 2)
        self.assertTrue(run.call_args.kwargs['skip_models'])
        self.assertEqual(run.call_args.kwargs['config']['cycle_request_budget'], 6)
        store.checkpoint.assert_called_once()
        self.assertEqual(store.assert_lease.call_count, 2)

    def test_errors_and_metadata_never_echo_secrets(self):
        with patch.object(cli, 'cloud_status', side_effect=RuntimeError('postgresql://user:private@remote/db')), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(['check']), 1)
            self.assertNotIn('private', output.getvalue())
            self.assertNotIn('postgresql://', output.getvalue())
        self.assertEqual(cli._safe_summary(dict(status='completed', requests=1, secret='private', dsn='private')),
                         dict(status='completed', requests=1))

    def test_seed_cannot_overwrite_remote(self):
        store = self.storage()
        with patch.object(cli, '_storage', return_value=store):
            with self.assertRaises(cli.CloudCommandError):
                cli.seed_workspace()
        store.seed.assert_not_called()

    def test_schedule_is_opt_in_bounded_and_never_receives_pull_requests(self):
        workflow = (Path(__file__).parent / '.github/workflows/cloud-automation.yml').read_text()
        self.assertIn("cron: '17 7,15,23 * * *'", workflow)
        self.assertIn("vars.PATTERNLAB_CLOUD_AUTOMATION == 'true'", workflow)
        self.assertIn('timeout-minutes: 8', workflow)
        self.assertIn('contents: read', workflow)
        self.assertIn('cancel-in-progress: false', workflow)
        self.assertIn("PATTERNLAB_CLOUD_REQUIRED: 'true'", workflow)
        self.assertNotIn('pull_request', workflow)
        self.assertNotIn('upload-artifact', workflow)


if __name__ == '__main__':
    unittest.main()
