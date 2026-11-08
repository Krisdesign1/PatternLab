"""Inspect local import conflicts; an explicit reviewed decision is optional.

No API request or model training. Default action is read-only inspection.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_football as api
import cloud_storage
from fixture_import_policy import QUALITY_KEY


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path', type=Path, default=api.STORE_PATH)
    parser.add_argument('--match', help='Inspect this API-Football ID and its preserved observations.')
    parser.add_argument('--choose', choices=('received', 'prior'), help='Explicitly accept the latest received or prior version.')
    parser.add_argument('--expected', help='Exact latest observation ID, required for a reviewed decision.')
    parser.add_argument('--reason', help='Reason for the reviewed decision, recorded in the audit.')
    args = parser.parse_args(argv)
    if cloud_storage.configured():
        parser.error('Cet outil est réservé au stockage local autonome.')
    if args.choose and not all((args.match, args.expected, args.reason)):
        parser.error('--choose exige --match, --expected et --reason ; sinon la consultation reste en lecture seule.')
    try:
        if args.choose:
            event = api.review_import(args.match, path=args.path, choice=args.choose,
                expected_observation=args.expected, reason=args.reason)
            print(json.dumps(dict(reviewed_observation=event['id'], decision=event['review']), ensure_ascii=False))
        elif args.match:
            print(json.dumps(dict(current=api.load_fixtures(args.path, ids=[args.match]),
                observations=api.load_import_observations(args.path, ids=[args.match])), ensure_ascii=False, indent=2))
        else:
            print(json.dumps([dict(match_id=str(raw['fixture']['id']),
                home=raw.get('teams', {}).get('home', {}).get('name'),
                away=raw.get('teams', {}).get('away', {}).get('name'), quality=raw[QUALITY_KEY])
                for raw in api.import_quarantines(args.path)], ensure_ascii=False, indent=2))
    except (api.ApiFootballError, ValueError, OSError):
        print('La consultation ou la revue a échoué. Aucune décision non validée n’est appliquée.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
