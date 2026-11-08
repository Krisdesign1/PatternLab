"""Verify a recovery point and exercise restored application readers offline."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main(argv=None):
    from recovery_rehearsal import rehearse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--point',type=Path,required=True,help='Point local existant ; aucune restauration en production.')
    parser.add_argument('--output',type=Path,required=True,help='Nouveau fichier JSON ; jamais écrasé.')
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error('La destination existe déjà ; choisir un nouveau fichier.')
    try:
        report = rehearse(args.point)
        with args.output.open('x',encoding='utf-8') as stream:
            json.dump(report,stream,ensure_ascii=False,indent=2,allow_nan=False)
    except (ValueError,OSError,sqlite3.Error,KeyError,TypeError):
        parser.exit(2,'Contrôle impossible : point de reprise ou destination invalide. Aucune donnée active modifiée.\n')
    print(json.dumps(dict(state=report['state'],output=str(args.output),
        stores_checked=len(report['domain_checks'])),ensure_ascii=False))
    return 0 if report['state']=='passed' else 1


if __name__=='__main__':
    raise SystemExit(main())
