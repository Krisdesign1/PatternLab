"""Explicit feature snapshots/cohorts using local import versions before T."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    from causal_feature_workspace import prepare_cohort,attach_outcomes
    from causal_feature_artifacts import load_bundle,write_bundle
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    prepare=sub.add_parser('prepare',help='Préparer sans entraîner ni modifier les modèles actifs.')
    prepare.add_argument('--database',type=Path,required=True)
    prepare.add_argument('--requests',type=Path,required=True,help='Liste JSON de {match_id,decision_at}.')
    prepare.add_argument('--outcome-at',help='Instant de lecture ultérieure des labels, facultatif.')
    prepare.add_argument('--output',type=Path,required=True,help='Dossier nouveau obligatoire.')
    verify=sub.add_parser('verify',help='Relire les variables figées sans consulter les données actuelles.')
    verify.add_argument('directory',type=Path)
    args=parser.parse_args()
    if args.action=='verify':
        bundle=load_bundle(args.directory)
        print(json.dumps(dict(state='verified',cohort_id=bundle['manifest']['id'],snapshots=len(bundle['snapshots']))))
        return
    if args.output.exists():
        parser.error('Le dossier de sortie doit être nouveau ; aucun paquet ne peut être écrasé.')
    requests=json.loads(args.requests.read_text(encoding='utf-8'))
    if not isinstance(requests,list):
        parser.error('Le fichier de demandes doit être une liste JSON.')
    cohort,snapshots=prepare_cohort(args.database,requests)
    outcomes=attach_outcomes(args.database,cohort,snapshots,outcome_at=args.outcome_at) if args.outcome_at else None
    write_bundle(args.output,cohort,snapshots,outcomes)
    load_bundle(args.output)
    print(json.dumps(dict(state='verified',requested=cohort['requested'],included=cohort['included'],excluded=cohort['excluded'],
                         labelled=outcomes['evaluated'] if outcomes else 0,output=str(args.output)),ensure_ascii=False))


if __name__=='__main__':
    main()
