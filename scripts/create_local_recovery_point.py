"""Create/verify a private local recovery point without touching live data."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from local_recovery import create_recovery_point, verify_recovery_point


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify', type=Path, help='Vérifier et restaurer en espace isolé un point existant.')
    parser.add_argument('--destination', type=Path, help='Nouveau dossier local (jamais réutilisé).')
    args = parser.parse_args()
    if args.verify and args.destination:
        parser.error('Choisir création ou vérification.')
    if args.verify:
        report = verify_recovery_point(args.verify)
    else:
        destination = args.destination or ROOT / '.local_backups' / (
            'patternlab2-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        report = create_recovery_point(ROOT, destination)
        report['recovery_point'] = str(destination)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
