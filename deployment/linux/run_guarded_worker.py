#!/usr/bin/env python3
"""Versioned Linux entry point with strict season intake and no model training.

Original worker, provider import policy and first Linux kit stay unchanged.
Only the injected provider boundary prevalidates full-season responses. It
preserves the original request reservations, quotas, lock, temporal checks,
six-hour failed-job deferral, and interleaved priority work.
"""
import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def run_once(root=PROJECT_ROOT, *, service=None, worker=None, provider=None, max_duration_seconds=None):
    sys.path.insert(0, str(root))
    import api_football as api
    import automation_worker
    import cloud_storage
    from season_intake import SeasonIntakeClient
    if service is None:
        from scripts import automation_service as service
    worker = automation_worker if worker is None else worker
    config = dict(service._configuration(root))
    config.update(models_enabled=False, model_jobs_per_cycle=0)
    client = (SeasonIntakeClient(api.ApiFootballClient(api.load_key()) if provider is None else provider,
                                Path(root)/'data/api_football/fixtures.json')
              if config['enabled'] else None)

    class Boundary:
        @staticmethod
        def run_cycle(**arguments):
            return worker.run_cycle(**arguments, client=client)

    with cloud_storage.transaction():
        result = service._run_once(Boundary, root, dry_run=False, config=config,
                                   skip_models=True, max_duration_seconds=max_duration_seconds)
        result['season_intake'] = dict(version='season-intake-v1', deferred=client.deferred if client else [])
        cloud_storage.checkpoint()
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true', required=True)
    parser.add_argument('--max-duration-seconds',type=float,default=None,
                        help='Optional qualification window; normal timer remains unbounded.')
    options=parser.parse_args(argv)
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from deployment.linux.preflight import check
        check(PROJECT_ROOT, worker=True)
        result = run_once(max_duration_seconds=options.max_duration_seconds)
    except Exception:
        print('{"status":"error","error":"Guarded Linux cycle refused; inspect preflight and service status."}')
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return int(result['status'] == 'error')


if __name__ == '__main__':
    raise SystemExit(main())
