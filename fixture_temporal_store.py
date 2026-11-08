"""Explicit historical fixture reads from retained import versions.

This adapter never consults today's normalized scores as a historical fallback.
An import-recorded timestamp is not provider publication time, nor a proof of
database commit time. These reads are qualified retrospective reconstructions.
"""
from hashlib import sha256
from datetime import date, datetime, time, timezone
import json
from pathlib import Path

import fixture_temporal as temporal
import fixture_store as store


VERSION = 'fixture-history-as-of-v1'


def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',', ':'),allow_nan=False)


def _bound(value):
    if value is None:
        return None
    if isinstance(value,str) and len(value)==10:
        return datetime.combine(date.fromisoformat(value),time.min,timezone.utc)
    return temporal.utc_instant(value)


def _indexed_id(item):
    if not isinstance(item,dict):
        return None
    record=item.get('record')
    return item.get('match_id') if 'record' not in item or item.get('match_id') is not None else (
        record.get('match_id') if isinstance(record,dict) else None)


def _read(path, ids):
    import api_football as api
    database = api.active_store(path)
    if database is None:
        payload = api._load_store(path)
        inventory = {str(row['fixture']['id']) for row in payload['fixtures']}
        records = payload.get('import_revisions', [])
        if ids is not None:
            records = [row for row in records if not isinstance(row,dict) or str(row.get('match_id')) in ids]
        return records, inventory if ids is None else inventory & set(ids), dict(kind='json')
    with store._connection(database) as connection:
        meta = store._meta(connection)
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        records = []
        chunks = [None] if ids is None else [ids[index:index+500] for index in range(0,len(ids),500)]
        for chunk in chunks:
            if 'fixture_import_revisions' not in tables:
                break
            condition = '' if chunk is None else ' WHERE match_id IN (' + ','.join('?' for _ in chunk) + ')'
            for row in connection.execute('SELECT sequence,match_id,observed_at_utc,payload,digest '
                    'FROM fixture_import_revisions' + condition + ' ORDER BY sequence', [] if chunk is None else chunk):
                try:
                    record = json.loads(row['payload'])
                    if not isinstance(record,dict):
                        raise ValueError('Non-object event')
                except (ValueError,TypeError):
                    # A corrupt event has no admissible version. Keep its known
                    # index identity so other fixtures can still be inspected.
                    record = dict(match_id=row['match_id'],observed_at_utc=row['observed_at_utc'])
                records.append(dict(record=record,match_id=row['match_id'],observed_at_utc=row['observed_at_utc'],
                                    digest=row['digest'],sequence=row['sequence']))
        if ids is None:
            inventory = {row[0] for row in connection.execute('SELECT match_id FROM fixtures')}
        else:
            inventory = set()
            for chunk in chunks:
                inventory.update(row[0] for row in connection.execute('SELECT match_id FROM fixtures WHERE match_id IN ('
                    + ','.join('?' for _ in chunk) + ')',chunk))
        return records,inventory,dict(kind='sqlite',revision=int(meta['revision']),store_uuid=meta['store_uuid'])


def state_as_of(path, at, *, ids=None):
    """Read target metadata and history from one coherent observation inventory.

    The selected payloads include NS fixtures. This does not normalize a target
    outcome and never claims the local import time is provider publication.
    """
    import api_football as api
    at=temporal.utc_instant(at)
    if ids is not None:
        ids=list(dict.fromkeys(api._numeric_id(mid,'du match') for mid in ids))
    records,inventory,origin=_read(Path(path),ids)
    reconstructed=temporal.reconstruct(records,at=at)
    tracked={str(_indexed_id(item)) for item in records if _indexed_id(item) is not None}
    untracked=inventory-tracked
    exclusions=list(reconstructed['excluded'])
    if ids is not None:
        exclusions.extend(dict(match_id=mid,reason='untracked_availability') for mid in sorted(untracked,key=int))
    return dict(version=VERSION,as_of_utc=at.isoformat(),selected=reconstructed['selected'],
                temporal_exclusions=exclusions,untracked_inventory_count=len(untracked),
                source_store=origin,temporal_quality='retrospective_conditional',
                time_basis='local_import_recorded',provider_publication_time_proven=False,
                database_visibility_at_cutoff_proven=False,prospective_confirmation=False)


def history_as_of(path, at, **filters):
    """Return normal training rows plus a qualified, source-referenced audit.

    ``at`` is exclusive and must include its timezone. Fixture/league/date/status
    filters apply AFTER reconstruction, so a later schedule or score cannot
    remove or alter its earlier version. SQL retrieval only prefilters exact IDs.
    Unknown old values remain excluded; no migration or API call is performed.
    """
    import api_football as api
    at = temporal.utc_instant(at)
    allowed = {'ids','leagues','statuses','start','end'}
    if set(filters)-allowed:
        raise ValueError('Filtres historiques inconnus.')
    if filters.get('ids') is not None:
        filters['ids'] = list(dict.fromkeys(api._numeric_id(mid,'du match') for mid in filters['ids']))
    start,end=_bound(filters.pop('start',None)),_bound(filters.pop('end',None))
    if start is not None and end is not None and start>end:
        raise ValueError('La borne initiale doit précéder la borne finale.')
    state=state_as_of(path,at,ids=filters.get('ids'))
    selected = {item['match_id']:item for item in state['selected']}
    raw = api._select_fixtures([item['raw'] for item in selected.values()],**filters)
    if start is not None or end is not None:
        bounded=[]
        for item in raw:
            try:
                kickoff=temporal.utc_instant(item['fixture']['date'])
                if (start is None or kickoff>=start) and (end is None or kickoff<end):
                    bounded.append(item)
            except (ValueError,TypeError,KeyError):
                # Preserve invalid/quarantined records for the normal quality
                # audit; do not turn a missing score into an absent fixture.
                bounded.append(item)
        raw=bounded
    rows,audit = api._normalize_matches(raw)
    kept,refs = [],[]
    for row in rows:
        kickoff=temporal.utc_instant(row['timestamp']+'+00:00')
        if kickoff >= at:
            audit['issues'].append(dict(match_id=row['match_id'],reason='Résultat final annoncé avant le coup d’envoi à la coupure choisie.'))
            continue
        source = selected[row['match_id']]
        if temporal.utc_instant(source['observed_at_utc'])<kickoff:
            audit['issues'].append(dict(match_id=row['match_id'],reason='Résultat final enregistré avant son coup d’envoi annoncé.'))
            continue
        kept.append(row)
        refs.append(dict(match_id=row['match_id'],observation_id=source['observation_id'],
                         observed_at_utc=source['observed_at_utc'],time_kind=source['time_kind'],
                         normalized_sha256=sha256(_json(row).encode()).hexdigest()))
    refs.sort(key=lambda item:int(item['match_id']))
    audit.update(version=VERSION,as_of_utc=at.isoformat(),matched=len(kept),
                 temporal_quality='retrospective_conditional',time_basis='local_import_recorded',
                 provider_publication_time_proven=False,database_visibility_at_cutoff_proven=False,
                 prospective_confirmation=False,source_store=state['source_store'],source_refs=refs,
                 source_fingerprint=sha256(_json(refs).encode()).hexdigest(),
                 temporal_exclusions=state['temporal_exclusions'],untracked_inventory_count=state['untracked_inventory_count'],
                 inventory_scope='current stored fixture IDs, restricted only by explicit ids; not a historical eligibility cohort')
    return kept,audit
