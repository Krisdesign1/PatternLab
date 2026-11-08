"""Freeze, resume or inspect a local daily causal target population."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    import causal_population as population
    from api_football import ApiFootballError
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    start = commands.add_parser('start', help='Figer une copie locale et la règle, sans entraîner.')
    start.add_argument('--database', type=Path, required=True)
    start.add_argument('--start', required=True, help='Premier jour UTC inclus.')
    start.add_argument('--end', required=True, help='Dernier jour UTC exclu.')
    start.add_argument('--hour-utc', type=int, default=8)
    start.add_argument('--outcome-at', help='Heure ultérieure de lecture des labels ; facultative.')
    start.add_argument('--output', type=Path, required=True, help='Nouveau dossier de recherche.')
    run = commands.add_parser('run', help='Continuer par tranches, sans plafond total.')
    run.add_argument('directory', type=Path)
    run.add_argument('--batch-size', type=int, default=50)
    run.add_argument('--until-complete', action='store_true')
    for action in ('status', 'verify'):
        commands.add_parser(action).add_argument('directory', type=Path)
    args = parser.parse_args()
    try:
        if args.action == 'start':
            policy = population.make_policy(args.start, args.end, hour_utc=args.hour_utc, outcome_at=args.outcome_at)
            job = population.start(args.database, args.output, policy)
            print(json.dumps(dict(state='source_frozen', job_id=job['id'], output=str(args.output))))
        elif args.action == 'run':
            while True:
                result = population.run(args.directory, batch_size=args.batch_size)
                print(json.dumps(result, ensure_ascii=False), flush=True)
                prepared = result.get('state') == 'prepared' or result.get('progress', {}).get('state') == 'prepared'
                if prepared and args.until_complete:
                    verified = population.verify(args.directory)
                    print(json.dumps(dict(state='verified', summary=verified['summary']), ensure_ascii=False), flush=True)
                if not args.until_complete or prepared:
                    break
        elif args.action == 'verify':
            result = population.verify(args.directory)
            print(json.dumps(dict(state='verified', summary=result['summary']), ensure_ascii=False))
        else:
            print(json.dumps(population.status(args.directory), ensure_ascii=False))
    except (ValueError, OSError, KeyError, TypeError, ApiFootballError) as error:
        parser.exit(2, f'Préparation impossible : {error}\n')


if __name__ == '__main__':
    main()
