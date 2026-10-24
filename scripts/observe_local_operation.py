"""Inspect local cycles, forecast timing and evidence without running a cycle.

The default is JSON on stdout. --output explicitly creates a new report file;
it never replaces an existing report. No configuration, forecast or API data is
changed. Compare separately dated snapshots during several days of real use.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import local_operation_report as observations


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,default=Path(__file__).resolve().parents[1]/'data'/'api_football')
    parser.add_argument('--days',type=int,default=7)
    parser.add_argument('--timezone',default=observations.DEFAULT_TIMEZONE)
    parser.add_argument('--skip-evidence',action='store_true',help='Omettre le comparatif prospectif pour une lecture rapide.')
    parser.add_argument('--output',type=Path,help='Créer ce nouveau fichier JSON, sans écrasement.')
    args = parser.parse_args(argv)
    try:
        report = observations.collect(args.data_dir,days=args.days,timezone_name=args.timezone,
                                      include_evidence=not args.skip_evidence)
        content = json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n'
        if args.output is None:
            print(content,end='')
        else:
            with args.output.open('x',encoding='utf-8') as stream:
                stream.write(content)
            print('Rapport local créé. Aucune collecte ni modification des données effectuée.')
        return 0
    except (ValueError,OSError):
        print('Observation interrompue : paramètres invalides ou destination indisponible. Aucun secret affiché.',file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
