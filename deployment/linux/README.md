# PatternLab Linux staging kit

This is a new-host kit for **Ubuntu 24.04, x86_64, systemd** at the fixed path
`/opt/patternlab`. It changes no application source or saved configuration.
Installation stages files and reloads systemd; it starts and enables no service.
The Mac worker remains authoritative throughout staging.

The web app binds **127.0.0.1:8501**. A separate, non-root Caddy service exposes
HTTPS for one explicitly supplied public DNS hostname. Application files and
the Linux virtual environment belong to root. Only `data/` and the service's
private `/var/lib/patternlab` directory are writable by the `patternlab` account.
Both services force local storage. Persistent data, models and certificates
must live on the instance's persistent, encrypted volume, not instance-store
or `/tmp`. Arrange volume snapshots/backups separately before cutover.

## Frozen-runtime gate

The Mac runtime is Python **3.12.10**, Streamlit **1.64.0**, pandas **2.3.3**,
CatBoost **1.2.10**, NumPy **2.5.3**, SciPy **1.18.1**. The separate Linux
requirements constrain those package versions; the existing requirements file
is untouched. This is a compatibility constraint set, **not a complete
transitive dependency lock or a claim that matching Linux wheels exist**.

No Linux wheel installation, native ABI test, server boot, frozen-artifact
equivalence check or AWS deployment has been performed by creating this kit.
Python supplied by Ubuntu 24.04 need not have the required patch version.
Provision exactly Python 3.12.10 using a trusted, verified distribution. Do not
copy the Mac `.venv`, relax the constraints, retrain models, alter the frozen
protocol, search new parameters or backfill journal forecasts to get a green
check. A version/import check alone does not prove numerical equivalence.
Run the accepted frozen-source and frozen-artifact checks on a disposable Linux
copy before authorizing a worker cutover. Keep the audit evidence in the bundle.

### Guarded Linux replay qualification

The dedicated `frozen_replay.py` reads an **already supplied disposable copy**;
it never creates or repairs a snapshot. It anchors both Lot14 evidence JSON
files by their fixed SHA256 hashes, verifies exactly 22 frozen sources plus
supporting inference sources, checks the registered candidate's artifact bytes,
and reproduces the exact saved Lot14 forecast within `1e-12`. The forecast's
own original registration is checked; the later followup protocol does not
replace it. Matching one retained prediction is a compatibility check, not a
predictive evaluation or permission to activate a worker.

Restore the reviewed source/audit/data bundle into a **new separate** directory,
for example `/var/lib/patternlab-qualification/replay-20261103`. It must contain
regular independent copies of the saved files, with no symbolic/hard links,
`.venv`, `.git`, `.local_backups`, `.aws`, `.ssh`, `.env*`, private keys or real
`secrets.toml`. SQLite WAL/SHM/journal sidecars are rejected: use the verified,
closed database snapshots from the staging bundle. Do not copy a running data
directory. Leave its snapshot files readable and parent directories traversable
to `patternlab`; the runner does not alter permissions or contents.
Keep this copy outside `/opt/patternlab` and outside any live project tree.

Declare only this new copy disposable before running qualification:

```bash
sudo /opt/patternlab/.venv/bin/python -I -B -c 'import json; from pathlib import Path; root=Path("/var/lib/patternlab-qualification/replay-20261103").resolve(); marker=root/".linux-replay-sandbox.json"; payload=dict(version="patternlab-linux-frozen-replay-sandbox-v1", project_root=str(root), disposable=True, production=False); f=marker.open("x"); json.dump(payload,f); f.close()'
```

Use the **new Linux venv**, with the exact versions above. Run it as a non-root
account with a read-only filesystem and no network. On the Ubuntu systemd host:

```bash
sudo systemd-run --wait --collect --pipe --unit=patternlab-frozen-replay-qualification \
  --property=User=patternlab --property=Group=patternlab \
  --property=NoNewPrivileges=yes --property=CapabilityBoundingSet= \
  --property=ProtectSystem=strict --property=ProtectHome=yes \
  --property=PrivateTmp=yes --property=PrivateDevices=yes \
  --property=PrivateNetwork=yes --property=RestrictAddressFamilies=AF_UNIX \
  --property=ReadOnlyPaths=/var/lib/patternlab-qualification/replay-20261103 \
  --property='InaccessiblePaths=-/opt/patternlab/data -/opt/patternlab/.streamlit/secrets.toml' \
  --setenv=PATTERNLAB_STORAGE_MODE=local --setenv=PYTHONDONTWRITEBYTECODE=1 \
  /opt/patternlab/.venv/bin/python -I -B \
  /opt/patternlab/deployment/linux/frozen_replay.py \
  --copy-root /var/lib/patternlab-qualification/replay-20261103
```

The runner adds Python audit guards against network access, credential reads,
process creation and file changes. CatBoost fitting, candidate writing and API
key loading are separately blocked. SQLite opens only the expected snapshot DB
with `mode=ro&immutable=1`; the worker is never imported or executed. Source,
artifact and database hashes plus snapshot metadata are checked again after
inference. Python guards do not sandbox arbitrary native-library syscalls, so
retain the OS isolation above. The marker is an operator declaration and cannot
detect an unrelated writer using the same snapshot; supply a quiescent copy.

Require exit status zero and one JSON result with `state="passed"`,
`linux_host_qualification=true`, `frozen_source_files_verified=22`, probability
difference at most `1e-12`, and all `denied_operations` counters zero. The runner
prints its receipt to stdout and offers no file output or tolerance override.
Preserve that result externally with the deployment audit. The existing Mac
environment snapshot/rebuild guard remains unchanged; it is not used here.

The normal `scripts/automation_service.py --once` also prepares models. The
new `run_worker.py --once` instead calls that unchanged module's `run_once`
with **`skip_models=True` and `models_enabled=False` / `model_jobs_per_cycle=0`**.
These independently disable post-cycle model preparation and in-cycle training.
The stored automation configuration is not edited. Do not invoke the original
CLI directly on this staged host during the freeze.

## Prepare the host without activating a worker

1. Stage the reviewed source/audit bundle at `/opt/patternlab`, owned by root
   and without group/other write permission. Exclude Mac environments, secrets,
   `.env*`, SSH/AWS credentials, logs and running-process lock files. Restore
   application data separately from an audited, consistent snapshot. Preserve
   the frozen source hashes. Do not use a live recursive copy of SQLite files.
2. Provision the exact Linux Python interpreter, then create a **new** venv.
   Use a trusted wheel index or a previously verified Linux wheelhouse. Fail
   on unavailable binaries rather than falling back to source builds:

   ```bash
   sudo /PATH/TO/VERIFIED/PYTHON-3.12.10 -m venv /opt/patternlab/.venv
   sudo /opt/patternlab/.venv/bin/python -m pip install --only-binary=:all: -r /opt/patternlab/deployment/linux/requirements-linux.txt
   sudo /opt/patternlab/.venv/bin/python -m pip check
   ```

   For an offline wheelhouse, add `--no-index --find-links=/PATH/TO/WHEELS`.
   Neither this kit nor its installer downloads an interpreter or dependencies.
3. Install Caddy with the host's reviewed package workflow. Its package may
   enable the stock `caddy.service`; review it, then disable that stock service
   on this dedicated new host before using the separate PatternLab proxy:

   ```bash
   sudo systemctl disable --now caddy.service
   sudo bash /opt/patternlab/deployment/linux/install.sh --domain YOUR_PUBLIC_DNS_NAME
   ```

   A real domain is mandatory. IP addresses, URLs, wildcards and example
   domains are rejected. Installation refuses existing PatternLab control
   files and leaves them for a manual upgrade review. It creates the non-login
   service account and a new empty data directory only if one does not exist.
4. Supply **new server secrets** yourself. The template contains no credential:

   ```bash
   sudo install -o patternlab -g patternlab -m 0600 /opt/patternlab/deployment/linux/secrets.toml.example /opt/patternlab/.streamlit/secrets.toml
   sudoedit /opt/patternlab/.streamlit/secrets.toml
   ```

   Keep `PATTERNLAB_STORAGE_MODE = "local"` and `[access] local_owner = false`.
   Loopback binding behind a public proxy must never grant local-owner access.
   For administrator sign-in, configure the existing OIDC support, register
   `https://YOUR_REAL_DOMAIN/oauth2callback`, generate a fresh cookie secret and
   set an explicit administrator allowlist. Until then, administration is
   read-only; public consultation is the application's existing behavior.
   No provider secret belongs in a unit file, Caddyfile, shell command or log.
5. Restore the approved local data snapshot. Preserve its contents and frozen
   artifacts; give the real, non-symlink data directory and its contents to the
   `patternlab` account. After the snapshot has been checked for unsafe paths:

   ```bash
   sudo chown -R --no-dereference patternlab:patternlab /opt/patternlab/data
   sudo chmod 0700 /opt/patternlab/data
   sudo -u patternlab -H env PYTHONDONTWRITEBYTECODE=1 PATTERNLAB_STORAGE_MODE=local /opt/patternlab/.venv/bin/python /opt/patternlab/deployment/linux/preflight.py
   sudo systemd-analyze verify /etc/systemd/system/patternlab-web.service /etc/systemd/system/patternlab-worker.service /etc/systemd/system/patternlab-worker.timer /etc/systemd/system/patternlab-proxy.service
   ```

   Preflight reads only deployment metadata, the supplied server secrets and
   installed package metadata/imports. It does not import application modules,
   start a cycle, query a provider, train a model or modify application data.

## Start the web service separately

Point the domain's DNS to the server. Allow ingress TCP 80/443, restrict SSH to
the administrator's address, and leave 8501 closed. Review the rendered
`/etc/patternlab/Caddyfile` and `/etc/patternlab/web.env` before starting. The
public domain is also passed to Streamlit's browser URL, Host allowlist and CORS
allowlist, while its listener stays on loopback and XSRF/CORS remain enabled.
Caddy obtains its HTTPS certificate
when the proxy starts and needs DNS/ACME connectivity. IPv6 DNS must reach the
same host or be omitted.

```bash
sudo systemctl enable --now patternlab-web.service patternlab-proxy.service
curl --fail --silent http://127.0.0.1:8501/_stcore/health
sudo systemctl status patternlab-web.service patternlab-proxy.service
sudo systemctl is-enabled patternlab-worker.timer
sudo systemctl is-active patternlab-worker.service
```

The final two checks should report `disabled` and `inactive` respectively
(their nonzero exit codes are expected). Confirm 8501 listens only on
127.0.0.1, test HTTPS and OIDC where configured, and check that anonymous
visitors cannot perform administrator operations. The Mac worker stays active.

## Deliberate worker cutover — later, separately authorized

Do not perform this step merely to test the web UI. First validate the accepted
Linux frozen-source/artifact checks without training. Stop the Mac scheduler,
wait for its active cycle to finish, take a final consistent snapshot and
reconcile/restore it on Linux. Only one host may write or call the provider as
the authoritative worker. A marker is an operator attestation; this kit cannot
detect a worker still running on another computer.

Only after that cutover is explicitly authorized:

```bash
sudo install -o root -g root -m 0600 /dev/null /etc/patternlab/worker-cutover-approved
sudo -u patternlab -H env PYTHONDONTWRITEBYTECODE=1 PATTERNLAB_STORAGE_MODE=local /opt/patternlab/.venv/bin/python /opt/patternlab/deployment/linux/preflight.py --worker
sudo systemctl enable --now patternlab-worker.timer
```

The timer waits one minute before its first attempt, then fifteen minutes after
each completed cycle. It uses `OnUnitInactiveSec`, so a long-running oneshot
stays active and cannot overlap itself. There is no automatic restart or
timeout that would repeatedly interrupt training-sized work. The wrapper keeps
training disabled even after this cutover. Review any intended cadence override
against the existing approved scheduler setting before activating it.

Both systemd and preflight refuse a worker start without the root-owned 0600
marker. No script creates that marker or activates the timer automatically.
Monitor with `systemctl list-timers patternlab-worker.timer` and
`journalctl -u patternlab-worker.service`. Check disk use, worker outcomes and
backups regularly; local storage does not replicate itself.

To suspend Linux scheduling, disable the timer and remove the marker:

```bash
sudo systemctl disable --now patternlab-worker.timer
sudo rm -f /etc/patternlab/worker-cutover-approved
sudo systemctl status patternlab-worker.service
```

An already running cycle continues. Wait for it to finish and reconcile the
last state before starting the Mac worker again. Do not allow a rollback to
create two active writers. Keep the data directory and backups when removing
services.
