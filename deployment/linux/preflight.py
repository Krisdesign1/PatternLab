#!/usr/bin/env python3
"""Read-only Linux deployment checks; never import PatternLab or run a cycle.

No network access, subprocesses, secret values or application data are emitted.
The full check is intended for the Linux service interpreter, not the Mac venv.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import os
from pathlib import Path
import platform
import pwd
import re
import stat
import sys
import tomllib


PYTHON_VERSION = (3, 12, 10)
PINNED_VERSIONS = {
    "streamlit": "1.64.0", "pandas": "2.3.3", "catboost": "1.2.10",
    "numpy": "2.5.3", "scipy": "1.18.1",
}
WORKER_APPROVAL = Path("/etc/patternlab/worker-cutover-approved")


class PreflightError(ValueError):
    """Fixed, credential-free messages safe for the service journal."""


def validate_domain(domain):
    """Accept one ASCII public DNS name, never a URL/Caddyfile fragment."""
    if not isinstance(domain, str) or len(domain) > 253:
        raise PreflightError("Supply one explicit public DNS hostname with --domain.")
    labels = domain.split(".")
    if (len(labels) < 2
            or any(not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
                   for label in labels)
            or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9-]*", labels[-1])
            or labels[-1].lower() in {"localhost", "local", "internal", "test", "invalid", "example"}
            or domain.lower() in {"example.com", "example.net", "example.org"}
            or domain.lower().endswith((".example.com", ".example.net", ".example.org"))):
        raise PreflightError("Supply one explicit public DNS hostname with --domain.")
    return domain.lower()


def render_caddy(domain):
    domain = validate_domain(domain)
    template = Path(__file__).with_name("Caddyfile.template").read_text(encoding="utf-8")
    return template.replace("@@PATTERNLAB_DOMAIN@@", domain)


def render_web_environment(domain):
    # One public value, safe for systemd EnvironmentFile and ExecStart expansion.
    return f"PATTERNLAB_DOMAIN={validate_domain(domain)}\n"


def validate_platform():
    try:
        release = platform.freedesktop_os_release()
    except OSError:
        raise PreflightError("Ubuntu 24.04 x86_64 is required.") from None
    if (platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}
            or release.get("ID") != "ubuntu" or release.get("VERSION_ID") != "24.04"):
        raise PreflightError("Ubuntu 24.04 x86_64 is required.")


def validate_runtime(root):
    if tuple(sys.version_info[:3]) != PYTHON_VERSION:
        raise PreflightError("Python must remain exactly 3.12.10 for the frozen protocol.")
    if Path(sys.prefix) != root / ".venv" or sys.prefix == sys.base_prefix:
        raise PreflightError("Run preflight with /opt/patternlab/.venv/bin/python.")
    try:
        for package, expected in PINNED_VERSIONS.items():
            if importlib.metadata.version(package) != expected:
                raise PreflightError("A frozen runtime version differs; do not relax the Linux constraints.")
        # Exercise native-library loading without importing application modules.
        for module in ("numpy.linalg", "scipy.linalg", "catboost", "pandas", "streamlit",
                       "reportlab", "certifi", "psycopg"):
            importlib.import_module(module)
    except importlib.metadata.PackageNotFoundError:
        raise PreflightError("A required frozen runtime package is missing.") from None
    except PreflightError:
        raise
    except Exception:
        raise PreflightError("Runtime imports failed; validate the Linux wheels and native libraries.") from None


def _metadata(path):
    if path.is_symlink():
        raise PreflightError("Deployment control paths must not be symbolic links.")
    try:
        return path.stat()
    except OSError:
        raise PreflightError("A required deployment path is missing or inaccessible.") from None


def _root_owned(path):
    info = _metadata(path)
    if info.st_uid != 0 or info.st_mode & 0o022:
        raise PreflightError("Application code and deployment controls must be root-owned and not writable by other users.")
    return info


def validate_layout(root, user_id):
    # Source, environment and controls stay immutable to the service account.
    for directory in (root, root / "scripts", root / "deployment", root / "deployment/linux",
                      root / ".streamlit", root / ".venv", root / ".venv/bin"):
        if not stat.S_ISDIR(_root_owned(directory).st_mode):
            raise PreflightError("A required deployment directory is invalid.")
    for name in ("app.py", "scripts/automation_service.py", "deployment/linux/preflight.py"):
        if not stat.S_ISREG(_root_owned(root / name).st_mode):
            raise PreflightError("A required application entry point is invalid.")
    # The app's unchanged source uses data/ relative to its installation root.
    data = _metadata(root / "data")
    if (not stat.S_ISDIR(data.st_mode) or data.st_uid != user_id
            or stat.S_IMODE(data.st_mode) != 0o700):
        raise PreflightError("The local data directory must belong to patternlab with mode 0700.")


def validate_secrets(path, user_id, *, worker=False):
    info = _metadata(path)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != user_id
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise PreflightError("The user-supplied secrets file must belong to patternlab with mode 0600.")
    try:
        with path.open("rb") as handle:
            values = tomllib.load(handle)
    except (OSError, ValueError):
        raise PreflightError("The user-supplied secrets file is inaccessible or invalid TOML.") from None
    access = values.get("access")
    if not isinstance(access, dict) or access.get("local_owner") is not False:
        raise PreflightError("Set access.local_owner = false before exposing the loopback server through HTTPS.")
    if (values.get("PATTERNLAB_STORAGE_MODE") != "local"
            or os.environ.get("PATTERNLAB_STORAGE_MODE", "local") != "local"):
        raise PreflightError("Local storage must be selected explicitly in secrets and the service environment.")
    if worker:
        key = values.get("API_FOOTBALL_KEY")
        section = values.get("api_football", {})
        if not key and isinstance(section, dict):
            key = section.get("key")
        if not isinstance(key, str) or not key.strip():
            raise PreflightError("Supply the API key on the Linux host before enabling its worker.")


def validate_worker_approval(path=WORKER_APPROVAL):
    info = _root_owned(path)
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise PreflightError("The deliberate worker cutover marker must be a root-owned 0600 regular file.")
    # The service needs only stat access; the root-only marker contains no key.
    _root_owned(path.parent)


def validate_web_environment(path=Path("/etc/patternlab/web.env")):
    info = _root_owned(path)
    if not stat.S_ISREG(info.st_mode):
        raise PreflightError("The public web-domain environment file must be a regular file.")
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise PreflightError("The public web-domain environment file is inaccessible.") from None
    prefix = "PATTERNLAB_DOMAIN="
    if not content.startswith(prefix) or content != render_web_environment(content[len(prefix):].rstrip("\n")):
        raise PreflightError("The public web-domain environment file must contain only the validated DNS hostname.")
    _root_owned(path.parent)


def check(root, *, worker=False):
    root = Path(root)
    if not root.is_absolute():
        raise PreflightError("Use an absolute application root.")
    validate_platform()
    try:
        account = pwd.getpwnam("patternlab")
    except KeyError:
        raise PreflightError("Create the non-root patternlab service account with install.sh first.") from None
    if account.pw_uid == 0 or account.pw_dir != "/var/lib/patternlab":
        raise PreflightError("The patternlab account must be non-root with home /var/lib/patternlab.")
    validate_layout(root, account.pw_uid)
    validate_secrets(root / ".streamlit/secrets.toml", account.pw_uid, worker=worker)
    validate_web_environment()
    if worker:
        validate_worker_approval()
    validate_runtime(root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/opt/patternlab"))
    parser.add_argument("--worker", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--platform-only", action="store_true")
    mode.add_argument("--render-caddy", metavar="DOMAIN")
    mode.add_argument("--render-web-env", metavar="DOMAIN")
    options = parser.parse_args(argv)
    try:
        if options.render_caddy is not None:
            print(render_caddy(options.render_caddy), end="")
            return 0
        if options.render_web_env is not None:
            print(render_web_environment(options.render_web_env), end="")
            return 0
        if options.platform_only:
            validate_platform()
        else:
            check(options.root, worker=options.worker)
    except PreflightError as error:
        print(f"Preflight refused: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print("Preflight refused: deployment metadata could not be validated.", file=sys.stderr)
        return 1
    print("Preflight passed. No worker cycle or network request was performed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
