"""Audit stored context without collecting data, changing a DB or training."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from context_quality_audit import run_audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--label', required=True, help='Explicit snapshot label, e.g. local Mac or AWS.')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    database = args.database.resolve(strict=True)
    if (output.exists() or output == database or output.is_relative_to(ROOT/'data/api_football')):
        parser.error('Use a NEW audit receipt outside the active data directory.')
    source_paths = [ROOT/name for name in (
        'context_event_quality.py', 'prematch_context_quality.py', 'context_quality_audit.py',
        'scripts/audit_context_quality.py')]
    preservation = ROOT/'audit/PATTERNLAB2_PRESERVATION_LOT14_2026-11-02.json'
    if preservation.exists():
        frozen = json.loads(preservation.read_text())['frozen_source_sha256']
        source_paths += [ROOT/name for name in dict.fromkeys([
            *frozen, 'active_candidate_integrity.py', 'causal_partitions.py', 'catboost_scenarios.py'])]
    def hashes():
        return {str(p.relative_to(ROOT)): sha256(p.read_bytes()).hexdigest() for p in source_paths}
    before = hashes()
    report = run_audit(database, source_label=args.label)
    if before != hashes():
        raise ValueError('Audit implementation or protected sources changed during the run.')
    report['source_code_hashes_before_after_equal'] = before
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({key: report[key] for key in (
        'source_label', 'fixture_count', 'availability', 'events', 'lineups', 'revisions',
        'elapsed_seconds')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
