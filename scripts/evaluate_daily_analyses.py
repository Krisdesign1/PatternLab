"""Rejouer localement la sélection quotidienne, sans API ni entraînement."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import daily_evaluation_store as store


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start',default='2022-01-01',help='Premier jour inclus, au format AAAA-MM-JJ (défaut : 2022-01-01).')
    parser.add_argument('--end',help='Dernier jour exclu ; au plus aujourd’hui à Kinshasa (défaut : aujourd’hui).')
    parser.add_argument('--today',help='Date de référence passée ou actuelle pour reproduire un audit, jamais une date future.')
    parser.add_argument('--root',type=Path,help='Répertoire local API-Football contenant fixtures.json (facultatif).')
    args=parser.parse_args(argv)
    def progress(*values,**kwargs):
        print(json.dumps(dict(progress=list(values),**kwargs),ensure_ascii=False,allow_nan=False),flush=True)
    try:
        report=store.run_audit(root=args.root,today=args.today,start=args.start,end=args.end,progress=progress)
    except (store.EvaluationStoreError,ValueError,OSError) as error:
        parser.error(str(error))
    print(json.dumps({key:report[key] for key in ('run_id','created_at','input_fingerprint','source_signature','report_path','counts') if key in report},
                     ensure_ascii=False,allow_nan=False),flush=True)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
