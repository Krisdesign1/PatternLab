#!/usr/bin/env python3
"""One Linux automation cycle using the existing service, without training.

The worker's in-cycle training and the service's post-cycle model preparation
are disabled independently. No existing application/configuration file changes.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def run_once(root=PROJECT_ROOT, *, service=None):
    if service is None:
        # Direct script invocation initially puts deployment/linux on sys.path.
        sys.path.insert(0, str(root))
        from scripts import automation_service as service
    config = dict(service._configuration(root))
    config.update(models_enabled=False, model_jobs_per_cycle=0)
    return service.run_once(root=root, config=config, skip_models=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", required=True)
    parser.parse_args(argv)
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from deployment.linux.preflight import check
        check(PROJECT_ROOT, worker=True)
        report = run_once()
        if not isinstance(report, dict) or report.get("status") not in {
            "completed", "paused", "error", "busy", "disabled", "dry_run"
        }:
            raise ValueError()
    except Exception:
        # Keep provider keys, configuration and arbitrary exceptions out of logs.
        print('{"status":"error","error":"Linux cycle refused; inspect preflight and service status."}')
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 1 if report["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
