"""Enroll or inspect a frozen candidate; provider captures belong to the worker."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    import causal_confirmation as confirmation
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    register = commands.add_parser('register', help='Enregistrer le candidat et ses règles avant les futures décisions.')
    register.add_argument('--candidate', type=Path, required=True)
    followup = commands.add_parser('register-followup', help='Inscrire un nouveau suivi de huit semaines UTC, sans modifier le test original.')
    followup.add_argument('--candidate', type=Path, required=True)
    followup.add_argument('--start', required=True, help='Lundi inclus, ISO UTC à minuit.')
    followup.add_argument('--end', required=True, help='Lundi exclu, huit semaines après le début.')
    commands.add_parser('status', help='Lire le suivi local, sans capture ni appel API.')
    diagnostics = commands.add_parser('diagnostics', help='Mesurer les probabilités sur les mêmes matchs, sans appel API.')
    diagnostics.add_argument('--protocol', help='Identifiant du protocole ; sans option, bilans séparés pour chaque protocole.')
    weekly = commands.add_parser('weekly', help='Lire la couverture hebdomadaire et la comparaison préenregistrée, sans appel API.')
    weekly.add_argument('--protocol', help='Identifiant du protocole ; sans option, bilans séparés.')
    commands.add_parser('reconcile', help='Rapprocher les résultats déjà collectés, sans appel API ni prévision nouvelle.')
    args = parser.parse_args()
    try:
        if args.action == 'register':
            result = confirmation.register(args.candidate)
        elif args.action == 'register-followup':
            result = confirmation.register_followup(args.candidate, start=args.start, end=args.end)
        elif args.action == 'status':
            result = confirmation.report()
        elif args.action == 'reconcile':
            result = confirmation.reconcile()
        else:
            if args.action == 'weekly':
                from causal_confirmation_followup import build
            else:
                from causal_confirmation_diagnostics import build
            report = confirmation.report()
            protocols = [row for row in report['protocols'] if not args.protocol or row['id'] == args.protocol]
            if args.protocol and not protocols:
                raise ValueError('Protocole demandé introuvable.')
            result = dict(generated_at=report['generated_at'], protocols=[build(row) for row in protocols])
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as error:
        parser.exit(2, f'Suivi impossible : {error}\n')
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
