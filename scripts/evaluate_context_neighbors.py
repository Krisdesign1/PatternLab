"""Run explicit, local seasonal evaluation of contextual recurrence."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api_football
import contextual_validation
import model_campaign as campaign


def selected_issues(issues, fixtures, leagues):
    """Attribute score issues to raw league IDs before blocking a scoped audit."""
    requested = {str(league) for league in leagues}
    selected_ids = set()
    for raw in fixtures:
        try:
            if str(raw['league']['id']) in requested:
                selected_ids.add(str(raw['fixture']['id']))
        except (KeyError, TypeError):
            continue
    return [issue for issue in issues if str(issue.get('match_id')) in selected_ids]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('leagues', nargs='*', default=['39', '61'])
    parser.add_argument('--sizes', nargs='+', type=int, default=[2, 4, 6, 8])
    args = parser.parse_args()
    try:
        leagues = list(dict.fromkeys(api_football._numeric_id(league, 'de compétition') for league in args.leagues))
        for league in leagues:
            if campaign.load_manifest(league) is not None:
                raise campaign.CampaignError(f'La campagne de la compétition {league} est déjà verrouillée. '
                    'Ses réglages sont conservés ; consultez prepare_model_campaign.py --dry-run.')
    except (ValueError, OSError) as error:
        parser.error(str(error))
    rows, audit = api_football.load_matches(api_football.STORE_PATH)
    issues = selected_issues(audit['issues'], api_football.load_fixtures(api_football.STORE_PATH), leagues)
    if issues:
        parser.error(f"Résolvez les {len(issues)} anomalies HT/FT des compétitions sélectionnées avant cette évaluation.")
    ignored = len(audit['issues'])-len(issues)
    if ignored:
        print(f'{ignored} anomalies d’autres compétitions exclues du périmètre de cette évaluation.', flush=True)
    for league in leagues:
        def progress(fraction, message):
            print(f'{league} · {fraction:.0%} · {message}', flush=True)
        try:
            if campaign.load_manifest(league) is not None:
                raise campaign.CampaignError(f'La campagne de la compétition {league} vient d’être verrouillée.')
            report = contextual_validation.run(rows, league, sizes=tuple(args.sizes), progress=progress)
            path = contextual_validation.save_report(report)
        except (ValueError, OSError) as error:
            parser.error(str(error))
        print(json.dumps({'league': league, 'path': str(path), 'exploratory': True,
                          'aggregate_complete': report['aggregate_complete'],
                          'aggregate_partial': report['aggregate_partial'],
                          'deployment': report['deployment']}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
