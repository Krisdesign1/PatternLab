"""Audit retained fixture versions at a declared instant, without API calls."""
import argparse
from collections import Counter
from datetime import datetime,timezone
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import api_football as api
from local_recovery import fingerprint


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--as-of',required=True,help='Instant ISO avec fuseau, borne exclusive.')
    parser.add_argument('--ids',nargs='+',help='Identifiants de matchs précis, facultatifs.')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():
        parser.error('Le rapport doit utiliser une destination nouvelle.')
    source=api.active_store(args.database) or args.database
    def hashes():
        return {name:fingerprint(path) if path.exists() else None for name,path in (
            ('database',source),('wal',Path(str(source)+'-wal')))}
    before=hashes()
    rows,audit=api.load_matches(args.database,as_of=args.as_of,**({'ids':args.ids} if args.ids is not None else {}))
    report=dict(version='temporal-source-audit-v1',created_at=datetime.now(timezone.utc).isoformat(),
                source_files_sha256=before,database_unchanged=hashes()==before,
                historical_rows=len(rows),classes=dict(Counter(row['value'] for row in rows)),
                temporal_exclusion_counts=dict(Counter(row['reason'] for row in audit['temporal_exclusions'])),
                audit=audit)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as stream:
        json.dump(report,stream,ensure_ascii=False,indent=2,allow_nan=False)
    print(json.dumps({key:value for key,value in report.items() if key!='audit'},ensure_ascii=False))


if __name__=='__main__':
    main()
