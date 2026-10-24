"""Read-only local observations, without reconstructing unobserved uptime.

The fixture query reads indexed date/status projections, never raw provider
payloads. Frozen forecasts and their latest imported results are read through a
read-only SQLite connection. No API, training, reconciliation or registration
is performed. Separate databases are separate snapshots, not one transaction.
"""
from collections import Counter
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from statistics import median
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import automation_health as health
import fixture_store
import journal_protocol
import probability_reliability as reliability
import saved_model_comparison as comparison


VERSION = 'local-operation-observations-v1'
DEFAULT_TIMEZONE = 'Africa/Kinshasa'
REFRESH_PHASES = ('calendar','results','forecasts','reconcile')
REFRESH_EVENT_LIMIT = 100000
LIMITATIONS = [
    'Les passages enregistrés ne démontrent pas une disponibilité continue du Mac ou du service.',
    'Le dernier signal d’un cycle ne reconstitue pas ses signaux passés ; un signal ancien ne prouve pas un arrêt.',
    'Les intervalles entre opérations terminées décrivent les événements enregistrés ; ils ne prouvent ni une panne ni une prévision manquée.',
    'Le calendrier conserve son dernier relevé par date, sans date de collecte individuelle de chaque score.',
    'Les résultats sont ceux importés au moment du rapport ; une attente ou une exclusion n’est pas un échec prédictif.',
    'Les journées closes du calendrier ne sont pas des journées de fonctionnement entièrement vérifiées.',
    'Les bases sont lues séparément ; une collecte simultanée peut les faire évoluer entre deux lectures.',
    'Les comparatifs prospectifs conservent leurs cohortes et critères ; les effectifs ne sont pas additionnables entre cohortes.',
]


class ObservationError(ValueError):
    pass


def _stamp(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
            # Forecast fixture timestamps are stored in naive UTC by design.
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
        except ValueError:
            raise ObservationError('Date d’observation invalide.') from None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ObservationError('Date d’observation invalide.')
    return value.astimezone(timezone.utc)


def _window(now, days, timezone_name):
    if type(days) is not int or not 1 <= days <= 365:
        raise ObservationError('La fenêtre doit contenir entre 1 et 365 journées.')
    try:
        zone = ZoneInfo(timezone_name)
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        raise ObservationError('Fuseau d’observation invalide.') from None
    now = _stamp(now)
    first = now.astimezone(zone).date() - timedelta(days=days-1)
    start = datetime.combine(first, datetime.min.time(), zone)
    return now, zone, start


def _journal(path):
    """Validate frozen digests and read only the latest result revision."""
    path = Path(path)
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=2)) as con:
        con.execute('PRAGMA query_only=ON')
        con.execute('BEGIN')
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'predictions' not in tables:
            if tables:
                raise ObservationError('Schéma du journal local invalide.')
            return []
        output = []
        # Read all frozen forecasts for a genuine global deduplication and the
        # registered comparison. The large fixture payload store is not read.
        query = '''SELECT p.id,p.created_at,p.payload,p.digest,r.checked_at,r.payload
            FROM predictions p LEFT JOIN results r ON r.prediction_id=p.id
            AND r.revision=(SELECT max(revision) FROM results WHERE prediction_id=p.id)
            ORDER BY p.created_at,p.id'''
        for identity, created, payload, digest, checked, result in con.execute(query):
            if sha256(payload.encode()).hexdigest() != digest:
                raise ObservationError('Intégrité du journal local invalide.')
            item = json.loads(payload)
            latest = json.loads(result) if result is not None else dict(status='pending', observed=[])
            if (not isinstance(identity,str) or not identity or not isinstance(item,dict)
                    or not isinstance(item.get('fixtures'),list) or not isinstance(latest,dict)):
                raise ObservationError('Contenu du journal local invalide.')
            _stamp(created)
            if checked is not None:
                _stamp(checked)
            output.append(dict(item, id=identity, created_at=created, **latest,
                               **({'checked_at':checked} if checked is not None else {})))
        return output


def _fixtures(path, start, end):
    """Read a compact date projection within one fixture-store snapshot."""
    path = fixture_store.database_path(path)
    if not path.exists():
        return dict(state='missing', rows=[], calendar_updates={})
    # Reuse the fixture store's validated read-only transaction and projection
    # authority; never fall back to parsing the retained 123 MB source JSON.
    with fixture_store._connection(path) as con:
        meta = fixture_store._meta(con)
        bounds = [stamp.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds')
                  for stamp in (start,end)]
        rows = [dict(row) for row in con.execute('''SELECT match_id,kickoff_utc,status,
            normalized_json IS NOT NULL AS usable FROM fixtures
            WHERE kickoff_utc>=? AND kickoff_utc<? ORDER BY kickoff_utc,match_id''', bounds)]
        updates = {(row[0],row[1]):row[2] for row in con.execute(
            'SELECT day,timezone,updated_at FROM calendar_updates')}
    return dict(state='available', rows=rows, calendar_updates=updates,
                revision=int(meta['revision']), updated_at=meta.get('updated_at') or None)


def _day_service(path):
    """Read only controlled checkpoint fields, never copy arbitrary settings."""
    path = Path(path)
    if not path.exists():
        return dict(state='missing', timestamps={})
    payload = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(payload,dict):
        raise ObservationError('Dates du suivi journalier invalides.')
    value = payload.get('day_service',{})
    if not isinstance(value,dict):
        raise ObservationError('Dates du suivi journalier invalides.')
    if not value:
        return dict(state='not_yet_recorded',timestamps={})
    day = date.fromisoformat(value['day']).isoformat()
    stamps = {field:_stamp(value[field]).isoformat()
              for field in ('calendar_at','results_at','forecasts_at','reconciled_at') if field in value}
    return dict(state='available',day=day,timestamps=stamps)


def _refresh_events(root, now, start):
    """Read at most 31 days/100k successful-operation observations, no schema writes."""
    path = health.path(root)
    # Stored event timestamps are UTC ISO strings. Normalize SQL bounds too;
    # lexical comparison against a local +01:00 bound would lose midnight rows.
    start = max(start,now-timedelta(days=31)).astimezone(timezone.utc)
    empty = dict(rows=[],truncated=False,window_start=start.isoformat(),limit=REFRESH_EVENT_LIMIT)
    if not path.exists():
        return dict(empty,state='missing')
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as con:
        con.execute('PRAGMA query_only=ON')
        con.execute('BEGIN')
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'refresh_events' not in tables:
            return dict(empty,state='not_yet_recorded')
        values = con.execute('''SELECT id,observed_at,cycle_started_at,day,phase,counters
            FROM refresh_events WHERE observed_at>=? AND observed_at<=?
            ORDER BY observed_at DESC,id DESC LIMIT ?''',
            (start.isoformat(),now.isoformat(),REFRESH_EVENT_LIMIT+1)).fetchall()
    rows = []
    for identity,observed,cycle,day,phase,counters in values[:REFRESH_EVENT_LIMIT]:
        if (not isinstance(identity,str) or len(identity)!=32
                or any(character not in '0123456789abcdef' for character in identity)
                or phase not in REFRESH_PHASES or _stamp(cycle)>_stamp(observed)):
            raise ObservationError('Événements de suivi journalier invalides.')
        day = date.fromisoformat(day).isoformat()
        counts = json.loads(counters)
        if not isinstance(counts,dict) or any(key not in health.COUNTERS or type(value) is not int or value<0
                                              for key,value in counts.items()):
            raise ObservationError('Compteurs de suivi journalier invalides.')
        rows.append(dict(id=identity,observed_at=_stamp(observed).isoformat(),cycle_started_at=_stamp(cycle).isoformat(),
                         day=day,phase=phase,counters=counts))
    return dict(empty,state='available',rows=rows,truncated=len(values)>REFRESH_EVENT_LIMIT)


def _refresh_summary(view, now, zone):
    """Successive completion intervals describe observation density, not uptime."""
    output, unique, duplicate = {}, {}, 0
    for row in view.get('rows',[]):
        if row['id'] in unique:
            duplicate += 1
        elif _stamp(row['observed_at'])<=now:
            unique[row['id']] = row
    for phase in REFRESH_PHASES:
        values = sorted((_stamp(row['observed_at']) for row in unique.values() if row['phase']==phase))
        intervals = [(following,(following-previous).total_seconds()) for previous,following in zip(values,values[1:])]
        output[phase] = dict(count=len(values),first_at=values[0].isoformat() if values else None,
            last_at=values[-1].isoformat() if values else None,
            age_since_last_seconds=(now-values[-1]).total_seconds() if values else None,
            longest_observed_interval_seconds=max((gap for _,gap in intervals),default=None),
            daily={day:dict(count=sum(stamp.astimezone(zone).date().isoformat()==day for stamp in values),
                first_at=min(stamp for stamp in values if stamp.astimezone(zone).date().isoformat()==day).isoformat(),
                last_at=max(stamp for stamp in values if stamp.astimezone(zone).date().isoformat()==day).isoformat(),
                longest_observed_interval_seconds=max((gap for stamp,gap in intervals
                    if stamp.astimezone(zone).date().isoformat()==day),default=None))
                for day in sorted({stamp.astimezone(zone).date().isoformat() for stamp in values})})
    return output,duplicate


def _journal_observations(entries, now, zone):
    unique_entries, repeated, future = {}, 0, 0
    for entry in sorted(entries, key=lambda item:(str(item.get('created_at','')),str(item.get('id','')))):
        if _stamp(entry['created_at']) > now:
            future += 1
            continue
        identity = entry['id']
        if identity in unique_entries:
            repeated += 1
        else:
            unique_entries[identity] = entry
    matches, appearances, invalid = {}, 0, 0
    for entry in sorted(unique_entries.values(),key=lambda item:(_stamp(item['created_at']),item['id'])):
        created = _stamp(entry['created_at'])
        local_seen = set()
        for fixture in entry.get('fixtures',[]):
            try:
                if entry.get('source') != 'API-Football' or fixture.get('provider') != 'api-football':
                    raise ValueError()
                mid = reliability._id(fixture['match_id'])
                kickoff = _stamp(fixture['timestamp'])
                if mid in local_seen:
                    raise ValueError()
                local_seen.add(mid)
                appearances += 1
                if mid in matches:
                    continue
                result = reliability._result(entry,dict(fixture,match_id=mid))
                if entry.get('checked_at') and _stamp(entry['checked_at']) > now:
                    result = dict(state='excluded',reason='observation_after_report')
                matches[mid] = dict(match_id=mid, day=kickoff.astimezone(zone).date().isoformat(),
                    entry_id=entry['id'], created_at=created.isoformat(), kickoff_utc=kickoff.isoformat(),
                    before_kickoff=created<kickoff, lead_seconds=(kickoff-created).total_seconds(), **result)
            except (ValueError, TypeError, KeyError, AttributeError):
                invalid += 1
    return dict(entries=len(unique_entries), duplicate_entry_inputs=repeated, future_entry_inputs=future,
        unique_matches=len(matches), match_appearances=appearances,
        repeated_match_appearances=appearances-len(matches), invalid_fixture_inputs=invalid,
        group_states=dict(Counter(entry.get('status','unknown') for entry in unique_entries.values())),
        matches=list(matches.values()))


def build(*, now, days=7, timezone_name=DEFAULT_TIMEZONE, health_view=None,
          entries=(), fixture_snapshot=None, evidence=None, component_states=None, day_service=None,
          refresh_events=None):
    """Pure report construction: inputs are not mutated and no I/O occurs."""
    now, zone, start = _window(now,days,timezone_name)
    view = health_view or dict(rows=[],total=0,first_started_at=None,truncated=False)
    snapshots = fixture_snapshot or dict(state='missing',rows=[],calendar_updates={})
    checkpoints = day_service or dict(state='missing',timestamps={})
    refresh_view = refresh_events or dict(state='missing',rows=[],truncated=False,window_start=None)
    refreshes, duplicate_events = _refresh_summary(refresh_view,now,zone)
    journal = _journal_observations(list(entries),now,zone)
    cycles = view['rows']
    first_seen = _stamp(view['first_started_at']) if view.get('first_started_at') else None
    daily = []
    for offset in range(days):
        begin = start+timedelta(days=offset)
        finish = begin+timedelta(days=1)
        day = begin.date().isoformat()
        observed = [row for row in cycles if begin<=_stamp(row['started_at'])<finish]
        matched = [row for row in journal['matches'] if row['day']==day]
        projection = [row for row in snapshots['rows']
                      if begin<=_stamp(row['kickoff_utc'])<finish]
        refresh = snapshots.get('calendar_updates',{}).get((day,timezone_name))
        refresh_time = _stamp(refresh) if refresh else None
        leads = [row['lead_seconds'] for row in matched if row['before_kickoff']]
        latest = {field:stamp for field,stamp in checkpoints.get('timestamps',{}).items()
                  if checkpoints.get('day')==day and _stamp(stamp).astimezone(zone).date().isoformat()==day
                  and _stamp(stamp)<=now}
        daily.append(dict(day=day, closed_calendar_day=finish<=now,
            monitoring_began_before_day=first_seen is not None and first_seen<=begin,
            cycle_records_truncated=bool(view.get('truncated')),
            refresh_event_window_covers_day=bool(refresh_view.get('state')=='available' and refresh_view.get('window_start')
                and _stamp(refresh_view['window_start'])<=begin and finish<=now
                and not refresh_view.get('truncated')),
            cycles_started=len(observed), cycle_states=dict(Counter(row['status'] for row in observed)),
            calendar_matches=len(projection), calendar_states=dict(Counter(row['status'] for row in projection)),
            calendar_usable_results=sum(bool(row['usable']) for row in projection),
            calendar_last_refresh=refresh,
            calendar_refresh_age_seconds=max(0,(now-refresh_time).total_seconds())
                if refresh_time is not None and refresh_time<=now else None,
            latest_day_service_checkpoints=latest,
            latest_day_service_age_seconds={field:(now-_stamp(stamp)).total_seconds() for field,stamp in latest.items()},
            completed_refreshes={phase:refreshes[phase]['daily'].get(day,dict(count=0,first_at=None,last_at=None,
                longest_observed_interval_seconds=None)) for phase in REFRESH_PHASES},
            forecast_unique_matches=len(matched), forecasts_before_kickoff=sum(row['before_kickoff'] for row in matched),
            forecasts_not_before_kickoff=sum(not row['before_kickoff'] for row in matched),
            forecast_lead_seconds_min=min(leads,default=None), forecast_lead_seconds_median=median(leads) if leads else None,
            result_states=dict(Counter(row['state'] for row in matched)),
            result_exclusions=dict(Counter(row['reason'] for row in matched if row.get('state')=='excluded'))))
    running = [dict(id=row['id'], started_at=row['started_at'], phase=row.get('phase'),
                    last_seen_at=row.get('last_seen_at'),last_progress_at=row.get('last_progress_at'),
                    signal_state=row.get('signal_state','unavailable'),counters=dict(row.get('counters',{})))
               for row in cycles if row['status']=='running']
    evidence_summary = dict(state='unavailable')
    if evidence is not None:
        evidence_summary = dict(state='available',version=evidence['version'],registered_at=evidence['registered_at'],
            coverage=dict(evidence['coverage']),snapshot_exclusions=dict(evidence.get('snapshot_exclusions',{})),
            cohorts=[{key:row[key] for key in ('challenger','league','size','versions','forecasts','evaluated',
                'pending','excluded','result_exclusions','dates','scores','uncertainty')} for row in evidence['cohorts']])
    return dict(version=VERSION,as_of=now.isoformat(),timezone=timezone_name,
        window_start=start.astimezone(timezone.utc).isoformat(),days=days,
        component_states=dict(component_states or {}),
        automation=dict(total_recorded_cycles=view.get('total',0),first_recorded_start=view.get('first_started_at'),
                        returned_cycles=len(cycles),truncated=bool(view.get('truncated')),running=running,
                        day_service_state=checkpoints['state'],
                        day_service_scope='Derniers passages connus uniquement, sans historique des rafraîchissements.'),
        refresh_observations=dict(state=refresh_view['state'],window_start=refresh_view.get('window_start'),
            limit=refresh_view.get('limit',REFRESH_EVENT_LIMIT),truncated=bool(refresh_view.get('truncated')),
            duplicate_input_events=duplicate_events,
            phases={phase:{key:value for key,value in item.items() if key!='daily'} for phase,item in refreshes.items()}),
        fixture_store={key:snapshots.get(key) for key in ('state','revision','updated_at')},
        journal={key:value for key,value in journal.items() if key!='matches'},daily=daily,
        predictive_evidence=evidence_summary,limitations=list(LIMITATIONS))


def collect(root, *, now=None, days=7, timezone_name=DEFAULT_TIMEZONE, include_evidence=True):
    """Inspect a local data directory; missing/corrupt components stay explicit."""
    now, zone, start = _window(now or datetime.now(timezone.utc),days,timezone_name)
    root = Path(root)
    states = {}
    def read(name, action, default):
        try:
            result = action()
            states[name] = 'available'
            return result
        except (ValueError,OSError,sqlite3.Error,KeyError,TypeError,AttributeError,OverflowError):
            states[name] = 'unavailable_or_invalid'
            return default
    view = read('automation',lambda:health.load(root,now=now,days=days,limit=1000),None)
    if not health.path(root).exists():
        states['automation'] = 'missing'
    entries = read('journal',lambda:_journal(root/'predictions.sqlite3'),[])
    if not (root/'predictions.sqlite3').exists():
        states['journal'] = 'missing'
    fixtures = read('fixtures',lambda:_fixtures(root/'fixtures.sqlite3',start,start+timedelta(days=days)),None)
    if fixtures is not None:
        states['fixtures'] = fixtures['state']
    else:
        fixtures = dict(state=states['fixtures'],rows=[],calendar_updates={})
    checkpoints = read('day_service',lambda:_day_service(root/'automation_state.json'),None)
    if checkpoints is not None:
        states['day_service'] = checkpoints['state']
    refreshes = read('refresh_events',lambda:_refresh_events(root,now,start),None)
    if refreshes is not None:
        states['refresh_events'] = refreshes['state']
    else:
        refreshes = dict(state=states['refresh_events'],rows=[],truncated=False,window_start=None)
    evidence = None
    if include_evidence:
        protocol = read('protocol',lambda:journal_protocol.load_protocol(root/'prospective_protocol.json'),None)
        if protocol is None and states['protocol']=='available':
            states['protocol'] = 'missing'
        if protocol is not None and states['journal']=='available':
            evidence = read('evidence',lambda:comparison.build_pairs(entries,protocol,now=now),None)
    else:
        states['evidence'] = 'not_requested'
    return build(now=now,days=days,timezone_name=timezone_name,health_view=view,entries=entries,
                 fixture_snapshot=fixtures,evidence=evidence,component_states=states,day_service=checkpoints,
                 refresh_events=refreshes)
