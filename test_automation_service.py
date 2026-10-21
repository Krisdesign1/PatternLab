"""Scheduler tests isolate launchd, the worker, credentials and all app data."""
import contextlib
import io
import json
from pathlib import Path
import plistlib
import stat
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import automation_service as service


def response(code=0, output=""):
    return SimpleNamespace(returncode=code, stdout=output, stderr="not logged")


class AutomationServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "Pattern Lab & tests"
        self.agents = Path(self.temporary.name).resolve() / "LaunchAgents"
        (self.root / ".venv" / "bin").mkdir(parents=True)
        (self.root / "scripts").mkdir()
        self.python = self.root / ".venv" / "bin" / "python"
        self.python.write_text("#!/bin/sh\nexit 0\n")
        self.python.chmod(0o700)
        (self.root / "scripts" / "automation_service.py").write_text("# fixture\n")
        self.worker = Mock()
        self.worker.load_config.return_value = {"enabled": True, "interval_minutes": 15}
        self.worker.run_cycle.return_value = {"status": "completed", "requests": 3, "created": 2}
        self.addCleanup(patch.stopall)
        patch.object(service, "_worker", return_value=self.worker).start()
        patch.object(service.sys, "platform", "darwin").start()
        patch.object(service.os, "getuid", return_value=501).start()

    def installed_path(self):
        return self.agents / service.PLIST_NAME

    def existing(self, payload=None):
        self.agents.mkdir(exist_ok=True)
        path = self.installed_path()
        path.write_bytes(plistlib.dumps(payload or {"Label": service.LABEL, "StartInterval": 600}))
        return path

    def invoke(self, arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = service.main(arguments, root=self.root)
        return code, json.loads(output.getvalue())

    def test_plist_uses_venv_absolute_arguments_without_shell_or_credentials(self):
        payload = service.plist_payload(self.root)
        self.assertEqual(payload["ProgramArguments"], [str(self.python), str(self.root / "scripts" / "automation_service.py"), "--once"])
        self.assertEqual(payload["WorkingDirectory"], str(self.root))
        self.assertEqual(payload["StartInterval"], 900)
        self.assertTrue(payload["RunAtLoad"])
        self.assertNotIn("EnvironmentVariables", payload)
        self.assertNotIn("KeepAlive", payload)
        self.assertEqual(plistlib.loads(plistlib.dumps(payload)), payload)
        self.worker.load_config.assert_called_once_with(root=self.root / "data" / "api_football")

    def test_venv_symlink_stays_in_program_arguments(self):
        target = self.root / "base-python"
        target.write_text("#!/bin/sh\n")
        target.chmod(0o700)
        self.python.unlink()
        self.python.symlink_to(target)
        self.assertEqual(service.plist_payload(self.root)["ProgramArguments"][0], str(self.python))

    def test_prepare_only_writes_private_workspace_plist(self):
        self.worker.load_config.return_value["interval_minutes"] = 30
        with patch.object(service, "_command") as command:
            path = service.prepare_plist(self.root)
        self.assertTrue(path.is_relative_to(self.root))
        self.assertEqual(plistlib.loads(path.read_bytes())["StartInterval"], 1800)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertFalse(self.agents.exists())
        command.assert_not_called()
        self.worker.run_cycle.assert_not_called()

    def test_prepare_requires_executable_venv_and_valid_interval(self):
        self.python.chmod(0o600)
        with self.assertRaisesRegex(service.ServiceError, "exécutable"):
            service.prepare_plist(self.root)
        self.python.chmod(0o700)
        for interval in (0, True, 1.5, "15", 1441):
            with self.subTest(interval=interval), self.assertRaises(service.ServiceError):
                service.plist_payload(self.root, config={"interval_minutes": interval})

    def test_status_is_read_only_and_loaded_is_active_between_cycles(self):
        runner = Mock(return_value=response(0, "state = waiting\n"))
        result = service.service_status(self.root, launch_agents_dir=self.agents, runner=runner)
        self.assertFalse(result["installed"])
        self.assertTrue(result["running"])
        self.assertFalse(result["process_running"])
        self.assertEqual(result["interval_seconds"], 900)
        self.assertEqual(runner.call_args.args[0], [service.LAUNCHCTL, "print", "gui/501/" + service.LABEL])
        self.assertFalse((self.root / "data").exists())
        self.worker.run_cycle.assert_not_called()

    def test_status_distinguishes_current_process_and_handles_launchctl_error_safely(self):
        runner = Mock(return_value=response(0, "\tstate = running\n\tpid = 42\n"))
        self.assertTrue(service.service_status(self.root, runner=runner)["process_running"])
        runner.side_effect = OSError("secret must not be displayed")
        result = service.service_status(self.root, runner=runner)
        self.assertFalse(result["running"])
        self.assertNotIn("secret", result["error"])

    def test_other_platform_only_reports_unsupported_and_never_launches(self):
        with patch.object(service.sys, "platform", "linux"):
            runner = Mock()
            status = service.service_status(self.root, runner=runner)
            self.assertFalse(status["supported"])
            with self.assertRaises(service.ServiceError):
                service.install_service(self.root, launch_agents_dir=self.agents, runner=runner)
        runner.assert_not_called()
        self.assertFalse(self.agents.exists())

    def test_status_reports_installed_interval_and_configuration_drift(self):
        self.existing(service.plist_payload(self.root))
        self.worker.load_config.return_value["interval_minutes"] = 30
        result = service.service_status(self.root, launch_agents_dir=self.agents, runner=Mock(return_value=response()))
        self.assertEqual(result["interval_seconds"], 900)
        self.assertEqual(result["configured_interval_seconds"], 1800)
        self.assertTrue(result["needs_reinstall"])
        self.worker.run_cycle.assert_not_called()

    def test_malformed_installed_plist_is_safe_to_inspect_and_is_not_deleted(self):
        path = self.existing(["not an agent"])
        runner = Mock(return_value=response(113))
        result = service.service_status(self.root, launch_agents_dir=self.agents, runner=runner)
        self.assertIn("error", result)
        with self.assertRaises(service.ServiceError):
            service.uninstall_service(self.root, launch_agents_dir=self.agents, runner=runner)
        self.assertTrue(path.exists())

    def test_install_bootstraps_user_agent_once_from_prepared_file(self):
        runner = Mock(side_effect=[response(113), response()])
        result = service.install_service(self.root, launch_agents_dir=self.agents, runner=runner)
        self.assertTrue(result["installed"])
        self.assertTrue(result["running"])
        self.assertEqual(self.installed_path().read_bytes(), Path(service.service_status(self.root, launch_agents_dir=self.agents, runner=Mock(return_value=response(113)))["prepared_plist_path"]).read_bytes())
        self.assertEqual(runner.call_args_list[-1].args[0], [service.LAUNCHCTL, "bootstrap", "gui/501", str(self.installed_path())])
        self.assertEqual(runner.call_count, 2)
        self.worker.run_cycle.assert_not_called()

    def test_reinstall_unloads_before_bootstrap_and_restores_previous_file_on_failure(self):
        path = self.existing()
        previous = path.read_bytes()
        runner = Mock(side_effect=[response(), response(), response(5), response()])
        with self.assertRaises(service.ServiceError):
            service.install_service(self.root, launch_agents_dir=self.agents, runner=runner)
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual([call.args[0][1] for call in runner.call_args_list], ["print", "bootout", "bootstrap", "bootstrap"])

    def test_failed_first_install_removes_unloaded_agent_file(self):
        runner = Mock(side_effect=[response(113), response(5)])
        with self.assertRaises(service.ServiceError):
            service.install_service(self.root, launch_agents_dir=self.agents, runner=runner)
        self.assertFalse(self.installed_path().exists())
        self.assertTrue((self.root / "data" / "api_football" / "automation_service" / service.PLIST_NAME).is_file())

    def test_foreign_plist_is_not_overwritten_or_removed(self):
        path = self.existing({"Label": "org.someone.other"})
        previous = path.read_bytes()
        runner = Mock()
        for action in (service.install_service, service.uninstall_service):
            with self.subTest(action=action.__name__), self.assertRaises(service.ServiceError):
                action(self.root, launch_agents_dir=self.agents, runner=runner)
            self.assertEqual(path.read_bytes(), previous)
        runner.assert_not_called()

    def test_uninstall_retains_data_logs_and_prepared_plist(self):
        self.existing()
        prepared = service.prepare_plist(self.root)
        log = prepared.parent / "automation.log"
        log.write_text("safe cycle summary\n")
        runner = Mock(side_effect=[response(), response()])
        result = service.uninstall_service(self.root, launch_agents_dir=self.agents, runner=runner)
        self.assertFalse(result["installed"])
        self.assertFalse(self.installed_path().exists())
        self.assertTrue(prepared.exists())
        self.assertEqual(log.read_text(), "safe cycle summary\n")
        self.assertEqual(runner.call_args_list[-1].args[0], [service.LAUNCHCTL, "bootout", "gui/501/" + service.LABEL])

    def test_uninstall_preserves_plist_when_loaded_agent_cannot_stop(self):
        path = self.existing()
        runner = Mock(side_effect=[response(), response(5)])
        with self.assertRaises(service.ServiceError):
            service.uninstall_service(self.root, launch_agents_dir=self.agents, runner=runner)
        self.assertTrue(path.exists())

    def test_once_calls_worker_exactly_once_and_whitelists_log_output(self):
        self.worker.run_cycle.return_value.update({
            "key": "secret", "requests": 3, "created": 2,
            "started_at": "2026-10-18T15:00:00+00:00",
            "finished_at": "secret", "issues": ["secret"], "jobs": [{"key": "secret"}],
        })
        code, result = self.invoke(["--once"])
        self.assertEqual(code, 0)
        self.assertEqual(result, {"status": "completed", "requests": 3, "created": 2, "started_at": "2026-10-18T15:00:00+00:00", "issues": 1})
        self.assertNotIn("secret", json.dumps(result))
        self.worker.run_cycle.assert_called_once_with(root=self.root / "data" / "api_football", dry_run=False)

    def test_service_runs_separate_bounded_model_phase_even_if_provider_is_unavailable(self):
        self.worker.run_cycle.return_value={'status':'error','requests':1,'issues':['safe error']}
        with patch.object(service,'_prepare_models',return_value=dict(status='completed',prepared=1)) as prepare:
            code,result=self.invoke(['--once'])
        self.assertEqual(code,1)
        self.assertEqual(result['models_prepared'],1)
        prepare.assert_called_once_with(self.root)

    def test_service_dry_run_and_busy_cycle_do_not_start_models(self):
        with patch.object(service,'_prepare_models',side_effect=AssertionError('No fit')):
            self.worker.run_cycle.return_value={'status':'dry_run','requests':0}
            self.assertEqual(self.invoke(['--dry-run'])[0],0)
            self.worker.run_cycle.return_value={'status':'busy','requests':0}
            self.assertEqual(self.invoke(['--once'])[0],0)

    def test_dry_run_only_calls_worker_in_simulation_mode(self):
        self.worker.run_cycle.return_value = {"status": "dry_run", "requests": 0}
        with patch.object(service, "_command") as command:
            code, result = self.invoke(["--dry-run"])
        self.assertEqual((code, result["status"]), (0, "dry_run"))
        self.worker.run_cycle.assert_called_once_with(root=self.root / "data" / "api_football", dry_run=True)
        command.assert_not_called()

    def test_install_dry_run_stages_only_without_registering_or_running_worker(self):
        with patch.object(service, "install_service") as install, patch.object(service, "_command") as command:
            code, result = self.invoke(["--install", "--dry-run"])
        self.assertEqual((code, result["status"]), (0, "prepared"))
        self.assertTrue(Path(result["prepared_plist_path"]).is_relative_to(self.root))
        install.assert_not_called()
        command.assert_not_called()
        self.worker.run_cycle.assert_not_called()

    def test_worker_exception_never_prints_exception_or_traceback(self):
        self.worker.run_cycle.side_effect = RuntimeError("API key secret would leak")
        code, result = self.invoke(["--once"])
        self.assertEqual((code, result["status"]), (1, "error"))
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("Traceback", json.dumps(result))

    def test_safe_service_error_keeps_actionable_message(self):
        self.python.unlink()
        code, result = self.invoke(["--install", "--dry-run"])
        self.assertEqual((code, result["status"]), (1, "error"))
        self.assertIn(".venv/bin/python", result["error"])

    def test_status_default_does_not_run_worker(self):
        with patch.object(service, "service_status", return_value={"installed": False, "running": False}) as status:
            code, result = self.invoke([])
        self.assertEqual(code, 0)
        self.assertFalse(result["running"])
        status.assert_called_once_with(self.root)
        self.worker.run_cycle.assert_not_called()


if __name__ == "__main__":
    unittest.main()
