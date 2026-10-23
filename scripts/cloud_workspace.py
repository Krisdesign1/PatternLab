"""Explicit cloud setup and runs, independent of Streamlit visitors.

Credentials come from environment variables or the local secrets file.  They
are never command-line arguments or part of the JSON summaries printed here.
"""
from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import re
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import automation_service as service


# Leave time to publish the final state before the free runner's job timeout.
# This bounds execution time, never the number of provider requests.
CYCLE_DURATION_SECONDS = 240
_STATUSES = frozenset(("completed", "paused", "error", "busy", "disabled", "dry_run",
                       "ready", "seeded", "initialized", "not_configured", "empty"))
_COUNTERS = frozenset(("revision", "files", "bytes", "compressed_bytes", "requests",
                      "created", "already_recorded", "history_imports", "history_received",
                      "coverage_competitions", "groups_considered", "settled", "pending",
                      "issues", "requests_today", "models_prepared"))


class CloudCommandError(ValueError):
    """An actionable, fixed message that does not include provider exceptions."""


def _storage():
    return importlib.import_module("cloud_storage")


def _worker():
    return importlib.import_module("automation_worker")


def _safe_summary(value):
    """Whitelist log metadata; never forward arbitrary backend payloads."""
    if not isinstance(value, dict):
        raise CloudCommandError("Le stockage Cloud a renvoyé un état invalide.")
    safe = {}
    if value.get("status") in _STATUSES:
        safe["status"] = value["status"]
    for field in _COUNTERS:
        number = value.get(field)
        if type(number) is int and number >= 0:
            safe[field] = number
    for field in ("configured", "initialized"):
        if type(value.get(field)) is bool:
            safe[field] = value[field]
    workspace = value.get("workspace")
    if isinstance(workspace, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", workspace):
        safe["workspace"] = workspace
    for field in ("started_at", "finished_at", "updated_at"):
        stamp = value.get(field)
        if isinstance(stamp, str) and re.fullmatch(r"[0-9TZ:+. -]{10,40}", stamp):
            safe[field] = stamp
    if value.get("models_status") in ("idle", "disabled", "busy", "completed", "error"):
        safe["models_status"] = value["models_status"]
    return safe


def cloud_status():
    """Read storage metadata only; do not restore files or call API-Football."""
    storage = _storage()
    if not storage.configured():
        return {"status": "not_configured", "configured": False, "initialized": False}
    report = _safe_summary(storage.status())
    report.update(configured=True)
    report["status"] = "ready" if report.get("initialized") else "empty"
    return report


def _require_configuration(storage):
    if not storage.configured():
        raise CloudCommandError(
            "Configurez PATTERNLAB_DATABASE_URL dans les secrets privés avant cette action.")


def seed_workspace():
    """Publish the local workspace only when the remote workspace is empty."""
    storage = _storage()
    _require_configuration(storage)
    status = storage.status()
    if status.get("initialized") or status.get("revision", 0) != 0:
        raise CloudCommandError(
            "Le stockage contient déjà un espace : aucune donnée distante n’a été remplacée.")
    report = _safe_summary(storage.seed())
    report["status"] = "seeded"
    return report


def runtime_config(value, *, models=False):
    """Normalize old settings without imposing any API request budget."""
    config = _worker()._config(value)
    if not models:
        config.update(models_enabled=False, model_jobs_per_cycle=0)
    return config


def run_workspace(*, root=None, models=False):
    """Restore, run under the remote lease, and checkpoint before releasing it."""
    storage = _storage()
    _require_configuration(storage)
    status = storage.status()
    if not status.get("initialized") or type(status.get("revision")) is not int or status["revision"] < 1:
        raise CloudCommandError(
            "Le stockage est vide : lancez d’abord seed depuis le Mac qui contient votre historique.")
    api = importlib.import_module("api_football")
    if not api.load_key():
        raise CloudCommandError("Configurez API_FOOTBALL_KEY dans les secrets privés.")
    project = Path(root).resolve() if root is not None else PROJECT_ROOT
    with storage.transaction():
        storage.assert_lease()
        config = runtime_config(_worker().load_config(project / "data" / "api_football"), models=models)
        result = service.run_once(root=project, config=config, skip_models=not models,
                                  max_duration_seconds=CYCLE_DURATION_SECONDS)
        storage.assert_lease()
        storage.checkpoint()
    report = _safe_summary(result)
    if report.get("status") not in ("completed", "paused", "error", "busy", "disabled", "dry_run"):
        raise CloudCommandError("Le cycle Cloud a renvoyé un état invalide.")
    return report


def main(argv=None, *, root=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="Vérifier la connexion et l’initialisation, sans appel API-Football.")
    commands.add_parser("status", help="Afficher uniquement les métadonnées du stockage distant.")
    commands.add_parser("seed", help="Initialiser un stockage vide depuis les données locales existantes.")
    run = commands.add_parser("run", help="Exécuter et sauvegarder un cycle Cloud sans plafond de requêtes applicatif.")
    run.add_argument("--models", action="store_true",
                     help="Autoriser aussi un job modèle configuré ; réservé à un passage manuel mesuré.")
    options = parser.parse_args(argv)
    try:
        if options.command in ("check", "status"):
            report = cloud_status()
        elif options.command == "seed":
            report = seed_workspace()
        else:
            report = run_workspace(root=root, models=options.models)
    except CloudCommandError as error:
        report = {"status": "error", "error": str(error)}
    except Exception:
        # Database/HTTP exceptions can contain a DSN or credentials.  The fixed
        # message is intentionally the only error exposed to job logs.
        report = {"status": "error", "error": (
            "Action Cloud interrompue : vérifier les secrets, la connexion, les quotas "
            "et le verrou distant. Aucun détail de connexion n’est affiché.")}
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 1 if report.get("status") in ("error", "not_configured") else 0


if __name__ == "__main__":
    raise SystemExit(main())
