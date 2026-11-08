#!/usr/bin/env python3
"""Qualify one retained Lot14 prediction on a separate Linux snapshot.

Run with the installation's Linux Python using -I -B. This reads a supplied,
explicitly marked disposable copy; it neither creates that copy nor writes a
receipt. It does not run the worker or change the Mac-only environment guard.
Use the systemd isolation command in README.md for OS-level protection too.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing, redirect_stderr, redirect_stdout
from hashlib import sha256
import importlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
from unittest.mock import patch


VERSION = "patternlab-linux-frozen-replay-v1"
MARKER_VERSION = "patternlab-linux-frozen-replay-sandbox-v1"
INSTALLATION_ROOT = Path(__file__).resolve().parents[2]
LIVE_ROOT = Path("/opt/patternlab")
MARKER = ".linux-replay-sandbox.json"
PRESERVATION = "audit/PATTERNLAB2_PRESERVATION_LOT14_2026-11-02.json"
PRESERVATION_SHA256 = "0f40a424772a4ade28f2a81d47030e01263d5b61a31f63947a84111cf1d7e8d9"
RECORDED_REPLAY = "audit/PATTERNLAB2_LOT14_SOURCES_2026-11-02/restoration/probe.json"
RECORDED_REPLAY_SHA256 = "09fb8857595f8c62826bb014e8af15bcb82dc832a3a58185bca0008caaaeff21"
FROZEN_FILES = frozenset({
    "api_football.py", "api_match_workspace.py", "automation_worker.py",
    "causal_candidate.py", "causal_confirmation.py", "causal_confirmation_diagnostics.py",
    "causal_confirmation_followup.py", "causal_confirmation_store.py",
    "causal_confirmation_worker.py", "causal_feature_artifacts.py",
    "causal_feature_workspace.py", "causal_population.py", "club_identity.py",
    "fixture_import_policy.py", "fixture_store.py", "fixture_temporal.py",
    "fixture_temporal_store.py", "halftime_domain.py", "league_model.py",
    "match_predictor.py", "prematch_feature_snapshot.py", "scenario_features.py",
})
TOLERANCE = 1e-12
PRIVATE_ENVIRONMENT = (
    "API_FOOTBALL_KEY", "PATTERNLAB_DATABASE_URL", "PATTERNLAB_CLOUD_REQUIRED",
    "PATTERNLAB_CLOUD_ADOPT_LOCAL", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS",
)
_HASH = re.compile(r"[a-f0-9]{64}")


class ReplayError(ValueError):
    """Fixed reason code; never carry a path, private value or exception text."""


def fingerprint(path):
    result = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def safe_path(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ReplayError("unsafe_snapshot_path")
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ReplayError("snapshot_links_forbidden")
    if not current.resolve().is_relative_to(root):
        raise ReplayError("unsafe_snapshot_path")
    return current


def validate_copy(given):
    given = Path(given)
    if not given.is_absolute():
        raise ReplayError("absolute_disposable_root_required")
    current = Path(given.anchor)
    for part in given.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ReplayError("snapshot_links_forbidden")
    root = given.resolve()
    for live in (LIVE_ROOT.resolve(), INSTALLATION_ROOT.resolve()):
        if root.is_relative_to(live) or live.is_relative_to(root):
            raise ReplayError("live_tree_forbidden")
    if not root.is_dir():
        raise ReplayError("disposable_copy_missing")
    # This marker is an explicit operator declaration, not a snapshot creator.
    try:
        marker = json.loads(safe_path(root, MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ReplayError("disposable_marker_required") from None
    if marker != dict(version=MARKER_VERSION, project_root=str(root), disposable=True, production=False):
        raise ReplayError("disposable_marker_invalid")
    snapshot_metadata(root)
    if os.environ.get("PATTERNLAB_STORAGE_MODE") != "local":
        raise ReplayError("explicit_local_environment_required")
    if any(os.environ.get(name) for name in PRIVATE_ENVIRONMENT):
        raise ReplayError("private_environment_forbidden")
    return root


def snapshot_metadata(root):
    result = {}
    for parent, directories, names in os.walk(root, followlinks=False):
        for name in sorted([*directories, *names]):
            path = Path(parent) / name
            relative = path.relative_to(root)
            if (name in {".venv", ".aws", ".ssh", ".git", ".local_backups", "credentials"}
                    or name.startswith(".env") or name == "secrets.toml"
                    or name in {"id_rsa", "id_ed25519"} or path.suffix == ".pem"):
                raise ReplayError("private_or_environment_files_forbidden")
            if name.endswith(("-wal", "-shm", "-journal")):
                raise ReplayError("sqlite_snapshot_sidecars_forbidden")
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ReplayError("snapshot_links_forbidden")
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise ReplayError("snapshot_special_files_forbidden")
            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                raise ReplayError("snapshot_hardlinks_forbidden")
            result[relative.as_posix()] = (info.st_mode, info.st_size, info.st_mtime_ns,
                                          info.st_dev, info.st_ino)
    return result


def reference(root):
    def checked_json(relative, expected):
        path = safe_path(root, relative)
        if fingerprint(path) != expected:
            raise ReplayError("lot14_evidence_hash_mismatch")
        return json.loads(path.read_text(encoding="utf-8"))
    preserved = checked_json(PRESERVATION, PRESERVATION_SHA256)
    recorded = checked_json(RECORDED_REPLAY, RECORDED_REPLAY_SHA256)
    sources = preserved.get("frozen_source_sha256")
    if (not isinstance(sources, dict) or set(sources) != FROZEN_FILES
            or any(not isinstance(value, str) or not _HASH.fullmatch(value) for value in sources.values())):
        raise ReplayError("frozen_22_inventory_invalid")
    replay = [item for item in recorded.get("checks", []) if item.get("component") == "candidate_replay"]
    if (len(replay) != 1 or replay[0].get("state") != "passed"
            or replay[0].get("model_binary_loaded") is not True):
        raise ReplayError("recorded_replay_invalid")
    candidate = preserved["candidate_integrity"]
    if (candidate.get("status") != "valid" or replay[0].get("candidate_id") != candidate.get("candidate_id")
            or any(not _HASH.fullmatch(replay[0].get(name, "")) for name in ("forecast_id", "candidate_id"))):
        raise ReplayError("recorded_candidate_invalid")
    return dict(sources=sources, replay=replay[0], candidate=candidate)


def verify_sources(root, saved):
    if set(saved["sources"]) != FROZEN_FILES:
        raise ReplayError("frozen_22_inventory_invalid")
    for name, expected in saved["sources"].items():
        if fingerprint(safe_path(root, name)) != expected:
            raise ReplayError("frozen_source_hash_mismatch")
    # Supporting inference modules are also anchored by Lot14's evidence.
    for name, expected in saved["candidate"]["implementation_sha256"].items():
        if fingerprint(safe_path(root, name)) != expected:
            raise ReplayError("candidate_implementation_hash_mismatch")


def artifact_paths(root, saved):
    directory = Path("data/api_football/causal_confirmation/candidates") / saved["candidate"]["candidate_id"]
    return {name: safe_path(root, directory / name) for name in saved["candidate"]["artifact_sha256"]}


def verify_artifacts(root, saved):
    for name, path in artifact_paths(root, saved).items():
        if fingerprint(path) != saved["candidate"]["artifact_sha256"][name]:
            raise ReplayError("registered_artifact_hash_mismatch")


class ReadOnlyGuard:
    """Python audit guard supplements the OS sandbox; it is process-wide."""
    def __init__(self, database):
        self.database = database.as_uri() + "?mode=ro&immutable=1"
        self.denied = dict(network=0, credential=0, training=0, write=0, process=0)

    def refuse(self, category):
        self.denied[category] += 1
        raise ReplayError(category + "_forbidden")

    def audit(self, event, arguments):
        if event.startswith("socket."):
            self.refuse("network")
        if event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.exec", "os.fork", "os.forkpty", "pty.spawn"}:
            self.refuse("process")
        if event in {"os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.link", "os.symlink",
                     "os.truncate", "os.chmod", "os.chown", "os.utime", "os.setxattr", "os.removexattr"}:
            self.refuse("write")
        if event == "open":
            path, mode, flags = arguments
            if isinstance(path, (str, bytes, os.PathLike)):
                parts = Path(os.fsdecode(path)).parts
                if any(part in {"secrets.toml", ".aws", ".ssh", "credentials"} or part.startswith(".env")
                       for part in parts):
                    self.refuse("credential")
            write_flags = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
            if ((isinstance(mode, str) and any(char in mode for char in "wax+"))
                    or isinstance(flags, int) and flags & write_flags):
                self.refuse("write")
        if event == "sqlite3.connect" and str(arguments[0]) != self.database:
            self.refuse("write")

    def sqlite_connect(self, original, database, *args, **kwargs):
        # The unchanged store requests mode=ro. Immutable prevents SQLite from
        # creating WAL/SHM sidecars when reading a closed, consistent snapshot.
        if str(database) != self.database.removesuffix("&immutable=1") or kwargs.get("uri") is not True:
            self.refuse("write")
        return original(self.database, *args, **kwargs)


def _module(name, root):
    module = importlib.import_module(name)
    if not Path(module.__file__).resolve().is_relative_to(root):
        raise ReplayError("application_import_outside_disposable_copy")
    return module


def compare_probabilities(actual, expected):
    if not isinstance(actual, dict) or not isinstance(expected, dict) or not expected or set(actual) != set(expected):
        raise ReplayError("replay_class_set_invalid")
    for values in (actual, expected):
        if (any(type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1
                for value in values.values()) or abs(sum(values.values()) - 1) > 1e-9):
            raise ReplayError("replay_probabilities_invalid")
    difference = max(abs(actual[key] - expected[key]) for key in expected)
    if difference > TOLERANCE:
        raise ReplayError("frozen_probability_mismatch")
    return difference


def replay(root, saved, store, candidates, integrity):
    """Same load/predict comparison as Lot14, selecting its exact saved row."""
    data = root / "data/api_football"
    with closing(store._connect(data)) as connection:
        row = connection.execute("SELECT * FROM forecasts WHERE id=?", (saved["replay"]["forecast_id"],)).fetchone()
        if row is None:
            raise ReplayError("recorded_forecast_missing")
        payload = store._forecast(connection, row)["payload"]
        protocol = store._protocol(connection, payload["protocol_id"])
    # The saved row belongs to the original protocol. The later active
    # followup in preservation evidence must not replace this registration.
    candidate = saved["candidate"]
    if (payload["id"] != saved["replay"]["forecast_id"] or payload["candidate_id"] != candidate["candidate_id"]
            or protocol.get("candidate_id") != candidate["candidate_id"]
            or protocol.get("package_id") != candidate["package_id"]
            or protocol.get("candidate_files") != {name: candidate["artifact_sha256"][name]
                                                   for name in ("candidate.cbm", "report.json")}):
        raise ReplayError("recorded_registration_mismatch")
    proof = integrity.inspect(data, protocol)
    if (proof.get("status") != "valid" or proof.get("candidate_id") != candidate["candidate_id"]
            or proof.get("package_id") != candidate["package_id"]
            or proof.get("artifact_sha256") != candidate["artifact_sha256"]):
        raise ReplayError("registered_candidate_integrity_failed")
    directory = artifact_paths(root, saved)["candidate.cbm"].parent
    loaded = candidates.load_candidate(directory)
    actual = candidates.predict_candidate(loaded, [payload["snapshot"]["features"]])
    if not isinstance(actual, list) or len(actual) != 1:
        raise ReplayError("replay_prediction_invalid")
    difference = compare_probabilities(actual[0], payload["methods"]["candidate"])
    return dict(forecast_id=payload["id"], protocol_id=protocol["id"], candidate_id=candidate["candidate_id"],
                package_id=candidate["package_id"], max_absolute_probability_difference=difference)


def run(root, saved):
    database = safe_path(root, "data/api_football/causal_confirmation.sqlite3")
    before = snapshot_metadata(root)
    database_before = fingerprint(database)
    verify_sources(root, saved)
    verify_artifacts(root, saved)
    guard = ReadOnlyGuard(database)
    sys.dont_write_bytecode = True
    sys.addaudithook(guard.audit)
    os.chdir(root)
    runtime_check()
    sys.path.insert(0, str(root))
    original_connect = sqlite3.connect
    with ExitStack() as guards:
        guards.enter_context(patch.object(sqlite3, "connect", side_effect=lambda *a, **k: guard.sqlite_connect(original_connect, *a, **k)))
        import catboost
        for owner, name in ((catboost.CatBoost, "fit"), (catboost.CatBoostClassifier, "fit"),
                            (catboost.CatBoostRegressor, "fit"), (catboost, "train"), (catboost, "cv")):
            guards.enter_context(patch.object(owner, name, side_effect=lambda *a, **k: guard.refuse("training")))
        api = _module("api_football", root)
        guards.enter_context(patch.object(api, "load_key", side_effect=lambda: guard.refuse("credential")))
        guards.enter_context(patch.object(api.ApiFootballClient, "get", side_effect=lambda *a, **k: guard.refuse("network")))
        candidates = _module("causal_candidate", root)
        guards.enter_context(patch.object(candidates, "fit_candidate", side_effect=lambda *a, **k: guard.refuse("training")))
        guards.enter_context(patch.object(candidates, "write_candidate", side_effect=lambda *a, **k: guard.refuse("write")))
        store = _module("causal_confirmation_store", root)
        integrity = _module("active_candidate_integrity", root)
        result = replay(root, saved, store, candidates, integrity)
    if any(guard.denied.values()):
        raise ReplayError("forbidden_operation_attempted")
    verify_sources(root, saved)
    verify_artifacts(root, saved)
    if fingerprint(database) != database_before or snapshot_metadata(root) != before:
        raise ReplayError("disposable_copy_changed")
    return dict(version=VERSION, state="passed", **result, frozen_source_files_verified=len(FROZEN_FILES),
                tolerance=TOLERANCE, model_binary_loaded=True, new_forecast_published=False,
                training_performed=False, worker_executed=False, predictive_evaluation=False,
                database_sha256_unchanged=True, snapshot_metadata_unchanged=True,
                denied_operations=guard.denied, linux_host_qualification=True)


def runtime_check():
    # Import only the adjacent trusted deployment preflight, never Mac package
    # rehydration helpers or a module selected by the disposable sys.path.
    path = Path(__file__).with_name("preflight.py")
    spec = importlib.util.spec_from_file_location("patternlab_linux_replay_preflight", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.validate_platform()
    module.validate_runtime(INSTALLATION_ROOT)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--copy-root", type=Path, required=True)
    options = parser.parse_args(argv)
    try:
        if not sys.flags.isolated or not sys.dont_write_bytecode:
            raise ReplayError("isolated_python_and_no_bytecode_required")
        if os.geteuid() == 0:
            raise ReplayError("non_root_qualification_user_required")
        root = validate_copy(options.copy_root)
        saved = reference(root)
        verify_sources(root, saved)
        verify_artifacts(root, saved)
        # Suppress arbitrary dependency/application output; only this fixed
        # JSON result reaches stdout. There is no output-path/write option.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = run(root, saved)
    except ReplayError as error:
        result = dict(version=VERSION, state="failed", reason=str(error))
    except Exception:
        result = dict(version=VERSION, state="failed", reason="frozen_replay_refused_or_unavailable")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0 if result["state"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
