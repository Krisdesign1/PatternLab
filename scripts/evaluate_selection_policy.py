"""Comparer les règles de sélection sur une copie locale figée, sans publication de modèles."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import daily_selection_study as study


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,help='Dossier local API-Football contenant fixtures.json.')
    parser.add_argument('--today',help='Date de référence à Kinshasa, actuelle ou passée.')
    parser.add_argument('--end',help='Fin exclusive du test historique, au plus aujourd’hui.')
    args=parser.parse_args(argv)
    def progress(*args,**kwargs):
        print(json.dumps(dict(progress=list(args),**kwargs),ensure_ascii=False,allow_nan=False),flush=True)
    try:
        report=study.run_study(root=args.root,today=args.today,end=args.end,progress=progress)
    except (study.StudyError,ValueError,OSError) as error:
        parser.error(str(error))
    print(json.dumps({key:report[key] for key in ('run_id','created_at','input_fingerprint','report_path','inventory')},
                     ensure_ascii=False,allow_nan=False),flush=True)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
