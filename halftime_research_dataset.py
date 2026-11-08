"""Read-only source extraction and immutable research snapshots, without Cloud."""
from datetime import datetime, timezone
from hashlib import sha256
import gzip
import json
from pathlib import Path
import sqlite3

from halftime_statistics_parser import parse_statistics
from halftime_statistics_collector import fixture
from halftime_research_features import LEAGUES


def _connection(path):
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)
    db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
    return db


def normalize(raw,cutoff):
    # Discovery covers FT only. Existing normalized rows also retain AET/PEN
    # regulation-time scores as warmup, never as newly discovered FT targets.
    value=fixture(raw,before=cutoff)
    if value is None:return None
    ht=raw['score']['halftime'];ft=raw['score']['fulltime']
    first=ht['home']+ht['away'];second=ft['home']+ft['away']-first
    return dict(match_id=value['match_id'],league=value['league'],timestamp=value['kickoff_utc'],
        season=value['season'],home_id=value['home_id'],away_id=value['away_id'],
        home=raw['teams']['home']['name'],away=raw['teams']['away']['name'],
        ht=f"{ht['home']}-{ht['away']}",ft=f"{ft['home']}-{ft['away']}",
        value='1st' if first>second else '2nd' if second>first else 'Égal',provider='api-football')


def extract(fixtures_path,statistics_path,*,cutoff,leagues=LEAGUES):
    """No transaction is held while another store/network is consulted."""
    if cutoff.tzinfo is None:raise ValueError('Aware cutoff required.')
    selected=set(map(str,leagues));cutoff_text=cutoff.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds')
    base=_connection(fixtures_path)
    try:
        marks=','.join('?' for _ in selected)
        historical=[json.loads(r[0]) for r in base.execute('''SELECT normalized_json FROM fixtures
            WHERE normalized_json IS NOT NULL AND kickoff_utc<? AND league IN ('''+marks+') ORDER BY kickoff_utc',
            (cutoff_text,*sorted(selected)))]
    finally:base.rollback();base.close()
    rows={str(r['match_id']):r for r in historical if str(r['league']) in selected}
    side=_connection(statistics_path)
    try:
        manifests=[dict(r) for r in side.execute('SELECT * FROM discoveries') if str(r['league']) in selected]
        tasks=[dict(r) for r in side.execute('SELECT * FROM tasks') if str(r['league']) in selected and r['kickoff_utc']<cutoff_text]
        observations=[dict(r) for r in side.execute('''SELECT t.*,a.finished_at,a.envelope_json,a.id AS observation_id
            FROM tasks t JOIN attempts a ON a.id=t.latest_observation WHERE a.envelope_json IS NOT NULL''')
            if str(r['league']) in selected and r['kickoff_utc']<cutoff_text]
    finally:side.rollback();side.close()
    conflicts=[];universe={}
    for manifest in manifests:
        envelope=json.loads(manifest['payload_json'])
        params=envelope.get('parameters',{})
        if (envelope.get('transport')!='received' or envelope.get('http_status')!=200
            or envelope.get('endpoint')!='fixtures' or envelope.get('payload',{}).get('errors')
            or str(params.get('league'))!=str(manifest['league']) or params.get('season')!=manifest['season']
            or params.get('status')!='FT' or envelope.get('payload',{}).get('paging',{}).get('total',1)!=1
            or not isinstance(envelope.get('payload',{}).get('response'),list)):
            raise ValueError('Invalid saved discovery manifest.')
        for raw in envelope['payload']['response']:
            row=normalize(raw,cutoff)
            if row is None or row['league'] not in selected:continue
            mid=row['match_id']
            if row['league']!=str(manifest['league']) or row['season']!=manifest['season']:
                raise ValueError('Discovery metadata mismatch.')
            if row['timestamp']>='2024-01-01T00:00:00':
                universe[mid]=dict(match_id=mid,league=row['league'],season=row['season'],timestamp=row['timestamp'])
            old=rows.get(mid)
            if old:
                fields=('league','season','timestamp','home_id','away_id','ht','ft','value')
                if any(str(old.get(k))!=str(row.get(k)) for k in fields):
                    conflicts.append(dict(match_id=mid,reason='Local/discovery score or identity conflict'))
                    continue
            else:rows[mid]=row
    if conflicts:
        raise ValueError('Conflicting local/discovery fixtures; resolve before research snapshot.')
    statistics={}
    for row in observations:
        if row['match_id'] not in rows:continue
        source=rows[row['match_id']]
        if any(str(source[k])!=str(row[k]) for k in ('league','season','home_id','away_id')) or source['timestamp']!=row['kickoff_utc']:
            raise ValueError('Statistics/score identity or kickoff mismatch.')
        envelope=json.loads(row['envelope_json'])
        if (envelope.get('endpoint')!='fixtures/statistics' or str(envelope.get('parameters',{}).get('fixture'))!=row['match_id']
            or envelope.get('parameters',{}).get('half')!='true'):
            raise ValueError('Wrong statistics endpoint or fixture.')
        if envelope.get('transport')!='received' or envelope.get('http_status') not in (200,204):continue
        parsed=parse_statistics(envelope.get('payload'),(source['home_id'],source['away_id']))
        if parsed['status']!='ok':continue
        statistics[row['match_id']]=dict(observed_at=row['finished_at'],home_id=source['home_id'],away_id=source['away_id'],
            periods={p:block['pairs'] for p,block in parsed['periods'].items() if p in ('first','second')},
            core_available=parsed['halves_core_shots_available'],observation_id=row['observation_id'])
    # Readiness depends on request resolution, never actual labels or target stats.
    task_by_id={r['match_id']:r for r in tasks};blocks={}
    for mid,item in universe.items():
        year=item['timestamp'][:4]
        if year not in ('2024','2025','2026'):continue
        b=blocks.setdefault(year,dict(targets=0,observed=0,rich_core=0,missing_requests=0))
        b['targets']+=1
        known=task_by_id.get(mid,{}).get('availability') in ('ready','partial','empty','invalid','unknown_shape')
        b['observed']+=int(known);b['missing_requests']+=int(not known)
        b['rich_core']+=int(statistics.get(mid,{}).get('core_available',False))
    return dict(rows=sorted(rows.values(),key=lambda r:(r['timestamp'],int(r['match_id']))),
        statistics=statistics,universe=sorted(universe.values(),key=lambda r:(r['timestamp'],int(r['match_id']))),
        coverage=blocks,source=dict(fixtures=str(fixtures_path),statistics=str(statistics_path),
                                    cutoff_utc=cutoff.isoformat(),discovery_manifests=len(manifests)))


def write_snapshot(directory,data):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=False)
    for name,value in data.items():
        path=directory/(name+'.json.gz')
        with gzip.open(path,'xt',encoding='utf-8') as handle:
            json.dump(value,handle,ensure_ascii=False,separators=(',',':'),allow_nan=False)
    hashes={p.name:sha256(p.read_bytes()).hexdigest() for p in directory.iterdir()}
    with (directory/'hashes.json').open('x') as handle:json.dump(hashes,handle,indent=2)
    return hashes


def load_snapshot(directory):
    directory=Path(directory)
    hashes=json.loads((directory/'hashes.json').read_text())
    for name,digest in hashes.items():
        if sha256((directory/name).read_bytes()).hexdigest()!=digest:raise ValueError('Snapshot hash mismatch.')
    out={}
    for path in directory.glob('*.json.gz'):
        with gzip.open(path,'rt',encoding='utf-8') as handle:out[path.name[:-8]]=json.load(handle)
    return out
