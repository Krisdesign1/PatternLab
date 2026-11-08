"""Immutable offline study of the argumented rule; no serving or API changes."""
from collections import Counter
from datetime import date,datetime,timezone
import gzip
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import tempfile

import api_football as api
import argumented_selection_engine as engine


VERSION='argumented-selection-study-v1'
ROOT=Path(__file__).resolve().parent
DEFAULT_DIRECTORY=ROOT/'data/research/argumented_selection/2026-10-30-v1'
SOURCES=('argumented_selection_study.py','argumented_selection_replay.py','argumented_selection_statistics.py',
    'argumented_selection_engine.py','high_probability_engine.py','match_predictor.py',
    'club_identity.py','league_model.py','api_football.py')


def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def _hash(path):
    digest=sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda:handle.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def _new(path,value,*,compressed=False):
    """Publish a complete file exclusively; an interrupted write is resumable."""
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent,prefix='.study-',delete=False) as handle:
            temporary=Path(handle.name)
            if compressed:
                with gzip.GzipFile(fileobj=handle,mode='wb',mtime=0,compresslevel=4) as zipped:
                    with io.TextIOWrapper(zipped,encoding='utf-8') as text:
                        json.dump(value,text,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
            else:handle.write((_json(value)+'\n').encode())
            handle.flush();os.fsync(handle.fileno())
        os.link(temporary,path)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)


def _read(path):
    path=Path(path)
    if path.suffix=='.gz':
        with gzip.open(path,'rt',encoding='utf-8') as handle:return json.load(handle)
    return json.loads(path.read_text(encoding='utf-8'))


def _sources():
    return {name:_hash(ROOT/name) for name in SOURCES}


def _snapshot(root,end):
    """A single committed SQLite read, including the archive's own revision."""
    import fixture_store
    path=api.active_store(Path(root)/'fixtures.json')
    if path is None:
        first=api.data_signature(Path(root)/'fixtures.json')
        rows,audit=api.load_matches(Path(root)/'fixtures.json',end=end,statuses=api.FINISHED_STATUSES)
        if first!=api.data_signature(Path(root)/'fixtures.json'):
            raise ValueError('L’historique a changé pendant la copie ; relancer la préparation.')
        return dict(rows=rows,audit=audit,source_signature=first,status_inventory=None)
    rows=[];issues=[];expected=0
    with fixture_store._connection(path) as con:
        meta=fixture_store._meta(con)
        inventory=dict(con.execute('SELECT status,COUNT(*) FROM fixtures WHERE kickoff_utc<? GROUP BY status',(end,)))
        for row in con.execute("SELECT match_id,normalized_json,issue,expected FROM fixtures "
                "WHERE kickoff_utc<? AND status IN ('FT','AET','PEN') ORDER BY kickoff_utc,match_id",(end,)):
            expected+=row['expected']
            if row['normalized_json'] is not None:rows.append(json.loads(row['normalized_json']))
            if row['issue'] is not None:issues.append(dict(match_id=row['match_id'],reason=row['issue']))
    rows.sort(key=lambda row:(row['timestamp'],int(row['match_id'])))
    return dict(rows=rows,audit=dict(expected=expected,matched=len(rows),issues=issues),
        source_signature=dict(store_uuid=meta['store_uuid'],revision=int(meta['revision'])),status_inventory=inventory)


def prepare(directory=None,*,root=None,start='2024-01-01',end='2026-10-30'):
    import argumented_selection_replay as replay
    import argumented_selection_statistics as statistics
    directory=Path(directory or DEFAULT_DIRECTORY).resolve()
    start_day,end_day=date.fromisoformat(start),date.fromisoformat(end)
    if not start_day<end_day<=datetime.now(timezone.utc).date():
        raise ValueError('Période historique close requise.')
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/'plan.json'
    if path.exists():
        plan=_read(path)
        if plan['start']!=start or plan['end']!=end or plan['source_sha256']!=_sources():
            raise ValueError('La période ou le code diffère du protocole figé ; utiliser un nouveau dossier.')
    else:
        plan=dict(version=VERSION,created_at=datetime.now(timezone.utc).isoformat(),start=start,end=end,
            source='API-Football',source_sha256=_sources(),rule=engine.CRITERIA,replay=replay.PROTOCOL,
            policies=statistics.POLICIES,controls=['top_clubs','top_baseline'],
            fair_comparison='Exact same number selected per Kinshasa day, from the same >=10 role candidate pool.',
            fixed_variants=dict(recent_matching=[5,7],recent_age_days=[None,365]),
            bootstrap=dict(seed=statistics.BOOTSTRAP_SEED,resamples=statistics.BOOTSTRAP_RESAMPLES,
                min_complete_weeks=statistics.MIN_BOOTSTRAP_WEEKS),
            primary='Descriptive observed accuracy difference versus equally sized daily simple rankings.',
            secondary=['class logloss','Brier','calibration','coverage','year/league stability'],
            limits=['Historique déjà exploré : évaluation descriptive, non aveugle.',
                'État corrigé actuel de la base : disponibilité réelle passée des données et calendriers non démontrée.',
                'Population conditionnée aux matchs avec scores et identifiants exploitables présents dans la base.',
                'Statuts NS reconstruits pour le rejeu ; aucune capture avant match réelle n’est inventée.',
                'Clubs, ligues et journées sont liés ; les intervalles par blocs restent descriptifs.',
                'Les variantes sont fixées avant le calcul ; aucun gagnant n’est activé automatiquement.'],
            network=False,fitting=False,promotion=False,production_forecasts_changed=False)
        _new(path,plan)
    source_copy=directory/'sources.json.gz'
    if not source_copy.exists():
        texts={name:(ROOT/name).read_text(encoding='utf-8') for name in SOURCES}
        if {name:sha256(text.encode()).hexdigest() for name,text in texts.items()}!=plan['source_sha256']:
            raise ValueError('Le code a changé pendant sa copie.')
        _new(source_copy,dict(plan_sha256=_hash(path),files=texts),compressed=True)
    source=_read(source_copy)
    if (source.get('plan_sha256')!=_hash(path) or
            {name:sha256(text.encode()).hexdigest() for name,text in source['files'].items()}!=plan['source_sha256']):
        raise ValueError('La copie du code ne correspond pas au protocole.')
    if not (directory/'input_manifest.json').exists():
        if (directory/'input.json.gz').exists():
            snapshot=_read(directory/'input.json.gz')
        else:
            snapshot=_snapshot(Path(root or api.DATA_DIR),end)
            snapshot['snapshot_at']=datetime.now(timezone.utc).isoformat()
            snapshot['plan_sha256']=_hash(path)
            _new(directory/'input.json.gz',snapshot,compressed=True)
        if snapshot.get('plan_sha256')!=_hash(path):
            raise ValueError('La copie historique ne correspond pas au protocole.')
        _new(directory/'input_manifest.json',dict(version=VERSION,rows=len(snapshot['rows']),
            source_signature=snapshot['source_signature'],input_sha256=_hash(directory/'input.json.gz'),
            plan_sha256=_hash(path),by_year=dict(Counter(row['timestamp'][:4] for row in snapshot['rows']))))
    manifest=_read(directory/'input_manifest.json')
    if manifest['plan_sha256']!=_hash(path) or manifest['input_sha256']!=_hash(directory/'input.json.gz'):
        raise ValueError('L’instantané historique a changé.')
    return dict(directory=str(directory),plan=plan,manifest=manifest)


def run(directory=None,*,root=None,start='2024-01-01',end='2026-10-30',progress=None):
    import argumented_selection_replay as replay
    import argumented_selection_statistics as statistics
    ready=prepare(directory,root=root,start=start,end=end)
    directory=Path(ready['directory']);plan=ready['plan']
    if (directory/'completed.json').exists():return load_report(directory)
    if not (directory/'replay_manifest.json').exists():
        if (directory/'replay.json.gz').exists():
            result=_read(directory/'replay.json.gz')
        else:
            snapshot=_read(directory/'input.json.gz')
            result=replay.replay(snapshot['rows'],start=start,end=end,progress=progress)
            result.update(plan_sha256=_hash(directory/'plan.json'),input_sha256=_hash(directory/'input.json.gz'))
            _new(directory/'replay.json.gz',result,compressed=True)
        if (result.get('plan_sha256')!=_hash(directory/'plan.json')
                or result.get('input_sha256')!=_hash(directory/'input.json.gz')):
            raise ValueError('Le rejeu ne correspond pas à son protocole ou à ses données.')
        _new(directory/'replay_manifest.json',dict(replay_sha256=_hash(directory/'replay.json.gz'),
            plan_sha256=_hash(directory/'plan.json'),input_sha256=_hash(directory/'input.json.gz'),
            records=len(result['records'])))
    else:result=_read(directory/'replay.json.gz')
    manifest=_read(directory/'replay_manifest.json')
    if (manifest['replay_sha256']!=_hash(directory/'replay.json.gz')
            or manifest['plan_sha256']!=_hash(directory/'plan.json')
            or manifest['input_sha256']!=_hash(directory/'input.json.gz')):
        raise ValueError('Le rejeu ne correspond pas à son instantané.')
    if progress:progress(dict(stage='statistics',records=len(result['records'])))
    if not (directory/'report.json').exists():
        report=statistics.summarize(result['records'])
        report['statistics_protocol']=report['protocol']
        report.update(study_version=VERSION,generated_at=datetime.now(timezone.utc).isoformat(),
            plan_sha256=_hash(directory/'plan.json'),input_sha256=_hash(directory/'input.json.gz'),
            replay_sha256=_hash(directory/'replay.json.gz'),inventory=ready['manifest'],
            replay_diagnostics=result['diagnostics'],protocol=plan)
        report['report_id']=sha256(_json(report).encode()).hexdigest()
        _new(directory/'report.json',report)
    report=_read(directory/'report.json')
    if (report.get('plan_sha256')!=_hash(directory/'plan.json')
            or report.get('input_sha256')!=_hash(directory/'input.json.gz')
            or report.get('replay_sha256')!=_hash(directory/'replay.json.gz')
            or report.get('report_id')!=sha256(_json({k:v for k,v in report.items() if k!='report_id'}).encode()).hexdigest()):
        raise ValueError('Le rapport ne correspond pas au protocole.')
    _new(directory/'completed.json',dict(version=VERSION,finished_at=datetime.now(timezone.utc).isoformat(),
        files={name:_hash(directory/name) for name in ('plan.json','sources.json.gz','input_manifest.json','input.json.gz',
            'replay_manifest.json','replay.json.gz','report.json')}))
    return load_report(directory)


def load_report(directory=None):
    """Cheap verified report read; never opens fixtures or launches the replay."""
    directory=Path(directory or DEFAULT_DIRECTORY)
    if not (directory/'completed.json').exists():return None
    done=_read(directory/'completed.json')
    if done.get('version')!=VERSION:raise ValueError('Version du bilan historique invalide.')
    for name in ('report.json','plan.json'):
        if done['files'].get(name)!=_hash(directory/name):raise ValueError('Le bilan historique conservé est altéré.')
    return _read(directory/'report.json')


def signature(directory=None):
    directory=Path(directory or DEFAULT_DIRECTORY)
    return tuple((str(p),p.stat().st_mtime_ns,p.stat().st_size)
        for p in (directory/'completed.json',directory/'report.json',directory/'plan.json') if p.exists())
