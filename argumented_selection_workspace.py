"""Local selection snapshots shared by the read-only UI and provider worker."""
from copy import deepcopy
from collections import Counter
from datetime import datetime, date, time, timezone
from functools import lru_cache
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import api_football as api
import api_match_workspace as calendar
import cloud_storage
import high_probability_workspace as probabilities


ZONE=ZoneInfo('Africa/Kinshasa')


def _root(root=None):
    return Path(api.DATA_DIR if root is None else root).resolve()


def _now(value=None):
    value=datetime.now(timezone.utc) if value is None else value
    if not isinstance(value,datetime) or value.tzinfo is None:
        raise ValueError('Une heure actuelle avec fuseau est nécessaire.')
    return value.astimezone(timezone.utc)


def is_enabled(root=None):
    path=_root(root)/'argumented_selection/enabled.json'
    if not path.exists():return False
    value=json.loads(path.read_text(encoding='utf-8'))
    return value.get('enabled') is True


def enable(root=None, *, enabled=True):
    if type(enabled) is not bool:raise ValueError('Activation booléenne requise.')
    if cloud_storage.configured():raise ValueError('Le suivi utilise le stockage local.')
    cloud_storage._assert_writable()
    import automation_worker
    automation_worker._write(_root(root)/'argumented_selection/enabled.json',dict(enabled=enabled))


@lru_cache(maxsize=4)
def _build(root, signature, day, period_start):
    import argumented_selection_engine as engine
    current=datetime.fromisoformat(period_start)
    path=Path(root)/'fixtures.json'
    base=probabilities.load_scan(root=root,now=current,day=date.fromisoformat(day))
    codes={row['fixture']['league'] for row in base['rows']}
    history,_=api.load_matches(path,leagues=codes,end=base['before'],statuses=api.FINISHED_STATUSES) if codes else ([],{})
    if api.data_signature(path)!=signature:
        raise ValueError('Les données ont changé pendant la sélection ; nouvelle lecture nécessaire.')
    report=engine.build(base,history,now=current)
    report['calendar_updated_at']=base.get('calendar_updated_at')
    return report


def load_report(root=None, *, now=None, day=None):
    """Indexed/cached reads only; no forecast, database, or API mutation."""
    root=_root(root);current=_now(now)
    target=current.astimezone(ZONE).date() if day is None else day
    if type(target) is not date:raise ValueError('Date de calendrier requise.')
    period_start=max(datetime.combine(current.date(),time.min,timezone.utc),
        datetime.combine(current.astimezone(ZONE).date(),time.min,ZONE).astimezone(timezone.utc))
    for attempt in range(2):
        signature=api.data_signature(root/'fixtures.json')
        try:
            report=deepcopy(_build(str(root),signature,target.isoformat(),period_start.isoformat()))
            break
        except ValueError:
            if attempt or signature==api.data_signature(root/'fixtures.json'):raise
    rows=[]
    for row in report['rows']:
        stamp=datetime.fromisoformat(row['fixture']['timestamp'].replace('Z','+00:00'))
        stamp=stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
        if stamp>current:
            rows.append(row)
        else:
            report['excluded'].append(dict(match_id=row['fixture']['match_id'],reason='kickoff_elapsed',
                detail='Le coup d’envoi est passé ; aucune nouvelle sélection avant match.',fixture=row['fixture']))
    report['rows']=rows
    # This timestamp describes this newly assembled report, including the live
    # expiry check, rather than pretending it was produced at the cache period's
    # midnight. Production mutations still enforce their own real clock.
    report['generated_at']=current.isoformat()
    report['checked_at']=current.isoformat()
    report['qualified_count']=len(rows)
    report['diagnostics']['selected_count']=len(rows)
    report['diagnostics']['excluded_counts']=dict(Counter(row['reason'] for row in report['excluded']))
    return report


def capture_cycle(root=None, *, clock=None, refresh=None, check_time=None):
    """Called by the existing API worker with its quota-aware confirmation."""
    import argumented_selection_store as store
    root=_root(root)
    if cloud_storage.configured() or not is_enabled(root):return dict(status='disabled',created=0)
    if check_time:check_time()
    current=_now(clock() if clock else None)
    report=load_report(root,now=current)
    return store.capture(report,root=root,clock=clock,refresh=refresh,check_time=check_time)


@cloud_storage.persisted
def capture_now(root=None, *, day=None):
    """Explicit UI action: refresh the chosen day, confirm NS, then freeze all."""
    import argumented_selection_store as store
    root=_root(root)
    if cloud_storage.configured():raise ValueError('Le suivi utilise le stockage local.')
    current=_now();target=current.astimezone(ZONE).date() if day is None else day
    if type(target) is not date or target<current.astimezone(ZONE).date():
        raise ValueError('Les journées passées sont consultables dans l’historique.')
    client=api.ApiFootballClient(api.load_key())
    raw=calendar._response_fixtures(client.fixtures(date=target.isoformat(),timezone=ZONE.key))
    if any(calendar._stamp(row).astimezone(ZONE).date()!=target for row in raw):
        raise ValueError('Le calendrier contient une rencontre hors de la date sélectionnée.')
    api.save_fixtures(raw,root/'fixtures.json')
    calendar._mark_calendar_refresh(root/'fixtures.json',target,ZONE.key)
    report=load_report(root,now=_now(),day=target)
    result=store.capture(report,root=root,
        refresh=lambda **params:calendar._response_fixtures(client.fixtures(**params)))
    result['results']=store.reconcile(root=root)
    return result
