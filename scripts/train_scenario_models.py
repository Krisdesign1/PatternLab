"""Train local league models; never requests provider data or credentials."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import catboost_scenarios as ml
import model_campaign as campaign
import workspace_sources as sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', choices=sources.SOURCES, default='API-Football')
    parser.add_argument('--league', nargs='+', default=['39', '61'])
    options = parser.parse_args()
    try:
        leagues = list(dict.fromkeys(campaign._league(league) for league in options.league))
        for league in leagues:
            if campaign.load_manifest(league) is not None:
                raise campaign.CampaignError(f'La campagne de la compétition {league} est déjà verrouillée. '
                    'Ses paramètres sont conservés ; consultez prepare_model_campaign.py --dry-run.')
    except (campaign.CampaignError, OSError, ValueError) as error:
        parser.error(str(error))
    rows, audit = sources.load_matches(options.source)
    for league in leagues:
        try:
            if campaign.load_manifest(league) is not None:
                raise campaign.CampaignError(f'La campagne de la compétition {league} vient d’être verrouillée.')
            print(f'Entraînement {options.source} / {league}…', flush=True)
            report = ml.train(rows, league, options.source)
        except (ValueError, OSError) as error:
            parser.error(str(error))
        print(json.dumps({'source': report['source'], 'league': report['league'],
                          'split': report['split'], 'selection': report['selection'],
                          'test': report['test'], 'deployment': report['deployment']},
                         ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
