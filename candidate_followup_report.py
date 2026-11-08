"""Dated observations outside the frozen candidate engines and study rules.

Reading never trains, fetches results or reconciles them. Snapshots are separate,
immutable observations of existing evidence; repeated reads are not new trials.
"""
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from math import fsum
from pathlib import Path
import re
import sqlite3
from zoneinfo import ZoneInfo

import api_football as api
import candidate_tracking as tracking
import journal_uncertainty as uncertainty
import model_reassessment as review
import prediction_journal as journal

VERSION = 'candidate-followup-v1'


def snapshot_path(root=None):
    return tracking._root(root)/'candidate_followup.sqlite3'


def _now(now=None):
    value = now if now is not None else datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('Le bilan doit préciser son heure avec fuseau.')
    return value.astimezone(timezone.utc)


def _implementation():
    # A code-compatibility change is a new operational observation even when
    # the same forecasts/results remain in the source. It never changes their
    # pinned versions or evaluation rules.
    return hashlib.sha256(Path(__file__).read_bytes()+review._json(tracking._implementation()).encode()).hexdigest()


def _inputs(root):
    """Read the source revision tokens together, without creating any database."""
    path = tracking.database_path(root)
    if not path.exists():
        return {}
    try:
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=15)) as con:
            con.execute('BEGIN')
            tables={row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'studies','predictions','results'}.issubset(tables):
                raise ValueError('La base du suivi est incomplète ; aucun compteur n’est déduit.')
            definitions={row[0]:row[1] for row in con.execute('SELECT id,digest FROM studies ORDER BY id')}
            forecasts=defaultdict(list)
            for identity,digest,study_id,revision in con.execute('''
                SELECT p.id,p.digest,json_extract(p.payload,'$.metadata.study_id'),COALESCE(MAX(r.revision),0)
                FROM predictions p LEFT JOIN results r ON r.prediction_id=p.id
                GROUP BY p.id ORDER BY p.id'''):
                if study_id not in definitions:
                    raise ValueError('Une prévision ne correspond à aucun suivi enregistré.')
                forecasts[study_id].append([identity,digest,revision])
            return {sid:dict(study_digest=digest,forecasts=forecasts[sid]) for sid,digest in definitions.items()}
    except sqlite3.Error:
        raise ValueError('La source du bilan est illisible ; les compteurs sont indisponibles.') from None


def _metrics(entries):
    settled=[e for e in entries if e['status']=='settled']
    result=[]
    for name in tracking.METHODS:
        scores=[e['evaluations'][name] for e in settled]
        n=len(scores)
        result.append(dict(method=name,groups=n,loss=fsum(s['loss'] for s in scores)/n if n else None,
                           top1=sum(s['top1'] for s in scores),top3=sum(s['top3'] for s in scores)))
    return result


def _counts(entries):
    counts=Counter(e['status'] for e in entries)
    return dict(groups=len(entries),unique_matches=len({f['match_id'] for e in entries for f in e['fixtures']}),
                **{name:counts[name] for name in ('pending','settled','unresolved','void')})


def build(summary, *, now=None, implementation_compatible=None):
    """Period tables retain global dependency eligibility before any filtering."""
    current=_now(now)
    study=summary['study']
    entries=summary['entries']
    by_size={value['size']:value for value in summary['sizes']}
    daily=defaultdict(list)
    weekly=defaultdict(list)
    timing={}
    for entry in entries:
        size=entry['size']
        if size not in study['sizes'] or entry['metadata']['study_id']!=study['id']:
            raise ValueError('Le bilan contient une cohorte incompatible.')
        stamps=[journal._stamp(f['timestamp']) for f in entry['fixtures']]
        first=min(stamps)
        created=journal._stamp(entry['created_at'])
        daily[(first.astimezone(ZoneInfo(tracking.TIMEZONE)).date().isoformat(),size)].append(entry)
        weekly[(uncertainty._week(first).isoformat(),size)].append(entry)
        timing[entry['id']]=(first-created).total_seconds()
    daily_rows=[]
    for (day,size),rows in sorted(daily.items()):
        leads=[timing[e['id']] for e in rows]
        daily_rows.append(dict(day=day,size=size,**_counts(rows),
            before_kickoff=sum(lead>0 for lead in leads),late=sum(lead<=0 for lead in leads),
            min_lead_seconds=min(leads)))
    weekly_rows=[]
    for (week,size),rows in sorted(weekly.items()):
        # Eligibility comes from the complete same-size cohort: never refit a
        # weekly interval after dropping shared fixtures or unresolved groups.
        comparison=next(c for c in by_size[size]['evidence']['comparisons'] if c['method']=='catboost')
        eligible=set(comparison['group_ids'])
        weekly_rows.append(dict(week_utc=week[:10],size=size,**_counts(rows),
            calendar_closed=week<uncertainty._week(current).isoformat(),
            eligible_groups=sum(e['id'] in eligible for e in rows),metrics=_metrics(rows)))
    return dict(study_id=study['id'],league=study['league'],registered_at=study['registered_at'],
        rules_fingerprint=review._digest(study),implementation_compatible=implementation_compatible,
        groups=len(entries),unique_matches=len({f['match_id'] for e in entries for f in e['fixtures']}),
        by_size=[dict(size=s['size'],counts=s['counts'],unique_matches=s['unique_matches'],
            closed_blocks=s['candidate_vs_incumbent']['blocks'],eligible_groups=s['candidate_vs_incumbent']['groups'],
            metrics=s['metrics']) for s in summary['sizes']],daily=daily_rows,weekly=weekly_rows,
        warnings=[
            'Journées de match à Kinshasa ; semaines de dépendance en UTC. Les périodes conservent les horaires enregistrés.',
            'Une semaine calendaire close ne garantit pas un bloc clos admissible. Les corrections horaires conservent leurs dépendances globales.',
            'Les erreurs hebdomadaires décrivent les groupes évalués observés, y compris ceux encore exclus des blocs clos.',
            'Les tailles partagent des rencontres ; leurs groupes et effectifs ne sont pas des observations indépendantes à additionner.',
            'Un relevé supplémentaire décrit l’état des mêmes prévisions ; il ne crée aucun nouvel essai ni confirmation.'])


def collect(root=None, *, study_id=None, now=None):
    """Read a consistent state or fail explicitly; never mutate source data."""
    root=tracking._root(root)
    for attempt in range(2):
        implementation=_implementation()
        before=_inputs(root)
        # On a retry, sample the real clock after acquiring the new revision
        # tokens. A forecast arriving during the first read must not appear in
        # an observation dated before that forecast was actually committed.
        current=_now(now)
        selected={key:value for key,value in before.items() if study_id is None or key==study_id}
        if study_id is not None and study_id not in selected:
            raise ValueError('Le suivi demandé est absent ; aucun bilan nul n’est inventé.')
        reports=[]
        for sid in selected:
            summary=tracking.summary(sid,root,now=current)
            reports.append(build(summary,now=current,
                implementation_compatible=summary['study']['implementation']==tracking._implementation()))
        after=_inputs(root)
        if (all(after.get(key)==value for key,value in selected.items())
                and (study_id is not None or set(before)==set(after)) and implementation==_implementation()):
            for report in reports:
                report['input_fingerprint']=review._digest(selected[report['study_id']])
                report['source_revisions']=selected[report['study_id']]['forecasts']
            return dict(version=VERSION,source='API-Football',as_of=current.isoformat(),
                input_fingerprint=review._digest(selected),report_implementation=implementation,
                source_state='available' if reports else 'not_started',studies=reports,
                warnings=['Lecture seule : aucun entraînement, appel API, rapprochement ou changement de règles.'])
    raise ValueError('Les résultats évoluent pendant la lecture ; réessayez pour obtenir un relevé cohérent.')


def _connection(root, *, create=False):
    path=snapshot_path(root)
    if not create:
        if not path.exists(): return None
        con=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=15)
    else:
        path.parent.mkdir(parents=True,exist_ok=True)
        con=sqlite3.connect(path,timeout=15)
        con.executescript('''
            CREATE TABLE IF NOT EXISTS snapshots(id TEXT PRIMARY KEY,study_id TEXT NOT NULL,
                observed_at TEXT NOT NULL,utc_day TEXT NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS snapshots_no_update BEFORE UPDATE ON snapshots
                BEGIN SELECT RAISE(ABORT,'Observations are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS snapshots_no_delete BEFORE DELETE ON snapshots
                BEGIN SELECT RAISE(ABORT,'Observations are immutable'); END;
        ''')
    con.row_factory=sqlite3.Row
    return con


def capture(root=None, *, now=None):
    """Scheduler-only, idempotent observation per UTC day and source revision.

    This is bookkeeping, never a new statistical test or permission to promote.
    A real observation always uses the real clock; tests use isolated roots.
    """
    tracking._write_allowed()
    root=tracking._root(root)
    if now is not None and root==Path(api.DATA_DIR).resolve():
        raise ValueError('Un relevé réel utilise uniquement l’horloge UTC.')
    inputs=_inputs(root)
    if not inputs: return dict(status='idle',created=0,existing=0)
    current=_now(now)
    utc_day=current.date().isoformat()
    keys={sid:review._digest([VERSION,_implementation(),sid,utc_day,review._digest(value)])
          for sid,value in inputs.items()}
    con=_connection(root)
    try:
        existing=set()
        if con:
            for sid in keys.values():
                row=con.execute('SELECT * FROM snapshots WHERE id=?',(sid,)).fetchone()
                if row is not None:
                    _decode(row)
                    existing.add(sid)
    finally:
        if con: con.close()
    if set(keys.values()).issubset(existing):
        return dict(status='unchanged',created=0,existing=len(keys))
    # Report identity is based on the source actually observed, not on tokens
    # read before a concurrent result revision.
    report=collect(root,now=now)
    utc_day=journal._stamp(report['as_of']).date().isoformat()
    created=0
    with closing(_connection(root,create=True)) as con,con:
        for study in report['studies']:
            value=dict(study_id=study['study_id'],as_of=report['as_of'],utc_day=utc_day,
                version=VERSION,report_implementation=report['report_implementation'],
                input_fingerprint=study['input_fingerprint'],study=study)
            sid=review._digest([VERSION,report['report_implementation'],study['study_id'],utc_day,study['input_fingerprint']])
            value['snapshot_id']=sid
            encoded=review._json(value)
            cursor=con.execute('INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?,?,?)',
                (sid,study['study_id'],report['as_of'],utc_day,encoded,review._digest(value)))
            created+=cursor.rowcount
    return dict(status='captured',created=created,existing=len(report['studies'])-created)


def _decode(row):
    value=json.loads(row['payload'])
    if (review._digest(value)!=row['digest'] or value.get('snapshot_id')!=row['id']
            or value.get('study_id')!=row['study_id'] or value.get('as_of')!=row['observed_at']
            or value.get('utc_day')!=row['utc_day'] or value.get('version')!=VERSION):
        raise ValueError('Le relevé daté est altéré ou incompatible.')
    expected=review._digest([VERSION,value['report_implementation'],value['study_id'],value['utc_day'],value['input_fingerprint']])
    if value['snapshot_id']!=expected:
        raise ValueError('L’identité du relevé ne correspond plus à ses entrées.')
    return value


def snapshots(root=None, study_id=None):
    con=_connection(root)
    if con is None: return []
    try:
        query='SELECT * FROM snapshots'+(' WHERE study_id=?' if study_id else '')+' ORDER BY observed_at DESC,id'
        params=(study_id,) if study_id else ()
        output=[]
        for row in con.execute(query,params):
            value=_decode(row)
            output.append(dict(snapshot_id=value['snapshot_id'],study_id=value['study_id'],
                observed_at=value['as_of'],utc_day=value['utc_day'],groups=value['study']['groups'],
                unique_matches=value['study']['unique_matches'],input_fingerprint=value['input_fingerprint']))
        return output
    except (sqlite3.Error,json.JSONDecodeError,KeyError,TypeError):
        raise ValueError('Les relevés datés sont indisponibles ; aucun historique vide n’est déduit.') from None
    finally:
        con.close()


def load_snapshot(snapshot_id,root=None):
    if not isinstance(snapshot_id,str) or not re.fullmatch('[0-9a-f]{64}',snapshot_id):
        raise ValueError('Identifiant de relevé invalide.')
    con=_connection(root)
    if con is None: raise ValueError('Aucun relevé daté disponible.')
    try:
        row=con.execute('SELECT * FROM snapshots WHERE id=?',(snapshot_id,)).fetchone()
        if row is None: raise ValueError('Relevé demandé absent.')
        return _decode(row)
    except (sqlite3.Error,json.JSONDecodeError,KeyError,TypeError):
        raise ValueError('Le relevé daté demandé est illisible.') from None
    finally:
        con.close()
