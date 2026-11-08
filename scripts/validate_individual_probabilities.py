"""Local individual validation: explicit protocol creation, otherwise read-only."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Validation individuelle appariée des probabilités.')
    parser.add_argument('action', choices=('prepare', 'report'))
    parser.add_argument('--root', type=Path, default=ROOT/'data/api_football')
    parser.add_argument('--output', type=Path, help='Rapport JSON vers un nouveau fichier uniquement.')
    args = parser.parse_args(argv)
    import individual_probability_validation as validation
    try:
        if args.action == 'prepare':
            plan = validation.prepare(args.root)
            result = dict(status='protocol_prepared', protocol_id=plan['id'], start_at=plan['start_at'],
                          enriched=plan['enriched']['status'], path=str(validation.protocol_path(args.root)))
        else:
            import high_probability_archive as archive
            now = datetime.now(timezone.utc)
            entries = archive.list_entries(args.root)
            plan = validation.load_protocol(args.root)
            result = dict(as_of=now.isoformat(), protocol=plan,
                compatibility=validation.compatibility(plan) if plan else None,
                descriptive=validation.build(entries, now=now),
                prospective=validation.build(entries, now=now, protocol=plan) if plan else None)
        encoded = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
        if args.output:
            with args.output.open('x', encoding='utf-8') as handle:
                handle.write(encoded+'\n')
            print(json.dumps(dict(status='saved', path=str(args.output)), ensure_ascii=False))
        else:
            print(encoded)
        return 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(json.dumps(dict(status='error', kind=type(error).__name__, message=str(error)), ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
