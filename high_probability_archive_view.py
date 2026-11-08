"""Pure merging of full ranking archives with genuine older saved marginals."""
from copy import deepcopy
from datetime import datetime, timezone

import high_probability_history as group_history
import probability_reliability as reliability


VERSION = 'probability-ranking-history-v1'


def _legacy(entries, intents, now):
    rows = group_history.build(entries, intents, now)['rows']
    values = {}
    for row in rows:
        entry = row['entry']
        try:
            positions = reliability._positions(entry['methods']['clubs'], entry['fixtures'])
        except (ValueError, KeyError, TypeError):
            continue
        for fixture, position, result in zip(entry['fixtures'], positions, row['match_rows']):
            identifier = f"journal:{entry['id']}:{fixture['match_id']}"
            if identifier in values:
                continue
            values[identifier] = dict(id=identifier, source='API-Football', source_kind='saved_group',
                day=row['day'].isoformat(), fixture=deepcopy(fixture),
                created_at=entry['created_at'], before=entry['methods']['clubs'].get('training_before'),
                probabilities=deepcopy(position['probabilities']), model_version=position['version'],
                status={'evaluated': 'settled', 'pending': 'pending', 'unresolved': 'unresolved',
                        'void': 'void'}[result['state']],
                ht=result['ht'], ft=result['ft'], actual=result['actual'],
                issues=deepcopy(entry.get('issues', [])), checked_at=entry.get('checked_at'),
                result_history=deepcopy(entry.get('result_history', [])),
                competition=result['competition'])
    return list(values.values())


def build(archived, entries=(), intents=(), now=None):
    """Choose the last saved pre-match snapshot, before applying UI filters.

    Legacy records prove only the marginals actually saved with a group. They
    never stand for the entire ranking of a past day.
    """
    current = datetime.now(timezone.utc) if now is None else now
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError('Une horloge avec fuseau horaire est nécessaire.')
    values, excluded = [], 0
    legacy_unavailable = False
    try:
        legacy = _legacy(entries, intents, current)
    except (ValueError, KeyError, TypeError, AttributeError):
        legacy, legacy_unavailable = [], True
    sources = [dict(row, source_kind='ranking_archive') for row in archived] + legacy
    for source in sources:
        try:
            fixture = source['fixture']
            created, kickoff = reliability._stamp(source['created_at']), reliability._stamp(fixture['timestamp'])
            day = kickoff.astimezone(group_history.ZONE).date()
            if created > current or created >= kickoff or fixture.get('provider') != 'api-football':
                raise ValueError('Prévision hors période avant match.')
            if kickoff > current and (source.get('status') == 'settled' or
                    any(source.get(field) is not None for field in ('actual', 'ht', 'ft'))):
                raise ValueError('Résultat enregistré avant le coup d’envoi réel.')
            probabilities = reliability._probabilities(source['probabilities'])
            value = deepcopy(source)
            value.update(day=day.isoformat(), probabilities=probabilities,
                match_id=str(fixture['match_id']), fixture=deepcopy(fixture),
                favorite=max(group_history.VALUES, key=probabilities.get),
                competition=source.get('competition') or ' · '.join(str(part) for part in
                    (fixture.get('country'), fixture.get('competition') or fixture['league']) if part))
            values.append(value)
        except (ValueError, KeyError, TypeError, AttributeError):
            excluded += 1
    versions = {}
    for row in sorted(values, key=lambda item: (item['source_kind'] == 'ranking_archive',
                                               reliability._stamp(item['created_at']), item['id']), reverse=True):
        versions.setdefault((row['day'], row['match_id']), []).append(row)
    latest = [dict(rows[0], snapshots=rows) for rows in versions.values()]
    latest.sort(key=lambda row: (row['day'], reliability._stamp(row['fixture']['timestamp']), row['match_id']), reverse=True)
    return dict(rows=latest, snapshots=len(values), excluded=excluded, legacy_unavailable=legacy_unavailable,
                archived_matches=sum(row['source_kind'] == 'ranking_archive' for row in latest),
                legacy_matches=sum(row['source_kind'] == 'saved_group' for row in latest))
