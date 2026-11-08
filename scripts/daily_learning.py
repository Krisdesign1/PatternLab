"""Activate, prepare one candidate, or inspect the local daily-learning loop."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    actions=parser.add_mutually_exclusive_group()
    actions.add_argument('--enable',action='store_true')
    actions.add_argument('--disable',action='store_true')
    actions.add_argument('--train',action='store_true',help='Préparer un candidat local, sans appel API ni remplacement.')
    actions.add_argument('--report',action='store_true',help='Lire les prévisions réellement conservées et leurs résultats.')
    parser.add_argument('--league',help='Compétition API-Football pour la préparation explicite.')
    parser.add_argument('--dry-run',action='store_true')
    arguments=parser.parse_args(argv)
    import daily_learning_worker as worker
    try:
        if arguments.enable or arguments.disable:
            if arguments.dry_run:parser.error('--dry-run ne s’applique pas à l’activation.')
            worker.enable(enabled=arguments.enable)
            report=dict(enabled=worker.is_enabled())
        elif arguments.train or arguments.dry_run:
            report=worker.run_training(league=arguments.league,dry_run=arguments.dry_run)
        elif arguments.report:
            import daily_learning_tracking as tracking
            report=tracking.summary()
        else:report=dict(enabled=worker.is_enabled(),last_training=worker.status())
        print(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False))
        return 1 if report.get('status')=='error' else 0
    except (ValueError,OSError,RuntimeError):
        print(json.dumps(dict(status='error',reason='Boucle quotidienne indisponible ; aucune version servie modifiée.'),ensure_ascii=False))
        return 1


if __name__=='__main__':raise SystemExit(main())
