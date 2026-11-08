"""Read candidate follow-up periods; explicitly archive an observation with --capture.

The default reads only. No provider request, model training, result reconciliation
or study registration occurs. --output creates a new file without replacement.
"""
import argparse
import json
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import candidate_followup_report as observations


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,default=Path(__file__).resolve().parents[1]/'data'/'api_football')
    parser.add_argument('--study',help='Identifiant du suivi, sinon tous les suivis.')
    parser.add_argument('--capture',action='store_true',help='Archiver un relevé daté des suivis existants ; aucune prévision créée.')
    parser.add_argument('--output',type=Path,help='Créer ce fichier JSON sans écraser un fichier existant.')
    args=parser.parse_args(argv)
    try:
        if args.capture:
            observations.capture(args.data_dir)
        report=observations.collect(args.data_dir,study_id=args.study)
        content=json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n'
        if args.output:
            with args.output.open('x',encoding='utf-8') as handle: handle.write(content)
            print('Relevé local créé ; prévisions et règles conservées.')
        else: print(content,end='')
        return 0
    except (ValueError,OSError,sqlite3.Error,KeyError,TypeError):
        print('Observation indisponible ; aucun compteur nul ni résultat reconstruit.',file=sys.stderr)
        return 1


if __name__=='__main__': raise SystemExit(main())
