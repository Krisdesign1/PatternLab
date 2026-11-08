#!/usr/bin/env python3
"""Build a dated, point-in-time scenario-selection policy from saved forecasts.

No API requests, model fitting, forecast rewriting or result reconciliation.
Activation remains separate from evaluation; lack of qualified contexts never
silently enables an arbitrary TopN replacement.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def atomic_json(target, value):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name+'.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary,target)


def main():
    import prediction_journal as journal
    import adaptive_scenario_calibration as calibration
    import adaptive_scenario_proposals as proposals
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal',type=Path,default=journal.DB_PATH)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--activate',action='store_true',help='Installer une calibration qualifiée et activer sa politique pour les nouvelles prévisions.')
    args=parser.parse_args()
    before=datetime.now(timezone.utc)
    entries=journal.list_entries(args.journal)
    artifact=calibration.fit(entries,as_of=before)
    proposals.validate_artifact(artifact)
    atomic_json(args.output,artifact)
    states=Counter(row['status'] for row in artifact['contexts'].values())
    if args.activate:
        if not states['qualified']:
            raise SystemExit('Aucun contexte qualifié : rapport conservé, politique non activée.')
        atomic_json(proposals.ARTIFACT_PATH,artifact)
        proposals.ENABLED_PATH.write_text(proposals.VERSION+'\n')
    print(json.dumps(dict(journal_entries=len(entries),created_at=artifact['created_at'],
        sha256=artifact['sha256'],contexts=dict(states),activated=args.activate,
        output=str(args.output)),ensure_ascii=False))


if __name__=='__main__':
    main()
