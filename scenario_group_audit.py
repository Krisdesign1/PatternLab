"""Read only the immutable forecast ledger, never results or current histories."""
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import scenario_group_contract as contract


VERSION = 'scenario-group-audit-v1'


def load_report(path=None):
    if path is None:
        from prediction_journal import DB_PATH
        path = DB_PATH
    path = Path(path).resolve()
    root = Path(__file__).resolve().parent
    implementation = {name: sha256((root/name).read_bytes()).hexdigest() for name in (
        'scenario_group_audit.py', 'scenario_group_contract.py', 'prospective_models.py',
        'group_probability_diagnostics.py', 'scenario_predictor.py')}
    rows, inputs = [], []
    if path.exists():
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=15)) as con:
            con.row_factory = sqlite3.Row
            con.execute('PRAGMA query_only=ON')
            con.execute('BEGIN')
            fields = {row['name'] for row in con.execute('PRAGMA table_info(predictions)')}
            if not {'id','created_at','payload','digest'} <= fields:
                raise ValueError('Le journal ne possède pas le schéma de prévisions attendu.')
            for raw in con.execute('SELECT id,created_at,payload,digest FROM predictions ORDER BY created_at,id'):
                if not isinstance(raw['payload'],str) or sha256(raw['payload'].encode()).hexdigest() != raw['digest']:
                    raise ValueError('Une prévision a été altérée ; le contrôle du journal est interrompu.')
                try:
                    value = json.loads(raw['payload'])
                    if (not isinstance(value,dict) or not isinstance(raw['id'],str) or not raw['id']
                            or value.get('id',raw['id']) != raw['id']
                            or value.get('created_at',raw['created_at']) != raw['created_at']):
                        raise ValueError()
                except (ValueError, TypeError):
                    raise ValueError('Les index du journal contredisent le contenu d’une prévision.') from None
                value.update(id=raw['id'],created_at=raw['created_at'])
                checked = contract.inspect(value)
                fixtures = value.get('fixtures')
                fixtures = fixtures if isinstance(fixtures,list) else []
                leagues = sorted({str(row.get('league','Inconnue')) for row in fixtures if isinstance(row,dict)})
                labels = [str(row.get('home','?'))+' — '+str(row.get('away','?'))
                          for row in fixtures if isinstance(row,dict)]
                rows.append(dict(id=raw['id'],size=checked['size'],created_at=raw['created_at'],
                    source=value.get('source'),leagues=leagues,match_labels=labels,
                    status=checked['status'],common_cutoff=checked['common_cutoff'],
                    decision_at=checked['decision_at'],issues=checked['issues'],methods=checked['methods'],
                    input_sha256=checked['input_sha256']))
                inputs.append(dict(id=raw['id'],created_at=raw['created_at'],payload_digest=raw['digest'],
                                   inspected_input_sha256=checked['input_sha256']))
            con.rollback()
    statuses = Counter(row['status'] for row in rows)
    issue_codes = Counter(issue['code'] for row in rows for issue in row['issues'])
    methods = Counter(issue.get('method','group') for row in rows for issue in row['issues'])
    projection = dict(version=VERSION,implementation=implementation,inputs=inputs)
    fingerprint = sha256(json.dumps(projection,ensure_ascii=False,sort_keys=True,
                                    separators=(',',':'),allow_nan=False).encode()).hexdigest()
    return dict(version=VERSION,generated_at=datetime.now(timezone.utc).isoformat(),
                implementation=implementation,source_sha256=fingerprint,
                counts=dict(total=len(rows),valid=statuses['valid'],unverified=statuses['unverified'],
                            invalid=statuses['invalid'],issue_codes=dict(issue_codes),issues_by_method=dict(methods)),
                rows=rows,interpretation='Recorded consistency only; no result reclassification, source visibility or COMMIT timing attestation.')
