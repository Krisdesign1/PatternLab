"""Prepare or evaluate an isolated frozen research snapshot."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))


def main(argv=None):
    parser=argparse.ArgumentParser(description='Comparaison isolée des profils de mi-temps.')
    parser.add_argument('action',choices=('prepare','status','evaluate'))
    parser.add_argument('--directory',type=Path,required=True,help='Nouveau dossier pour prepare ; instantané existant sinon.')
    parser.add_argument('--protocol',type=Path,help='Plan antérieur à respecter pour une nouvelle préparation ; vérifie ses empreintes avant tout calcul.')
    args=parser.parse_args(argv)
    try:
        if args.action=='status':
            result=json.loads((args.directory/'readiness.json').read_text())
        else:
            from halftime_research_experiment import prepare,evaluate,verify_code
            if args.protocol is not None:
                reference=json.loads(args.protocol.read_text())
                verify_code(reference)
            result=prepare(args.directory) if args.action=='prepare' else evaluate(args.directory)
            if args.action=='prepare' and args.protocol is not None:
                with (args.directory/'protocol_reference.json').open('x',encoding='utf-8') as handle:
                    json.dump(dict(path=str(args.protocol),sha256=sha256(args.protocol.read_bytes()).hexdigest(),
                        message='Research code and model parameters verified against earlier protocol before new snapshot extraction.'),handle,indent=2)
        print(json.dumps(result,ensure_ascii=False,allow_nan=False),flush=True)
        return 0
    except Exception as error:
        print(json.dumps(dict(status='error',kind=type(error).__name__,
            message='Préparation/évaluation interrompue ; vérifier la complétude, les identités, les empreintes et choisir un dossier neuf. Aucun modèle de l’application modifié.')),file=sys.stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())
