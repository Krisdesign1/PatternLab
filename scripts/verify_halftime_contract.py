"""Check the new target contract against a preserved local dataset, read only."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from halftime_domain import TARGET_VERSION, regulation_result
from local_recovery import fingerprint


def verify(database):
    database = Path(database).resolve()
    before = fingerprint(database)
    connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
    counts, failures = Counter(), []
    try:
        counts['stored'] = connection.execute('SELECT count(*) FROM fixtures').fetchone()[0]
        for mid, normalized, raw in connection.execute(
                'SELECT match_id,normalized_json,raw_json FROM fixtures WHERE normalized_json IS NOT NULL'):
            counts['checked'] += 1
            try:
                row, source = json.loads(normalized), json.loads(raw)
                result = regulation_result(source['score']['halftime'],source['score']['fulltime'],
                                           status=source['fixture']['status']['short'])
                projected = regulation_result(row['ht'],row['ft'])
                if projected != result or row['value'] != result.label:
                    raise ValueError('La projection historique et le contrat commun diffèrent.')
                counts[result.label] += 1
            except (KeyError,TypeError,ValueError) as error:
                failures.append(dict(match_id=mid,reason=str(error)))
    finally:
        connection.close()
    unchanged = fingerprint(database) == before
    return dict(target_version=TARGET_VERSION,checked_at=datetime.now(timezone.utc).isoformat(),
                database_sha256=before,counts=dict(counts),failures=failures,
                database_unchanged=unchanged,passed=unchanged and not failures and counts['checked']>0,
                scope='normalized historical projections only; no claim of prematch availability or predictive advantage')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Le rapport doit utiliser une destination nouvelle.')
    report = verify(args.database)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({key:value for key,value in report.items() if key!='failures'},ensure_ascii=False,indent=2))
    if not report['passed']:
        raise SystemExit(1)


if __name__=='__main__':
    main()
