"""Write-once individual forecasts for the daily candidate procedure.

One fixture counts once per protocol, regardless of later daily generations.
This store never trains, promotes, places bets or reconstructs past forecasts.
Provider confirmation is supplied by the existing scheduler's quota-aware gate.
"""
from collections import Counter, defaultdict
from contextlib import closing
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from math import fsum, isclose, isfinite, log
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

import api_football as api
import api_match_workspace as calendar
import cloud_storage
import high_probability_archive as old_archive

VERSION = 'daily-learning-individual-tracking-v1'
METHODS = ('candidate', 'clubs', 'frequency')
VALUES = ('1st', 'Égal', '2nd')
ZONE = ZoneInfo('Africa/Kinshasa')


class TrackingError(ValueError):
    pass


def _root(root=None):
    return Path(root if root is not None else api.DATA_DIR).resolve()


def database_path(root=None):
    return _root(root)/'daily_learning'/'tracking.sqlite3'


def signature(root=None):
    """Cheap read-only cache token; WAL changes must invalidate the reader."""
    path = database_path(root)
    return tuple((str(item), item.stat().st_mtime_ns, item.stat().st_size)
                 for item in (path, Path(str(path)+'-wal')) if item.exists())


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _digest(value):
    return sha256(_json(value).encode()).hexdigest()


def _stamp(value):
    if not isinstance(value, str):
        raise TrackingError('Un horodatage explicite est nécessaire.')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise TrackingError('Horodatage invalide.') from None
    # Existing canonical fixture timestamps are UTC without a suffix.
    return (result.replace(tzinfo=timezone.utc) if result.tzinfo is None
            else result.astimezone(timezone.utc))


def _now(root=None, clock=None, value=None):
    # Production captures/reconciliations cannot be backdated by a caller.
    result = datetime.now(timezone.utc) if _root(root) == Path(api.DATA_DIR).resolve() else (
        value if value is not None else clock() if clock else datetime.now(timezone.utc))
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise TrackingError('Une horloge avec fuseau horaire est nécessaire.')
    return result.astimezone(timezone.utc)


def _read_time(value=None):
    """Read-only as-of views are allowed; mutation clocks cannot be backdated."""
    result = datetime.now(timezone.utc) if value is None else value
    if not isinstance(result,datetime) or result.tzinfo is None:
        raise TrackingError('Une date de consultation avec fuseau est nécessaire.')
    return result.astimezone(timezone.utc)


def _probabilities(value):
    if (not isinstance(value, dict) or set(value) != set(VALUES)
            or any(type(p) not in (int, float) or not isfinite(p) or not 0 < p <= 1
                   for p in value.values())
            or not isclose(fsum(value.values()), 1., abs_tol=1e-10)):
        raise TrackingError('Les trois probabilités doivent être valides et normalisées.')
    return {label: float(value[label]) for label in VALUES}


def _fixture(value):
    if not isinstance(value, dict) or value.get('provider') != 'api-football':
        raise TrackingError('Seule une affiche API-Football confirmée est admise.')
    try:
        result = {key: api._numeric_id(value[key], key)
                  for key in ('match_id', 'league', 'home_id', 'away_id')}
        if result['home_id'] == result['away_id']:
            raise ValueError()
        for key in ('home', 'away'):
            if not isinstance(value[key], str) or not value[key].strip():
                raise ValueError()
            result[key] = value[key]
        if type(value.get('season')) is not int or not 1900 <= value['season'] <= 2100:
            raise ValueError()
        result.update(timestamp=_stamp(value['timestamp']).replace(tzinfo=None).isoformat(timespec='seconds'),
                      provider='api-football', season=value['season'])
        for key in ('competition', 'country'):
            if isinstance(value.get(key), str):
                result[key] = value[key]
        return result
    except (KeyError, ValueError, TypeError):
        raise TrackingError('Affiche, saison ou identifiants incomplets.') from None


def _connect(root=None, *, create=False):
    path = database_path(root)
    if not create and not path.exists():
        return None
    if create:
        cloud_storage._assert_writable()
        if cloud_storage.configured():
            raise TrackingError('Le suivi quotidien exige le stockage local.')
        path.parent.mkdir(parents=True, exist_ok=True)
    mode = 'rwc' if create else 'ro'
    con = sqlite3.connect(path.as_uri()+'?mode='+mode, uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    if create:
        con.execute('PRAGMA journal_mode=WAL')
        con.executescript('''
            CREATE TABLE IF NOT EXISTS protocols(
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, digest TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS forecasts(
                id TEXT PRIMARY KEY, protocol_id TEXT NOT NULL REFERENCES protocols(id),
                match_id TEXT NOT NULL, captured_at TEXT NOT NULL,
                payload TEXT NOT NULL, digest TEXT NOT NULL,
                UNIQUE(protocol_id,match_id));
            CREATE TABLE IF NOT EXISTS results(
                forecast_id TEXT NOT NULL REFERENCES forecasts(id), revision INTEGER NOT NULL,
                checked_at TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL,
                PRIMARY KEY(forecast_id,revision));
            CREATE INDEX IF NOT EXISTS daily_learning_capture ON forecasts(captured_at);
            CREATE INDEX IF NOT EXISTS daily_learning_results ON results(forecast_id,revision);
            CREATE TRIGGER IF NOT EXISTS daily_protocol_no_update BEFORE UPDATE ON protocols
                BEGIN SELECT RAISE(ABORT,'Protocols are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS daily_protocol_no_delete BEFORE DELETE ON protocols
                BEGIN SELECT RAISE(ABORT,'Protocols are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS daily_forecast_no_update BEFORE UPDATE ON forecasts
                BEGIN SELECT RAISE(ABORT,'Forecasts are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS daily_forecast_no_delete BEFORE DELETE ON forecasts
                BEGIN SELECT RAISE(ABORT,'Forecasts are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS daily_result_no_update BEFORE UPDATE ON results
                BEGIN SELECT RAISE(ABORT,'Result revisions are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS daily_result_no_delete BEFORE DELETE ON results
                BEGIN SELECT RAISE(ABORT,'Result revisions are immutable'); END;
        ''')
    else:
        con.execute('PRAGMA query_only=ON')
    return con


def _decode(row):
    if sha256(row['payload'].encode()).hexdigest() != row['digest']:
        raise TrackingError('Un enregistrement du suivi quotidien est altéré.')
    return json.loads(row['payload'])


def _identity(protocol_id, mid):
    return _digest([VERSION, protocol_id, mid])


def _validate_forecast(value):
    fixture = _fixture(value['fixture'])
    if (value.get('version') != VERSION or value.get('id') != _identity(value['protocol_id'], fixture['match_id'])
            or value['day'] != _stamp(fixture['timestamp']).astimezone(ZONE).date().isoformat()
            or value['captured_at'] != value['created_at']):
        raise TrackingError('Identité du suivi quotidien incohérente.')
    captured, completed = _stamp(value['captured_at']), _stamp(value['candidate_completed_at'])
    kickoff = _stamp(fixture['timestamp'])
    confirmation = value.get('confirmation') or {}
    if (confirmation.get('provider') != 'api-football' or confirmation.get('status') != 'NS'
            or _stamp(confirmation.get('checked_at')) > captured
            or len(confirmation.get('raw_sha256','')) != 64):
        raise TrackingError('Confirmation fournisseur quotidienne incohérente.')
    try:
        for key in ('before', 'baseline_before', 'trained_through'):
            if date.fromisoformat(value[key]).isoformat() != value[key]:
                raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise TrackingError('La coupure historique quotidienne est invalide.') from None
    if (completed > captured or captured >= kickoff
            or value['before'] > min(captured.date(), kickoff.date()).isoformat()
            or value['baseline_before'] > min(captured.date(), kickoff.date()).isoformat()
            or value['trained_through'] >= value['before']):
        raise TrackingError('Une prévision quotidienne contient une information trop tardive.')
    if set(value['methods']) not in (set(METHODS), set(METHODS)|{'incumbent'}):
        raise TrackingError('Comparateurs incomplets du suivi quotidien.')
    for p in value['methods'].values():
        _probabilities(p)
    return value


def _confirmed(fixture, raw, current):
    if raw.get('provider') not in (None, 'api-football', 'API-Football'):
        raise TrackingError('Source de confirmation différente.')
    fresh = calendar.fixture_from_raw(raw)
    if any(str(fresh[key]) != str(fixture[key]) for key in
           ('match_id', 'league', 'home_id', 'away_id', 'season', 'timestamp')):
        raise TrackingError('Identité ou horaire modifié depuis le calcul.')
    if api._status_short(raw) != 'NS' or _stamp(fresh['timestamp']) <= current:
        raise TrackingError('Le match n’est plus confirmé à venir.')
    import fixture_import_policy
    if fixture_import_policy.QUALITY_KEY in raw:
        raise TrackingError('Un relevé local annoté ne remplace pas une confirmation fournisseur.')
    return dict(provider='api-football', status='NS', checked_at=current.isoformat(),
                raw_sha256=_digest(raw))


def _report_counts():
    return dict(status='idle', created=0, existing=0, unavailable=0, issues=[])


def capture(report, candidates, rows, root=None, *, clock=None, refresh=None, check_time=None):
    """Archive a first individual comparison after a fresh provider check.

    ``refresh`` is injected by the scheduler and returns the raw fixture list.
    Its provider/quota exceptions propagate. No request is made by this module.
    """
    import daily_learning_models as models
    output = _report_counts()
    protocol = models.load_protocol(root)
    if protocol is None:
        return dict(output, status='not_started')
    if not isinstance(protocol.get('id'), str):
        raise TrackingError('Protocole quotidien absent ou invalide.')
    current = _now(root, clock)
    protocol_created = _stamp(protocol['created_at'])
    if protocol_created > current:
        raise TrackingError('Le suivi ne peut pas précéder son protocole.')
    available = {(entry['protocol_id'], entry['fixture']['match_id']) for entry in entries(root, now=current)}
    grouped, seen, conflicts = defaultdict(list), {}, set()
    for item in report.get('rows', []):
        try:
            fixture = _fixture(item['fixture'])
            mid = fixture['match_id']
            if mid in seen:
                if _json(seen[mid]) != _json(item): conflicts.add(mid)
                continue
            seen[mid] = item
        except (ValueError, KeyError, TypeError):
            output['unavailable'] += 1
    for mid,item in seen.items():
        fixture = _fixture(item['fixture'])
        if mid in conflicts:
            output['unavailable'] += 1
            continue
        if (protocol['id'],mid) in available:
            output['existing'] += 1
            continue
        if _stamp(fixture['timestamp']) <= current:
            output['unavailable'] += 1
            continue
        if fixture['league'] not in candidates:
            continue
        grouped[fixture['league']].append((fixture,item))
    pending = []
    for league, group in sorted(grouped.items()):
        if check_time: check_time()
        manifest = candidates[league]
        fixtures = [f for f,_ in group]
        try:
            if (manifest.get('protocol_id') != protocol['id'] or manifest.get('status') != 'completed'
                    or str(manifest.get('league')) != league or _stamp(manifest['completed_at']) > _now(root,clock)):
                raise TrackingError('Candidat quotidien incomplet ou incompatible avec le protocole.')
            candidate = models.predict(manifest, rows, fixtures, root=root)
            incumbent_issue = None
            try:
                incumbent = models.predict_incumbent(manifest, rows, fixtures, root=root)
            except (ValueError, OSError) as error:
                incumbent = None
                incumbent_issue = str(error)[:250]
            if len(candidate) != len(fixtures) or incumbent is not None and len(incumbent) != len(fixtures):
                raise TrackingError('Nombre de probabilités du candidat incohérent.')
            group_pending = []
            for index,(fixture,item) in enumerate(group):
                cutoff = manifest['cutoff_exclusive']
                if (cutoff > min(current.date(),_stamp(fixture['timestamp']).date()).isoformat()
                        or manifest['trained_through'] >= cutoff):
                    raise TrackingError('Candidat appris sur le jour cible ou une période future.')
                methods = dict(candidate=_probabilities(candidate[index]),
                               clubs=_probabilities(item['probabilities']),
                               frequency=_probabilities(item['baseline']))
                if incumbent is not None:
                    methods['incumbent'] = _probabilities(incumbent[index])
                group_pending.append(dict(version=VERSION, protocol_id=protocol['id'], fixture=fixture,
                    before=cutoff, baseline_before=item.get('before',report.get('before')),
                    methods=methods, candidate_id=manifest['candidate_id'],
                    candidate_completed_at=manifest['completed_at'], trained_through=manifest['trained_through'],
                    candidate_model_sha256=manifest['model_sha256'], params=deepcopy(manifest['params']),
                    input_fingerprint=manifest.get('history_fingerprint',manifest.get('input_fingerprint')),
                    history_matches=manifest['history_matches'], incumbent_generation=manifest.get('incumbent_generation'),
                    incumbent_unavailable=incumbent_issue,
                    references=dict(scan_version=report.get('version'),
                        history_fingerprint=item.get('history_fingerprint'),
                        scan_history_fingerprint=report.get('history_fingerprint'),
                        **{key:deepcopy(item[key]) for key in ('training','history_days','home_profile','away_profile') if key in item}),
                    clubs_version='half-goals-club-ids-v2', frequency_version='league-frequency-v1'))
            pending.extend(group_pending)
        except (ValueError, OSError, KeyError, TypeError) as error:
            output['unavailable'] += len(group)
            message = str(error)[:250]
            if message not in output['issues']: output['issues'].append(message)
    if not pending:
        return dict(output, status='completed' if available else 'idle')
    if refresh is None:
        return dict(output, status='unavailable', unavailable=output['unavailable']+len(pending),
                    issues=output['issues']+['Une confirmation API récente est nécessaire avant capture.'])
    created = []
    # The batch size controls one provider call, never the number of fixtures.
    for start in range(0,len(pending),20):
        if check_time: check_time()
        batch = pending[start:start+20]
        fresh = refresh(ids='-'.join(v['fixture']['match_id'] for v in batch),timezone='UTC')
        if isinstance(fresh,dict): fresh=fresh.get('response',[])
        indexed, conflict = {},set()
        for raw in fresh:
            mid=str(raw.get('fixture',{}).get('id'))
            if mid in indexed and indexed[mid] != raw: conflict.add(mid)
            indexed[mid]=raw
        values=[]
        for item in batch:
            mid=item['fixture']['match_id']
            try:
                if mid in conflict or mid not in indexed:
                    raise TrackingError('Confirmation API absente ou contradictoire.')
                confirmation = _confirmed(item['fixture'],indexed[mid],_now(root,clock))
                values.append(dict(item,confirmation=confirmation))
            except (ValueError,KeyError,TypeError):
                output['unavailable'] += 1
        if not values:continue
        if check_time: check_time()
        with closing(_connect(root,create=True)) as con:
            con.execute('BEGIN IMMEDIATE')
            encoded_protocol=_json(protocol)
            prior=con.execute('SELECT * FROM protocols WHERE id=?',(protocol['id'],)).fetchone()
            if prior is not None and _decode(prior) != protocol:
                raise TrackingError('Les règles du protocole enregistré ont changé.')
            if prior is None:
                con.execute('INSERT INTO protocols VALUES(?,?,?)',(protocol['id'],encoded_protocol,sha256(encoded_protocol.encode()).hexdigest()))
            batch_created=[]
            for original in values:
                item=deepcopy(original);mid=item['fixture']['match_id']
                if con.execute('SELECT 1 FROM forecasts WHERE protocol_id=? AND match_id=?',(protocol['id'],mid)).fetchone():
                    output['existing']+=1
                    continue
                captured=_now(root,clock)
                if captured < protocol_created:
                    raise TrackingError('La capture précède le protocole enregistré.')
                if captured >= _stamp(item['fixture']['timestamp']):
                    output['unavailable']+=1
                    continue
                item.update(captured_at=captured.isoformat(),created_at=captured.isoformat(),
                    day=_stamp(item['fixture']['timestamp']).astimezone(ZONE).date().isoformat(),
                    id=_identity(protocol['id'],mid))
                _validate_forecast(item)
                encoded=_json(item)
                con.execute('INSERT INTO forecasts VALUES(?,?,?,?,?,?)',
                    (item['id'],protocol['id'],mid,item['captured_at'],encoded,sha256(encoded.encode()).hexdigest()))
                batch_created.append(item)
            final=_now(root,clock)
            if any(final>=_stamp(item['fixture']['timestamp']) for item in batch_created):
                raise TrackingError('Un coup d’envoi est passé pendant la capture ; le lot n’est pas enregistré.')
            con.commit()
            created.extend(batch_created)
    return dict(output,status='completed',created=len(created))


def _validate_result(value):
    if value.get('status') not in ('pending','settled','unresolved','void'):
        raise TrackingError('État de résultat quotidien invalide.')
    if value['status']=='settled':
        from league_model import outcome,score
        if outcome(score(value.get('ht')),score(value.get('ft'))) != value.get('actual'):
            raise TrackingError('Résultat quotidien incompatible avec les scores.')
    elif any(value.get(key) is not None for key in ('ht','ft','actual')):
        raise TrackingError('Un match non réglé ne peut recevoir de résultat.')
    return value


def entries(root=None, *, now=None):
    """Consistent read of immutable forecasts and own result revisions as-of now."""
    path=database_path(root)
    if not path.exists():return []
    current=_read_time(now)
    with closing(_connect(root)) as con:
        con.execute('BEGIN')
        protocols = {}
        for record in con.execute('SELECT * FROM protocols'):
            protocol = _decode(record)
            if protocol.get('id') != record['id']:
                raise TrackingError('Identité du protocole enregistrée incohérente.')
            protocols[record['id']] = protocol
        output=[]
        for row in con.execute('SELECT * FROM forecasts ORDER BY captured_at,id'):
            value=_validate_forecast(_decode(row))
            if (value['id']!=row['id'] or value['protocol_id']!=row['protocol_id']
                    or value['fixture']['match_id']!=row['match_id'] or value['captured_at']!=row['captured_at']):
                raise TrackingError('Métadonnées de prévision quotidienne altérées.')
            if (value['protocol_id'] not in protocols or
                    _stamp(value['captured_at']) < _stamp(protocols[value['protocol_id']]['created_at'])):
                raise TrackingError('Prévision antérieure à ses règles enregistrées.')
            if _stamp(value['captured_at'])>current:continue
            history=[];previous=_stamp(value['captured_at'])
            for expected,event in enumerate(con.execute('SELECT * FROM results WHERE forecast_id=? ORDER BY revision',(value['id'],)),1):
                result=_validate_result(_decode(event))
                checked=_stamp(event['checked_at'])
                if event['revision']!=expected or checked<previous:
                    raise TrackingError('Chronologie des révisions quotidienne invalide.')
                if result['status']=='settled':
                    audit=result.get('schedule_resolution') or {}
                    effective=_stamp(audit.get('effective_timestamp',value['fixture']['timestamp']))
                    original=_stamp(value['fixture']['timestamp'])
                    if (checked<effective or _stamp(value['created_at']) >= min(original,effective)
                            or value['before'] > min(original,effective).date().isoformat()
                            or value['baseline_before'] > min(original,effective).date().isoformat()):
                        raise TrackingError('Résultat enregistré avec une chronologie de match incompatible.')
                previous=checked
                if checked<=current:history.append(dict(result,revision=expected,checked_at=event['checked_at']))
            state=history[-1] if history else dict(status='pending',actual=None,ht=None,ft=None,issues=[],revision=0,checked_at=None)
            output.append(dict(value,**state,result_history=history))
        return output


def _settlement(entry,item,current):
    if item is not None:
        try:
            if str(item['league'].get('season')) != str(entry['fixture']['season']):
                raise TrackingError('La saison du relevé ne correspond pas à la prévision.')
        except (KeyError,TypeError):
            raise TrackingError('La saison du relevé est absente.') from None
    state=old_archive._settlement(entry,item,current)
    if state['status']=='pending' and current>=_stamp(entry['fixture']['timestamp'])+timedelta(hours=6):
        state=dict(status='unresolved',ht=None,ft=None,actual=None,
                   issues=['Résultat final non reçu plus de six heures après le coup d’envoi.'])
    return state


def reconcile(root=None, *, now=None, raw_fixtures=None):
    """Append changed local result observations; never call the provider."""
    current=_now(root,value=now)
    saved=entries(root,now=current)
    if not saved:return dict(entries=0,changed=0,pending=0,settled=0,unresolved=0,void=0)
    if raw_fixtures is None:
        raw_fixtures=api.load_fixtures(_root(root)/'fixtures.json',
            ids=sorted({item['fixture']['match_id'] for item in saved},key=int))
    indexed,conflicts={},set()
    for raw in raw_fixtures:
        mid=str(raw.get('fixture',{}).get('id'))
        if mid in indexed and indexed[mid]!=raw:conflicts.add(mid)
        indexed[mid]=raw
    changed=0
    with closing(_connect(root,create=True)) as con:
        con.execute('BEGIN IMMEDIATE')
        for item in saved:
            try:
                mid=item['fixture']['match_id']
                state=_settlement(item,indexed.get(mid),current)
                if mid in conflicts:raise TrackingError('Relevés API contradictoires.')
            except (ValueError,TypeError,KeyError) as error:
                state=dict(status='unresolved',ht=None,ft=None,actual=None,issues=[str(error)])
            encoded=_json(state)
            prior=con.execute('SELECT * FROM results WHERE forecast_id=? ORDER BY revision DESC LIMIT 1',(item['id'],)).fetchone()
            if prior:
                old=_validate_result(_decode(prior))
                if (_stamp(prior['checked_at'])>current or prior['payload']==encoded
                        or old['status']=='settled' and state['status']=='pending'):
                    continue
            revision=prior['revision']+1 if prior else 1
            con.execute('INSERT INTO results VALUES(?,?,?,?,?)',
                (item['id'],revision,current.isoformat(),encoded,sha256(encoded.encode()).hexdigest()))
            changed+=1
        con.commit()
    observed=entries(root,now=current)
    return dict(entries=len(observed),changed=changed,
        **{status:sum(item['status']==status for item in observed) for status in ('pending','settled','unresolved','void')})


def _counts(rows):
    counts=Counter(row['status'] for row in rows)
    return dict(matches=len(rows),**{state:counts[state] for state in ('settled','pending','unresolved','void')})


def _metrics(rows,method):
    n=len(rows)
    return dict(matches=n,correct=sum(max(VALUES,key=row['methods'][method].get)==row['actual'] for row in rows),
        accuracy=sum(max(VALUES,key=row['methods'][method].get)==row['actual'] for row in rows)/n if n else None,
        logloss=fsum(-log(row['methods'][method][row['actual']]) for row in rows)/n if n else None,
        brier=fsum(sum((row['methods'][method][label]-int(label==row['actual']))**2 for label in VALUES) for row in rows)/n if n else None)


def _comparisons(rows):
    metrics={method:_metrics(rows,method) for method in METHODS}
    comparisons={}
    for reference in ('clubs','frequency'):
        comparisons['candidate_vs_'+reference]=dict(matches=len(rows),
            delta_logloss=metrics['candidate']['logloss']-metrics[reference]['logloss'] if rows else None,
            delta_brier=metrics['candidate']['brier']-metrics[reference]['brier'] if rows else None,
            net_correct=metrics['candidate']['correct']-metrics[reference]['correct'])
    return metrics,comparisons


def summary(root=None, *, now=None):
    """Describe the daily procedure, not a retrospectively chosen best version."""
    current=_read_time(now)
    rows=entries(root,now=current)
    evaluated=[row for row in rows if row['status']=='settled']
    metrics,comparisons=_comparisons(evaluated)
    incumbent_rows=[row for row in evaluated if 'incumbent' in row['methods']]
    incumbent=dict(matches=len(incumbent_rows),metrics={name:_metrics(incumbent_rows,name) for name in ('candidate','incumbent')})
    incumbent['comparison']=dict(matches=len(incumbent_rows),
        delta_logloss=incumbent['metrics']['candidate']['logloss']-incumbent['metrics']['incumbent']['logloss'] if incumbent_rows else None,
        delta_brier=incumbent['metrics']['candidate']['brier']-incumbent['metrics']['incumbent']['brier'] if incumbent_rows else None,
        net_correct=incumbent['metrics']['candidate']['correct']-incumbent['metrics']['incumbent']['correct'])
    protocols=[]
    if database_path(root).exists():
        with closing(_connect(root)) as con:
            protocols=[_decode(row) for row in con.execute('SELECT * FROM protocols ORDER BY rowid')]
    starts={p['id']:_stamp(p['created_at']) for p in protocols}
    grouped=defaultdict(list)
    for row in rows:grouped[row['day']].append(row)
    daily=[]
    for day,values in sorted(grouped.items()):
        settled=[row for row in values if row['status']=='settled']
        m,c=_comparisons(settled)
        daily.append(dict(day=day,counts=_counts(values),evaluated=len(settled),metrics=m,comparisons=c))
    weeks=defaultdict(list)
    for row in rows:
        stamp=_stamp(row['fixture']['timestamp']);monday=stamp.date()-timedelta(days=stamp.weekday())
        weeks[monday.isoformat()].append(row)
    weekly=[]
    for week,values in sorted(weeks.items()):
        start=datetime.combine(date.fromisoformat(week),datetime.min.time(),tzinfo=timezone.utc)
        closed=start+timedelta(days=7)<=current
        boundary=any(start<starts.get(row['protocol_id'],current) for row in values)
        settled=[row for row in values if row['status']=='settled']
        admissible=closed and not boundary and all(row['status'] in ('settled','void') for row in values)
        m,c=_comparisons(settled)
        weekly.append(dict(week_utc=week,calendar_closed=closed,protocol_boundary=boundary,
            admissible=admissible,matches=len(values),evaluated=len(settled),counts=_counts(values),metrics=m,comparisons=c))
    thresholds=[]
    for threshold in (.5,.6,.7):
        selected=[row for row in evaluated if max(row['methods']['candidate'].values())>=threshold]
        m,c=_comparisons(selected)
        thresholds.append(dict(threshold=threshold,matches=len(selected),
            coverage=len(selected)/len(evaluated) if evaluated else None,
            mean_confidence=fsum(max(row['methods']['candidate'].values()) for row in selected)/len(selected) if selected else None,
            metrics=m,comparisons=c))
    calibration=[]
    per_class=[]
    for method in METHODS:
        for label in VALUES:
            chosen=[row for row in evaluated if max(VALUES,key=row['methods'][method].get)==label]
            per_class.append(dict(method=method,label=label,selected=len(chosen),
                correct=sum(row['actual']==label for row in chosen),
                accuracy=sum(row['actual']==label for row in chosen)/len(chosen) if chosen else None))
            for index in range(10):
                chosen=[row for row in evaluated if min(9,int(row['methods'][method][label]*10))==index]
                calibration.append(dict(method=method,label=label,lower=index/10,upper=(index+1)/10,matches=len(chosen),
                    announced=fsum(row['methods'][method][label] for row in chosen)/len(chosen) if chosen else None,
                    observed=sum(row['actual']==label for row in chosen)/len(chosen) if chosen else None))
    return dict(version=VERSION,as_of=current.isoformat(),protocols=protocols,counts=_counts(rows),
        evaluated=len(evaluated),metrics=metrics,comparisons=comparisons,incumbent=incumbent,
        daily=daily,weekly=weekly,closed_weeks=sum(row['admissible'] for row in weekly),
        thresholds=thresholds,calibration=calibration,per_class=per_class,rows=rows,
        promotion=False,interpretation='One first pre-match capture per fixture and procedure protocol. '
            'Daily generations describe the fixed daily-training policy; no best-generation selection. '
            'Paired descriptive evidence only; repeated clubs/weeks remain dependent. '
            'Missing, postponed, unresolved and void observations are never prediction losses.')
