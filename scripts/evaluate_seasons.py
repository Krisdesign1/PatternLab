"""Run the predefined seasonal protocol on local API-Football history."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api_football
import seasonal_validation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--league', nargs='+', default=['39', '61'])
    args = parser.parse_args()
    rows, audit = api_football.load_matches()
    if audit['issues']:
        raise ValueError('Résoudre les incohérences de l’historique avant cette évaluation.')
    for league in args.league:
        report = seasonal_validation.run(
            rows, league, progress=lambda value, message: print(f'{league} {value:.0%} {message}', flush=True))
        seasonal_validation.save_report(report)
        print(json.dumps({'league': league, 'aggregate_complete': report['aggregate_complete'],
                          'aggregate_partial': report['aggregate_partial']}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
