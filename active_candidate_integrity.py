"""Read-only integrity of one registered candidate, without model loading.

This binds the two artifact bytes and sealed metadata to a supplied registered
protocol. It does not prove that CatBoost can load the binary, predict with it,
or improve on a reference. No provider, fit, publication or repair is performed.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re

import causal_candidate as candidates


VERSION = 'active-candidate-integrity-v1'
FILES = ('candidate.cbm','report.json')
MAX_JSON_BYTES = 16*1024*1024
MAX_BINARY_BYTES = 256*1024*1024


def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def _hash(value):
    return isinstance(value,str) and re.fullmatch('[a-f0-9]{64}',value) is not None


def _instant(value):
    if not isinstance(value,str):
        raise ValueError()
    result=datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError()
    return result.astimezone(timezone.utc)


def _file_hash(path,limit):
    if path.stat().st_size>limit:
        raise ValueError()
    result=sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            result.update(block)
    return result.hexdigest()


def _safe_path(root,relative):
    path=root
    for part in relative.parts:
        path=path/part
        if path.is_symlink():
            raise ValueError()
    if not path.resolve().is_relative_to(root):
        raise ValueError()
    return path


def inspect(root,protocol):
    """Return valid/missing/invalid with safe issue codes and evidence hashes."""
    implementation={name:sha256((Path(__file__).parent/name).read_bytes()).hexdigest() for name in
        ('active_candidate_integrity.py','causal_candidate.py','causal_partitions.py',
         'scenario_features.py','halftime_domain.py','catboost_scenarios.py')}
    proof=dict(version=VERSION,implementation=implementation,protocol_record_sha256=None,artifacts={})
    issues=[]
    candidate_id=package_id=protocol_id=None
    status='invalid'
    try:
        if not isinstance(protocol,dict):
            raise ValueError()
        fields={key:protocol.get(key) for key in
            ('id','candidate_id','package_id','candidate_files','candidate_completed_at','registered_at')}
        proof['protocol_record_sha256']=sha256(_json(fields).encode()).hexdigest()
        if (any(not _hash(fields[key]) for key in ('id','candidate_id','package_id'))
                or not isinstance(fields['candidate_files'],dict)
                or set(fields['candidate_files'])!=set(FILES)
                or any(not _hash(value) for value in fields['candidate_files'].values())
                or _instant(fields['candidate_completed_at'])>_instant(fields['registered_at'])):
            raise ValueError()
        candidate_id,package_id,protocol_id=(fields[key] for key in ('candidate_id','package_id','id'))
    except (ValueError,TypeError,KeyError,OverflowError,RecursionError):
        issues.append('protocol_metadata_invalid')
    if not issues:
        try:
            given=Path(root)
            if given.is_symlink():
                raise ValueError()
            root_path=given.resolve()
            directory=_safe_path(root_path,Path('causal_confirmation')/'candidates'/candidate_id)
            paths={name:_safe_path(root_path,Path('causal_confirmation')/'candidates'/candidate_id/name)
                   for name in (*FILES,'COMPLETE.json')}
            missing=[name for name,path in paths.items() if not path.exists()]
            for name,path in paths.items():
                if path.exists():
                    if not path.is_file():
                        raise ValueError()
                    proof['artifacts'][name]=_file_hash(path,MAX_BINARY_BYTES if name=='candidate.cbm' else MAX_JSON_BYTES)
            if missing:
                status='missing';issues.append('candidate_artifact_missing')
            else:
                package=json.loads(paths['COMPLETE.json'].read_text(encoding='utf-8'))
                candidates._check_seal(package)
                if (package.get('version')!='causal-candidate-bundle-v1' or package.get('state')!='complete'
                        or package.get('id')!=package_id or package.get('candidate_id')!=candidate_id
                        or package.get('files')!=fields['candidate_files']):
                    raise ValueError()
                if any(proof['artifacts'][name]!=fields['candidate_files'][name] for name in FILES):
                    issues.append('candidate_artifact_hash_mismatch')
                else:
                    report=json.loads(paths['report.json'].read_text(encoding='utf-8'))
                    candidates.validate_report(report)
                    if (report['id']!=candidate_id
                            or _instant(report['completed_at'])!=_instant(fields['candidate_completed_at'])):
                        raise ValueError()
                    # Detect replacement while metadata was parsed; no binary
                    # is loaded and no path comes from an artifact's free text.
                    if any(_file_hash(paths[name],MAX_BINARY_BYTES if name=='candidate.cbm' else MAX_JSON_BYTES)
                           !=proof['artifacts'][name] for name in paths):
                        issues.append('candidate_changed_during_read')
                    else:
                        status='valid'
        except FileNotFoundError:
            status='missing';issues.append('candidate_artifact_missing')
        except (OSError,ValueError,TypeError,KeyError,AttributeError,OverflowError,RecursionError):
            status='invalid';issues.append('candidate_metadata_or_path_invalid')
    proof['status']=status
    proof['issues']=sorted(set(issues))
    return dict(version=VERSION,status=status,issues=proof['issues'],protocol_id=protocol_id,
                candidate_id=candidate_id,package_id=package_id,artifact_sha256=proof['artifacts'],
                implementation_sha256=implementation,source_sha256=sha256(_json(proof).encode()).hexdigest(),
                model_loaded=False,model_inference_checked=False,fit_requested=False,api_requested=False)
