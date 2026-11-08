"""Inspect or explicitly train one isolated local API-Football candidate."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api_football as api
import model_reassessment as reassessment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--league', required=True, help='Identifiant API-Football de la compétition.')
    parser.add_argument('--sizes', nargs='+', type=int, default=list(reassessment.DEFAULT_SIZES))
    parser.add_argument('--run', action='store_true', help='Entraîner et conserver un candidat séparé. Sinon, lecture seule.')
    parser.add_argument('--track', action='store_true', help='Avec --run, activer aussi le suivi prospectif par le planificateur local.')
    args = parser.parse_args()
    if args.track and not args.run:
        parser.error('--track exige --run ; l’inspection reste en lecture seule.')
    try:
        rows, _ = api.load_matches(api.STORE_PATH)
        if args.run:
            result = reassessment.run(rows, args.league, sizes=args.sizes,
                progress=lambda fraction, message: print(f'{fraction:.0%} · {message}', flush=True))
            if args.track:
                import candidate_tracking
                study=candidate_tracking.register(args.league,result['run_id'])
                print(json.dumps({'prospective_study':study['id'],'registered_at':study['registered_at']}),flush=True)
            result = {name: result[name] for name in ('status', 'league', 'run_id', 'candidate_generation',
                       'incumbent_generation', 'history_matches', 'promoted')}
        else:
            result = reassessment.plan(rows, args.league, sizes=args.sizes)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return 0
    except (ValueError, OSError, api.ApiFootballError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
