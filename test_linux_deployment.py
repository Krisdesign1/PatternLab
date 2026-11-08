"""Check the Linux kit's activation/training guards without server/API access."""
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from deployment.linux import preflight, run_worker
from scripts import automation_service


KIT = Path(__file__).parent / "deployment/linux"


class LinuxDeploymentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.secrets = self.root / "secrets.toml"
        self.secrets.write_text('PATTERNLAB_STORAGE_MODE="local"\n[access]\nlocal_owner=false\n')
        self.secrets.chmod(0o600)
        self.environment = patch.dict(os.environ, {"PATTERNLAB_STORAGE_MODE": "local"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def check_secrets(self, **kwargs):
        return preflight.validate_secrets(self.secrets, os.getuid(), **kwargs)

    def test_template_is_local_and_explicitly_denies_loopback_ownership(self):
        self.secrets.write_bytes((KIT / "secrets.toml.example").read_bytes())
        self.check_secrets()
        with self.assertRaises(preflight.PreflightError):
            self.check_secrets(worker=True)

    def test_unsafe_permissions_symlinks_and_owner_are_refused(self):
        self.secrets.chmod(0o644)
        with self.assertRaises(preflight.PreflightError):
            self.check_secrets()
        self.secrets.chmod(0o600)
        with self.assertRaises(preflight.PreflightError):
            preflight.validate_secrets(self.secrets, os.getuid() + 1)
        target = self.root / "actual.toml"
        self.secrets.rename(target)
        self.secrets.symlink_to(target)
        with self.assertRaises(preflight.PreflightError):
            self.check_secrets()

    def test_private_values_are_not_exposed_for_invalid_toml(self):
        private = "TEST-ONLY-PRIVATE-KEY"
        self.secrets.write_text('API_FOOTBALL_KEY="' + private + '"\n[broken')
        with self.assertRaises(preflight.PreflightError) as raised:
            self.check_secrets()
        self.assertNotIn(private, str(raised.exception))

    def test_owner_bypass_and_ambiguous_storage_are_refused(self):
        for body in (
            'PATTERNLAB_STORAGE_MODE="local"\n[access]\nlocal_owner=true\n',
            'PATTERNLAB_STORAGE_MODE="local"\n[access]\n',
            'PATTERNLAB_STORAGE_MODE="auto"\n[access]\nlocal_owner=false\n',
        ):
            with self.subTest(body=body):
                self.secrets.write_text(body)
                with self.assertRaises(preflight.PreflightError):
                    self.check_secrets()
        self.secrets.write_text('PATTERNLAB_STORAGE_MODE="local"\n[access]\nlocal_owner=false\n')
        with patch.dict(os.environ, {"PATTERNLAB_STORAGE_MODE": "shared"}):
            with self.assertRaises(preflight.PreflightError):
                self.check_secrets()

    def test_domain_cannot_inject_config_or_enable_placeholder_https(self):
        self.assertIn("app.my-domain.net {", preflight.render_caddy("APP.My-Domain.net"))
        self.assertEqual(preflight.render_web_environment("APP.My-Domain.net"),
                         "PATTERNLAB_DOMAIN=app.my-domain.net\n")
        for domain in ("", "example.com", "www.example.org", "127.0.0.1", "localhost",
                       "*.my-domain.net", "https://app.my-domain.net", "app.my-domain.net:443",
                       "app.my-domain.net\n:80 { respond unsafe }", "app.my-domain.net/", "test.local"):
            with self.subTest(domain=domain):
                with self.assertRaises(preflight.PreflightError):
                    preflight.render_caddy(domain)

    def test_domain_environment_cannot_add_other_service_settings(self):
        path = self.root / "web.env"
        with patch.object(preflight, "_root_owned", return_value=SimpleNamespace(st_mode=0o100644)):
            path.write_text("PATTERNLAB_DOMAIN=app.my-domain.net\n")
            preflight.validate_web_environment(path)
            path.write_text("PATTERNLAB_DOMAIN=app.my-domain.net\nPATTERNLAB_STORAGE_MODE=shared\n")
            with self.assertRaises(preflight.PreflightError):
                preflight.validate_web_environment(path)

    def test_wrong_platform_or_runtime_fails_before_native_imports(self):
        with patch.object(preflight.platform, "system", return_value="Darwin"), \
                patch.object(preflight.platform, "freedesktop_os_release", return_value={}):
            with self.assertRaises(preflight.PreflightError):
                preflight.validate_platform()
        with patch.object(preflight.sys, "version_info", (3, 12, 3)), \
                patch.object(preflight.importlib, "import_module") as imports:
            with self.assertRaises(preflight.PreflightError):
                preflight.validate_runtime(self.root)
            imports.assert_not_called()

    def test_package_version_mismatch_cannot_be_ignored(self):
        with patch.object(preflight.sys, "version_info", (3, 12, 10)), \
                patch.object(preflight.sys, "prefix", str(self.root / ".venv")), \
                patch.object(preflight.sys, "base_prefix", "/usr/local"), \
                patch.object(preflight.importlib.metadata, "version", return_value="wrong"), \
                patch.object(preflight.importlib, "import_module") as imports:
            with self.assertRaises(preflight.PreflightError):
                preflight.validate_runtime(self.root)
            imports.assert_not_called()

    def test_constraints_and_preflight_share_the_frozen_versions(self):
        constraints = (KIT / "constraints-frozen.txt").read_text().splitlines()
        for package, version in preflight.PINNED_VERSIONS.items():
            self.assertIn(f"{package}=={version}", constraints)
        self.assertEqual((KIT / ".python-version").read_text().strip(), "3.12.10")

    def test_worker_requires_root_0600_regular_marker(self):
        with self.assertRaises(preflight.PreflightError):
            preflight.validate_worker_approval(self.root / "missing")
        for mode in (0o100644, 0o040600):
            with patch.object(preflight, "_root_owned", return_value=SimpleNamespace(st_mode=mode)):
                with self.assertRaises(preflight.PreflightError):
                    preflight.validate_worker_approval(self.root / "marker")
        with patch.object(preflight, "_root_owned", return_value=SimpleNamespace(st_mode=0o100600)):
            preflight.validate_worker_approval(self.root / "marker")

    def test_wrapper_disables_both_training_paths_without_editing_config(self):
        stored = dict(enabled=True, models_enabled=True, model_jobs_per_cycle=1)
        worker = SimpleNamespace(run_cycle=Mock(return_value={"status": "completed"}))
        def existing_run_once(**arguments):
            return automation_service._run_once(worker, arguments["root"], dry_run=False,
                config=arguments["config"], skip_models=arguments["skip_models"])
        service = SimpleNamespace(_configuration=Mock(return_value=stored),
                                  run_once=Mock(side_effect=existing_run_once))
        with patch.object(automation_service, "_prepare_models") as preparation:
            result = run_worker.run_once(self.root, service=service)
        self.assertEqual(result, {"status": "completed"})
        preparation.assert_not_called()
        config = worker.run_cycle.call_args.kwargs["config"]
        self.assertIs(config["models_enabled"], False)
        self.assertEqual(config["model_jobs_per_cycle"], 0)
        self.assertTrue(stored["models_enabled"])
        self.assertEqual(stored["model_jobs_per_cycle"], 1)

    def test_direct_wrapper_cannot_skip_preflight_or_log_private_exception(self):
        output = io.StringIO()
        with patch.object(preflight, "check", side_effect=ValueError("TEST-ONLY-PRIVATE-KEY")), \
                patch.object(run_worker, "run_once") as cycle, redirect_stdout(output):
            status = run_worker.main(["--once"])
        self.assertEqual(status, 1)
        cycle.assert_not_called()
        self.assertNotIn("TEST-ONLY-PRIVATE-KEY", output.getvalue())

    def test_no_runtime_activation_and_single_nonoverlapping_timer(self):
        installer = (KIT / "install.sh").read_text()
        self.assertNotRegex(installer, r"\bsystemctl\s+(?:start|enable|restart|reload|try-restart)\b")
        self.assertNotIn("install -o root -g root -m 0600 /dev/null", installer)
        service = (KIT / "patternlab-worker.service").read_text()
        self.assertIn("Type=oneshot", service)
        self.assertIn("ConditionPathExists=/etc/patternlab/worker-cutover-approved", service)
        self.assertIn("deployment/linux/run_worker.py --once", service)
        self.assertIn("TimeoutStartSec=infinity", service)
        timer = (KIT / "patternlab-worker.timer").read_text()
        self.assertIn("OnUnitInactiveSec=15min", timer)
        self.assertNotIn("OnCalendar=", timer)
        web = (KIT / "patternlab-web.service").read_text()
        self.assertIn("User=patternlab", web)
        self.assertIn("--server.address=127.0.0.1", web)
        self.assertIn("--server.enableXsrfProtection=true", web)
        self.assertIn("--client.showErrorDetails=none", web)
        self.assertIn("--browser.serverAddress=${PATTERNLAB_DOMAIN}", web)
        self.assertIn("--server.allowedHosts=${PATTERNLAB_DOMAIN}", web)
        self.assertIn("--server.corsAllowedOrigins=https://${PATTERNLAB_DOMAIN}", web)
        self.assertIn("ProtectSystem=strict", web)
        self.assertIn("ReadWritePaths=/opt/patternlab/data", web)


if __name__ == "__main__":
    unittest.main()
