"""Prepare frozen models from local API-Football data; never requests the API."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import model_campaign_worker as worker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='Lister les compétitions sans entraînement ni écriture.')
    parser.add_argument('--all-ready', action='store_true', help='Préparer séquentiellement les compétitions prêtes (50 maximum).')
    args = parser.parse_args()
    limit = 50 if args.all_ready and not args.dry_run else 1
    total = 0
    for _ in range(limit):
        report = worker.run_cycle(dry_run=args.dry_run)
        total += report.get('prepared', 0)
        summary = {key:report[key] for key in ('status','league','prepared','ready','frozen','competitions','issues') if key in report}
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        if report['status'] not in ('completed',):
            break
    return int(report['status']=='error')


if __name__ == '__main__':
    raise SystemExit(main())
