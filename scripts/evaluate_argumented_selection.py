"""Rejeu historique local et figé de la sélection argumentée, sans réseau."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main(argv=None):
    import argumented_selection_study as study
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,default=study.DEFAULT_DIRECTORY)
    parser.add_argument('--root',type=Path)
    parser.add_argument('--start',default='2024-01-01')
    parser.add_argument('--end',default='2026-10-30')
    parser.add_argument('--prepare-only',action='store_true')
    parser.add_argument('--report',action='store_true')
    args=parser.parse_args(argv)
    def progress(value):
        if value.get('stage')!='replay' or value['completed']%50==0 or value['completed']==value['total']:
            print(json.dumps(value,ensure_ascii=False),flush=True)
    try:
        if args.report:report=study.load_report(args.directory)
        elif args.prepare_only:report=study.prepare(args.directory,root=args.root,start=args.start,end=args.end)
        else:report=study.run(args.directory,root=args.root,start=args.start,end=args.end,progress=progress)
        if args.report:print(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False))
        else:print(json.dumps(dict(status='completed' if not args.prepare_only else 'prepared',
            directory=str(args.directory),report_path=str(args.directory/'report.json')),ensure_ascii=False))
    except (ValueError,OSError,KeyError,TypeError) as error:
        parser.error(str(error))
    return 0


if __name__=='__main__':raise SystemExit(main())
