"""Bounded read-only checks of archived delivery evidence, never live acceptance.

Archived source copies qualify the recorded checks. They are deliberately not
compared with today's README or every installed source file. A valid chain of
local hashes is not a signature and does not rerun tests or recheck the backup.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re


VERSION = 'acceptance-evidence-v1'
DATE = '2026-11-02'
MAX_FILE_BYTES = 16 * 1024 * 1024
EXPECTED = {
    'lot11':'dual_clock_coordinator_corrected_new_future_study_enrolled',
    'lot12':'recovery_hardening_and_isolated_application_rehearsal_delivered',
}
DOMAINS = {'fixtures':True,'forecast_journal':True,'causal_confirmation':True,
           'probability_archive':False,'scenario_intents':False,'manual_bets':False}


class EvidenceError(ValueError):
    pass


def _require(condition):
    if not condition:
        raise EvidenceError('historical_evidence_invalid')


def _sha(value):
    _require(isinstance(value,str) and re.fullmatch('[a-f0-9]{64}',value) is not None)
    return value


def _count(value, *, positive=False):
    _require(type(value) is int and value >= (1 if positive else 0))
    return value


def _instant(value):
    _require(isinstance(value,str))
    stamp = datetime.fromisoformat(value.replace('Z','+00:00'))
    _require(stamp.tzinfo is not None and stamp.utcoffset() is not None)
    return stamp.astimezone(timezone.utc)


def _relative(value):
    _require(isinstance(value,str) and bool(value) and '\\' not in value and '\x00' not in value)
    path = Path(value)
    _require(not path.is_absolute() and '..' not in path.parts
             and path != Path('.') and path.as_posix() == value)
    return path


def _path(base, value, *, directory=False):
    relative = _relative(value)
    result = base/relative
    current = base
    _require(not base.is_symlink())
    for part in relative.parts:
        current = current/part
        _require(not current.is_symlink())
    _require(result.resolve().is_relative_to(base.resolve()))
    _require(result.is_dir() if directory else result.is_file())
    return result


def _unique_object(pairs):
    result = {}
    for key,value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _json(data):
    value = json.loads(data, object_pairs_hook=_unique_object,
                       parse_constant=lambda _value: (_ for _ in ()).throw(EvidenceError()))
    _require(isinstance(value,dict))
    return value


def _read(path, observed, role):
    _require(path.stat().st_size <= MAX_FILE_BYTES)
    data = path.read_bytes()
    _require(len(data) <= MAX_FILE_BYTES)
    value = sha256(data).hexdigest()
    observed.append([role,value])
    return data,value


def _tests(receipt, lot, audit, archive, evidence, observed):
    value = receipt['tests']
    _require(isinstance(value,dict) and value.get('version') == f'{lot}-tests-v1')
    count = _count(value['passed'],positive=True)
    _require(_count(value['failures']) == 0 and _count(value['ignored']) == 0)
    _require(value.get('py_compile_pass') is True and value.get('diff_check_pass') is True)
    _require(_instant(value['checked_at']) <= _instant(receipt['received_at']))
    name = f'PATTERNLAB2_TESTS_{lot.upper()}_{DATE}.json'
    _require(name in evidence and evidence[name] == value)
    raw = _relative(value['log_path'])
    _require(raw.parts[0]=='audit')
    log = _path(audit,Path(*raw.parts[1:]).as_posix())
    _require(log.resolve().is_relative_to(archive.resolve()))
    data,digest = _read(log,observed,f'{lot}:tests_log')
    _require(digest == _sha(value['log_sha256']))
    text = data.decode('utf-8')
    runs = re.findall(r'^Ran (\d+) tests? in [0-9.]+s$',text,re.MULTILINE)
    _require(runs == [str(count)] and text.rstrip().endswith('\nOK'))
    _require(re.search(r'^(FAILED|FAIL:|ERROR:)',text,re.MULTILINE) is None)
    return count


def _lot11(receipt, evidence):
    enrollment = evidence[f'PATTERNLAB2_INSCRIPTION_LOT11_{DATE}.json']
    service = evidence[f'PATTERNLAB2_SERVICE_LOT11_{DATE}.json']
    protocol,ack = enrollment['new_protocol'],enrollment['registration_ack']
    pid = _sha(receipt['new_protocol_id'])
    _require(protocol['id']==pid and ack['protocol_id']==pid)
    _require(enrollment.get('old_payload_unchanged') is True and enrollment.get('candidate_unchanged') is True)
    _require(enrollment['old_protocol_id']==_sha(receipt['old_protocol_id']))
    _require(receipt.get('old_window_changed') is False and receipt.get('old_missed_window_backfilled') is False)
    _require(receipt.get('old_protocol_implementation_compatible') is False
             and receipt.get('new_protocol_implementation_compatible') is True)
    _require(receipt.get('scheduler_fix')=='civil-or-monotonic-refresh-v2')
    _require(receipt['candidate_id']==_sha(protocol['candidate_id'])
             and receipt['candidate_files_unchanged']==protocol['candidate_files'])
    _require(set(protocol['candidate_files'])=={'candidate.cbm','report.json'})
    for value in protocol['candidate_files'].values():
        _sha(value)
    _require(receipt['sources_sha256']['automation_worker.py']==protocol['implementation']['automation_worker.py'])
    _require(ack.get('kind')=='registration_ack' and ack.get('time_quality')=='local_registration_ack')
    _require(receipt['new_registered_at']==protocol['registered_at']
             and receipt['new_registration_ack_at']==ack['observed_at_utc'])
    period = receipt['new_period']
    _require(period['start']==protocol['confirmation_start'] and period['end_exclusive']==protocol['confirmation_end']
             and period['result_cutoff']==protocol['followup']['result_cutoff_utc'])
    _require(_instant(protocol['registered_at']) <= _instant(ack['observed_at_utc'])
             < _instant(period['start']) < _instant(period['end_exclusive']) < _instant(period['result_cutoff']))
    _require(service.get('real_calendar_fetch_observed') is True
             and receipt['activation'].get('real_calendar_fetch_observed') is True)
    start = _instant(receipt['activation']['new_cycle_started_at'])
    _require(service['new_cycle']['id']==receipt['activation']['new_cycle_id']
             and _instant(service['new_cycle']['started_at'])==start)
    _require(any(isinstance(row,list) and len(row)==3 and row[2]=='calendar'
                 and start <= _instant(row[0]) <= _instant(receipt['received_at'])
                 for row in service['completed_operations']))
    return dict(real_calendar_fetch_recorded=True,old_protocol_retained=True,new_protocol_id=pid,
                candidate_unchanged_recorded=True,
                period={key:_instant(period[key]).isoformat() for key in ('start','end_exclusive','result_cutoff')})


def _lot12(receipt, evidence):
    backup = evidence[f'PATTERNLAB2_SAUVEGARDE_LOT12_{DATE}.json']
    rehearsal = evidence[f'PATTERNLAB2_RESTAURATION_APPLICATION_LOT12_{DATE}.json']
    verification = backup['verification']
    _require(verification.get('version')=='local-recovery-v1' and verification.get('state')=='verified')
    _require(verification.get('known_private_paths_excluded') is True
             and verification.get('embedded_secret_content_audited') is False)
    files = _count(receipt['backup_files'],positive=True)
    databases = _count(receipt['databases_restored'],positive=True)
    restored = verification['databases_restored']
    _require(isinstance(restored,list) and all(isinstance(v,str) for v in restored)
             and len(restored)==databases and len(set(restored))==databases)
    for value in restored:
        _relative(value)
    _require(verification['files_verified']==files and type(verification['files_verified']) is int)
    _require(rehearsal.get('version')=='local-recovery-rehearsal-v1' and rehearsal.get('state')=='passed')
    reread = rehearsal['verification']
    _require(isinstance(reread,dict)
             and {k:v for k,v in reread.items() if k!='verified_at'}
                 == {k:v for k,v in verification.items() if k!='verified_at'}
             and rehearsal['recovery_manifest_sha256']==_sha(backup['manifest_sha256']))
    _require(rehearsal.get('production_restored') is False and rehearsal.get('api_calls_allowed') is False
             and rehearsal.get('credential_access_allowed') is False and rehearsal.get('model_fit_requested') is False)
    rows = rehearsal['domain_checks']
    _require(isinstance(rows,list) and len(rows)==len(DOMAINS) and receipt['application_rehearsal']==rows)
    _require(all(isinstance(row,dict) for row in rows))
    _require({r['component'] for r in rows}==set(DOMAINS))
    for row in rows:
        _require(row.get('state')=='passed' and row.get('required') is DOMAINS[row['component']])
        _count(row['items'])
    _require(backup.get('atomic_across_databases') is False and backup.get('off_machine') is False
             and receipt.get('backup_is_global_atomic_snapshot') is False
             and receipt.get('full_service_disaster_recovery_demonstrated') is False
             and receipt['security'].get('backup_off_machine') is False)
    _require(_instant(backup['started_at']) <= _instant(backup['completed_at'])
             <= _instant(reread['verified_at']) <= _instant(rehearsal['checked_at'])
             <= _instant(receipt['received_at']))
    return dict(backup_files=files,databases_restored=databases,application_readers_passed=len(rows),
                backup_off_machine=False,full_service_recovery_demonstrated=False)


def _load_one(root, audit, lot, observed):
    name = f'PATTERNLAB2_RECEPTION_{lot.upper()}_{DATE}.json'
    raw,digest = _read(_path(audit,name),observed,f'{lot}:receipt')
    receipt = _json(raw)
    _require(receipt.get('version')==f'patternlab2-delivery-{lot}-v1'
             and receipt.get('state')==EXPECTED[lot])
    for key in ('full_program_complete','predictive_improvement_demonstrated','active_model_promoted',
                'candidate_weights_changed','old_forecasts_rewritten'):
        _require(receipt.get(key) is False)
    _instant(receipt['received_at'])
    archive_name = _relative(receipt['sources_archive'])
    _require(archive_name.parts[0]=='audit')
    archive = _path(audit,Path(*archive_name.parts[1:]).as_posix(),directory=True)
    sources = receipt['sources_sha256']
    _require(isinstance(sources,dict) and bool(sources))
    for name,expected in sorted(sources.items()):
        _data,actual = _read(_path(archive,name),observed,f'{lot}:source:{name}')
        _require(actual==_sha(expected))
    references = receipt['evidence_sha256']
    _require(isinstance(references,dict) and bool(references))
    evidence = {}
    for name,expected in sorted(references.items()):
        relative = _relative(name)
        _require(relative.parts[0]=='audit' and len(relative.parts)>1)
        short = Path(*relative.parts[1:]).as_posix()
        data,actual = _read(_path(audit,short),observed,f'{lot}:evidence:{short}')
        _require(actual==_sha(expected))
        evidence[short] = _json(data)
    tests = _tests(receipt,lot,audit,archive,evidence,observed)
    facts = _lot11(receipt,evidence) if lot=='lot11' else _lot12(receipt,evidence)
    return dict(id=lot,state='verified',recorded_at=_instant(receipt['received_at']).isoformat(),
                tests_passed=tests,facts=facts,issues=[],receipt_sha256=digest)


def load(project_root):
    root = Path(project_root).resolve()
    reader_sha = sha256(Path(__file__).read_bytes()).hexdigest()
    observed,rows = [],[]
    audit_root = root/'audit'
    bad_audit_root = audit_root.is_symlink() or (audit_root.exists() and not audit_root.is_dir())
    for lot in EXPECTED:
        name = f'PATTERNLAB2_RECEPTION_{lot.upper()}_{DATE}.json'
        empty = dict(id=lot,recorded_at=None,tests_passed=None,facts={},issues=[])
        if bad_audit_root:
            observed.append([f'{lot}:receipt','invalid_audit_path'])
            rows.append(dict(empty,state='invalid',issues=['historical_evidence_invalid']))
            continue
        if not (root/'audit'/name).exists() and not (root/'audit'/name).is_symlink():
            observed.append([f'{lot}:receipt','missing'])
            rows.append(dict(empty,state='missing',issues=['receipt_missing']))
            continue
        try:
            audit = _path(root,'audit',directory=True)
            rows.append(_load_one(root,audit,lot,observed))
        except (EvidenceError,OSError,ValueError,KeyError,TypeError,AttributeError,OverflowError,UnicodeError):
            rows.append(dict(empty,state='invalid',issues=['historical_evidence_invalid']))
    source = dict(version=VERSION,reader_sha256=reader_sha,observed=observed,
                  states=[dict(id=row['id'],state=row['state']) for row in rows])
    return dict(version=VERSION,rows=rows,source_sha256=sha256(json.dumps(source,sort_keys=True,
                separators=(',',':'),allow_nan=False).encode()).hexdigest(),reader_sha256=reader_sha,
                backup_current_checked=False,tests_rerun=False,receipts_signed=False,
                scope='archived_delivery_evidence_only')
