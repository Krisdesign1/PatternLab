"""Restore a verified point and installed-package bundle into a private new folder."""
import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))


def main(argv=None):
    import service_restoration
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('point','environment','destination','output'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--expected-manifest',required=True)
    parser.add_argument('--expected-environment',required=True)
    args=parser.parse_args(argv)
    if args.output.exists():parser.error('La destination du rapport existe déjà.')
    try:
        r=service_restoration.rehearse(args.point,args.environment,args.destination,
            expected_manifest_sha256=args.expected_manifest,expected_environment_sha256=args.expected_environment)
        with args.output.open('x',encoding='utf-8') as f:json.dump(r,f,ensure_ascii=False,indent=2,allow_nan=False)
        print(json.dumps(dict(state=r['state'],output=str(args.output)),ensure_ascii=False))
        return 0
    except (ValueError,OSError,sqlite3.Error,subprocess.SubprocessError,KeyError,TypeError):
        parser.exit(2,'Exercice interrompu ; les données actives restent intactes. Aucun détail privé affiché.\n')


if __name__=='__main__':raise SystemExit(main())
