"""Prepare chronological partitions or an isolated candidate; never publish it."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    import causal_feature_artifacts as artifacts
    import causal_partitions as partitions

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    plan_command = commands.add_parser('plan', help='Préparer les périodes sans entraîner.')
    plan_command.add_argument('--bundle', type=Path, required=True, help='Paquet de variables du lot 03.')
    plan_command.add_argument('--boundaries', nargs=5, required=True, metavar='AAAA-MM-JJ',
                              help='Cinq dates UTC croissantes pour les quatre périodes.')
    plan_command.add_argument('--output', type=Path, required=True, help='Dossier nouveau obligatoire.')
    verify = commands.add_parser('verify', help='Vérifier un paquet de périodes, sans entraînement.')
    verify.add_argument('directory', type=Path)
    train = commands.add_parser('train', help='Entraîner un candidat isolé, sans activation.')
    train.add_argument('--partition', type=Path, required=True)
    train.add_argument('--output', type=Path, required=True, help='Dossier candidat nouveau obligatoire.')
    args = parser.parse_args()
    try:
        if args.action == 'verify':
            payload = partitions.load_partition_bundle(args.directory)
            result = dict(state='verified', partition_id=payload['partition']['id'],
                          ready=payload['partition']['ready'])
        else:
            if args.output.exists():
                parser.error('Le dossier de sortie doit être nouveau ; aucun paquet n’est écrasé.')
            if args.action == 'plan':
                names = ('train', 'tuning', 'calibration', 'confirmation')
                windows = {name: dict(start=args.boundaries[index] + 'T00:00:00Z',
                                      end=args.boundaries[index + 1] + 'T00:00:00Z')
                           for index, name in enumerate(names)}
                plan = partitions.make_plan(windows)
                bundle = artifacts.load_bundle(args.bundle)
                partition = partitions.prepare(bundle, plan)
                partitions.write_partition_bundle(args.output, bundle, plan, partition)
                payload = partitions.load_partition_bundle(args.output)
                result = dict(state='verified', partition_id=payload['partition']['id'],
                              ready=partition['ready'], blockers=partition['blockers'],
                              output=str(args.output))
            else:
                import causal_candidate as candidates
                payload = partitions.load_partition_bundle(args.partition)
                development = partitions.development_inputs(payload['partition'], payload['bundle'])
                model, report = candidates.fit_candidate(development)
                candidates.write_candidate(args.output, model, report)
                candidates.load_candidate(args.output)
                result = dict(state='candidate_saved', output=str(args.output),
                              active_model_changed=False, prospective_confirmation=False)
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.exit(2, f'Préparation impossible : {error}\n')
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()
