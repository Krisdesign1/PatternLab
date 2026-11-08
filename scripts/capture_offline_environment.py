"""Snapshot installed package bytes for a same-host offline restoration drill."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))


def main(argv=None):
    import offline_environment as environment
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination',type=Path,required=True)
    args=parser.parse_args(argv)
    given=args.destination
    if given.exists() or given.is_symlink():parser.error('La destination existe déjà.')
    dest=given.resolve()
    if (ROOT/'.local_backups').is_symlink() or not dest.is_relative_to(ROOT/'.local_backups'):
        parser.error('Choisir un nouveau dossier sous .local_backups.')
    try:
        result=environment.capture(dest)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0
    except (ValueError,OSError,KeyError,TypeError):
        parser.exit(2,'Capture interrompue ; environnement actif inchangé.\n')


if __name__=='__main__':raise SystemExit(main())
