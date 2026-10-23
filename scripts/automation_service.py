"""Run one automation cycle or manage its per-user macOS launchd agent.

No API key is copied into the plist.  Service inspection never starts a cycle.
The worker owns API budgets, grouping, locking and the persistent cycle report.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LABEL = "org.patternlab.automation"
PLIST_NAME = LABEL + ".plist"
LAUNCHCTL = "/bin/launchctl"
_STATUSES = {"completed", "paused", "error", "busy", "disabled", "dry_run"}
_COUNTERS = (
    "requests", "created", "already_recorded", "history_imports",
    "history_received", "coverage_competitions", "groups_considered",
    "settled", "pending",
)


class ServiceError(RuntimeError):
    """A fixed, safe-to-display scheduler error without command output."""


def _project(root=None):
    return Path(root).expanduser().resolve() if root is not None else PROJECT_ROOT


def _paths(root=None, launch_agents_dir=None):
    project = _project(root)
    # Preserve the venv symlink: resolving it would select the base interpreter.
    python = project / ".venv" / "bin" / "python"
    directory = project / "data" / "api_football" / "automation_service"
    agents = (Path(launch_agents_dir).expanduser() if launch_agents_dir is not None
              else Path.home() / "Library" / "LaunchAgents")
    return {
        "project": project, "data": project / "data" / "api_football",
        "python": python, "script": project / "scripts" / "automation_service.py",
        "directory": directory, "prepared": directory / PLIST_NAME,
        "installed": agents / PLIST_NAME,
        "stdout": directory / "automation.log",
        "stderr": directory / "automation.error.log",
    }


def _worker(root=None):
    project = str(_project(root))
    if project not in sys.path:
        sys.path.insert(0, project)
    return importlib.import_module("automation_worker")


def _configuration(root=None):
    try:
        config = _worker(root).load_config(root=_paths(root)["data"])
    except Exception:
        raise ServiceError("La configuration de l’automatisation est indisponible.") from None
    if not isinstance(config, dict):
        raise ServiceError("La configuration de l’automatisation est invalide.")
    interval = config.get("interval_minutes", 15)
    if type(interval) is not int or not 1 <= interval <= 1440:
        raise ServiceError("L’intervalle doit être un entier entre 1 et 1 440 minutes.")
    return {**config, "interval_minutes": interval}


def _atomic_bytes(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".automation-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def plist_payload(root=None, *, config=None):
    """Return the complete launchd payload; no file, process or API mutation."""
    paths = _paths(root)
    config = _configuration(root) if config is None else config
    if not isinstance(config, dict):
        raise ServiceError("La configuration de l’automatisation est invalide.")
    interval = config.get("interval_minutes", 15)
    if type(interval) is not int or not 1 <= interval <= 1440:
        raise ServiceError("L’intervalle doit être un entier entre 1 et 1 440 minutes.")
    return {
        "Label": LABEL,
        "ProgramArguments": [str(paths["python"]), str(paths["script"]), "--once"],
        "WorkingDirectory": str(paths["project"]),
        "StartInterval": interval * 60,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "StandardOutPath": str(paths["stdout"]),
        "StandardErrorPath": str(paths["stderr"]),
    }


def prepare_plist(root=None):
    """Stage a reviewable plist inside the project without registering it."""
    paths = _paths(root)
    if not paths["python"].is_file() or not os.access(paths["python"], os.X_OK):
        raise ServiceError("L’interpréteur .venv/bin/python est absent ou non exécutable.")
    if not paths["script"].is_file():
        raise ServiceError("Le script d’automatisation est introuvable dans le projet.")
    _atomic_bytes(paths["prepared"], plistlib.dumps(plist_payload(root)))
    return paths["prepared"]


def _target():
    return f"gui/{os.getuid()}/{LABEL}"


def _command(arguments, runner=None):
    try:
        return (runner or subprocess.run)(
            [LAUNCHCTL, *arguments], capture_output=True, text=True,
            check=False, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        raise ServiceError("Le service launchd est inaccessible ; aucune sortie technique n’est journalisée.") from None


def _assert_owned(path):
    if not path.exists():
        return
    try:
        payload = plistlib.loads(path.read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        raise ServiceError("Le fichier LaunchAgent existant est illisible ; modification refusée.") from None
    if not isinstance(payload, dict) or payload.get("Label") != LABEL:
        raise ServiceError("Le fichier LaunchAgent existant appartient à un autre service.")


def service_status(root=None, *, launch_agents_dir=None, runner=None):
    """Inspect config and launchd, without writes, API calls or worker execution.

    ``running`` means the agent is loaded, including the wait between cycles;
    ``process_running`` distinguishes a cycle currently executing.
    """
    paths = _paths(root, launch_agents_dir)
    result = {
        "label": LABEL, "supported": sys.platform == "darwin",
        "installed": paths["installed"].is_file(), "running": False,
        "process_running": False, "interval_seconds": 900,
        "plist_path": str(paths["installed"]),
        "prepared_plist_path": str(paths["prepared"]),
        "log_path": str(paths["stdout"]),
    }
    try:
        config = _configuration(root)
        result["interval_seconds"] = config["interval_minutes"] * 60
        result["configured_interval_seconds"] = result["interval_seconds"]
        result["enabled"] = config.get("enabled", True) is True
    except ServiceError as error:
        result["error"] = str(error)
    if result["installed"]:
        try:
            _assert_owned(paths["installed"])
            payload = plistlib.loads(paths["installed"].read_bytes())
            interval = payload.get("StartInterval")
            if type(interval) is not int or interval < 60:
                raise ServiceError("L’intervalle de l’agent installé est invalide.")
            result["interval_seconds"] = interval
            result["needs_reinstall"] = payload != plist_payload(root)
        except (OSError, ValueError, ServiceError):
            result["error"] = "L’agent installé ne correspond pas à la configuration ; réinstaller le service."
    if result["supported"]:
        try:
            response = _command(["print", _target()], runner)
            result["running"] = response.returncode == 0
            if result["running"]:
                result["process_running"] = bool(re.search(
                    r"^\s*state\s*=\s*running\s*$", response.stdout or "", re.MULTILINE))
        except ServiceError as error:
            result["error"] = str(error)
    return result


def install_service(root=None, *, launch_agents_dir=None, runner=None):
    """Explicitly install and load the user agent; never called on page render."""
    if sys.platform != "darwin":
        raise ServiceError("L’installation launchd nécessite macOS et une session utilisateur ouverte.")
    paths = _paths(root, launch_agents_dir)
    prepared = prepare_plist(root)
    _assert_owned(paths["installed"])
    previous = paths["installed"].read_bytes() if paths["installed"].exists() else None
    was_loaded = _command(["print", _target()], runner).returncode == 0
    if was_loaded and _command(["bootout", _target()], runner).returncode:
        raise ServiceError("L’ancien agent ne peut pas être arrêté ; installation interrompue.")
    try:
        _atomic_bytes(paths["installed"], prepared.read_bytes())
        response = _command(["bootstrap", f"gui/{os.getuid()}", str(paths["installed"])], runner)
        if response.returncode:
            raise ServiceError("launchd a refusé le chargement ; installation restaurée.")
    except (OSError, ServiceError):
        if previous is None:
            paths["installed"].unlink(missing_ok=True)
        else:
            _atomic_bytes(paths["installed"], previous)
            if was_loaded:
                try:
                    _command(["bootstrap", f"gui/{os.getuid()}", str(paths["installed"])], runner)
                except ServiceError:
                    pass
        raise ServiceError("L’installation a échoué ; le fichier précédent a été conservé si présent.") from None
    return {"status": "installed", "label": LABEL, "installed": True,
            "running": True, "interval_seconds": plistlib.loads(prepared.read_bytes())["StartInterval"],
            "plist_path": str(paths["installed"])}


def uninstall_service(root=None, *, launch_agents_dir=None, runner=None):
    """Unload only this agent and remove its plist; retain app data and logs."""
    if sys.platform != "darwin":
        raise ServiceError("La désinstallation launchd nécessite macOS.")
    paths = _paths(root, launch_agents_dir)
    _assert_owned(paths["installed"])
    if _command(["print", _target()], runner).returncode == 0:
        if _command(["bootout", _target()], runner).returncode:
            raise ServiceError("L’agent ne peut pas être arrêté ; son fichier est conservé.")
    paths["installed"].unlink(missing_ok=True)
    return {"status": "uninstalled", "label": LABEL,
            "installed": False, "running": False,
            "plist_path": str(paths["installed"])}


def _prepare_models(root=None, *, config=None):
    """Separate local phase: a provider failure need not prevent validation."""
    module = importlib.import_module('model_campaign_worker')
    arguments = {'root': _paths(root)['data']}
    if config is not None:
        arguments['config'] = config
    return module.run_cycle(**arguments)


def run_once(root=None, *, dry_run=False, config=None, skip_models=False):
    """Call the worker exactly once and return a log-safe summary only."""
    worker = _worker(root)  # Also makes the project importable for direct CLI use.
    cloud = importlib.import_module('cloud_storage')
    with cloud.transaction():
        summary = _run_once(worker, root, dry_run=dry_run, config=config, skip_models=skip_models)
        cloud.checkpoint()
        return summary


def _run_once(worker, root, *, dry_run, config, skip_models):
    arguments = {'root': _paths(root)['data'], 'dry_run': dry_run}
    if config is not None:
        arguments['config'] = config
    report = worker.run_cycle(**arguments)
    if not isinstance(report, dict) or report.get("status") not in _STATUSES:
        raise ServiceError("Le cycle a renvoyé un statut invalide.")
    summary = {"status": report["status"]}
    for field in _COUNTERS:
        value = report.get(field)
        if type(value) is int and value >= 0:
            summary[field] = value
    for field in ("started_at", "finished_at"):
        value = report.get(field)
        if isinstance(value, str) and re.fullmatch(r"[0-9TZ:+. -]{10,40}", value):
            summary[field] = value
    issues = report.get("issues")
    if isinstance(issues, (list, dict)):
        summary["issues"] = len(issues)
    if not dry_run and not skip_models and report['status'] not in ('busy', 'disabled'):
        model_report = (_prepare_models(root) if config is None else _prepare_models(root, config=config))
        if model_report.get('status') not in ('idle', 'disabled'):
            summary['models_status'] = model_report['status']
            summary['models_prepared'] = model_report.get('prepared', 0)
    return summary


def main(argv=None, *, root=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--once", action="store_true", help="Exécuter un cycle puis quitter.")
    action.add_argument("--install", action="store_true", help="Installer et démarrer l’agent utilisateur macOS.")
    action.add_argument("--uninstall", action="store_true", help="Arrêter et retirer l’agent, en conservant les données.")
    action.add_argument("--status", action="store_true", help="Afficher le statut sans requête API.")
    parser.add_argument("--dry-run", action="store_true", help="Simuler un cycle ; avec --install, préparer seulement le plist local.")
    options = parser.parse_args(argv)
    if options.dry_run and (options.status or options.uninstall):
        parser.error("--dry-run s’utilise seul, avec --once ou avec --install.")
    try:
        if options.install:
            if options.dry_run:
                report = {"status": "prepared", "label": LABEL,
                          "prepared_plist_path": str(prepare_plist(root)), "installed": False}
            else:
                report = install_service(root)
        elif options.uninstall:
            report = uninstall_service(root)
        elif options.once or options.dry_run:
            report = run_once(root, dry_run=options.dry_run)
        else:
            report = service_status(root)
    except ServiceError as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
        return 1
    except Exception:
        # Never print arbitrary worker/HTTP/subprocess exceptions or tracebacks.
        print(json.dumps({"status": "error", "error": "Action impossible ; vérifier la configuration et le statut local."}, ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 1 if report.get("status") == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
