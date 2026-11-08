"""Pure reconstruction of locally observed fixture versions strictly before T.

The event time of a match is not its local availability time. An old baseline
recovered by the first tracked import becomes usable only at that import's
recorded time. Later observations never repair an earlier missing version.
"""
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone

import fixture_import_policy as policy


VERSION = 'fixture-temporal-v1'


def utc_instant(value):
    """Accept an aware datetime or ISO instant, without guessing a timezone."""
    try:
        instant = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
        if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError
        return instant.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise ValueError('Un instant avec fuseau horaire explicite est nécessaire.') from None


def _identifier(value):
    if (isinstance(value, bool) or not isinstance(value, (str, int))
            or not str(value).isascii() or not str(value).isdecimal() or int(value) <= 0):
        raise ValueError('invalid_match_id')
    return str(int(value))


def _event(value):
    if not isinstance(value, dict):
        raise ValueError('invalid_record')
    return value.get('record') if 'record' in value else value


def _indexes(envelope, record, instant):
    if envelope is record:
        return None
    if not isinstance(envelope, dict):
        raise ValueError('invalid_index')
    if 'match_id' in envelope and _identifier(envelope['match_id']) != _identifier(record['match_id']):
        raise ValueError('indexed_identity_mismatch')
    if 'observed_at_utc' in envelope:
        try:
            indexed_time = utc_instant(envelope['observed_at_utc'])
        except ValueError:
            raise ValueError('invalid_indexed_time') from None
        if indexed_time != instant:
            raise ValueError('indexed_time_mismatch')
    if 'digest' in envelope and envelope['digest'] != record['id']:
        raise ValueError('indexed_digest_mismatch')
    sequence = envelope.get('sequence')
    if sequence is not None and (type(sequence) is not int or sequence < 1):
        raise ValueError('invalid_sequence')
    return sequence


def _selected(record, previous):
    """Recover the retained provider payload, not necessarily the received one."""
    incoming = record['received_raw']
    if (not isinstance(incoming, dict)
            or _identifier(incoming.get('fixture', {}).get('id')) != _identifier(record['match_id'])):
        raise ValueError('received_identity_mismatch')
    candidates = [policy.provider_raw(incoming)]
    if previous is not None:
        candidates.append(policy.provider_raw(previous))
    if 'baseline_raw' in record:
        if previous is not None or record.get('baseline_time_kind') != 'recovered_at_first_tracked_import':
            raise ValueError('invalid_baseline')
        baseline = record['baseline_raw']
        if not isinstance(baseline, dict) or policy.digest(baseline) != record.get('baseline_raw_sha256'):
            raise ValueError('invalid_baseline')
        candidates.append(policy.provider_raw(baseline))
    if record.get('baseline_equals_received') is True:
        if (previous is not None or record.get('baseline_time_kind') != 'recovered_at_first_tracked_import'
                or record.get('baseline_raw_sha256') != record['received_raw_sha256']):
            raise ValueError('invalid_baseline')
    matching = [raw for raw in candidates if policy.digest(raw) == record.get('selected_raw_sha256')]
    if not matching:
        raise ValueError('selected_version_unavailable')
    raw = deepcopy(matching[0])
    identity = policy.identity(raw)
    if identity is None or identity[0] != _identifier(record['match_id']):
        raise ValueError('selected_identity_mismatch')
    quality = record.get('selected_quality')
    if quality is not None:
        if (not isinstance(quality, dict) or quality.get('version') != policy.VERSION
                or quality.get('quarantined') is not True
                or quality.get('previous_raw_sha256') != record['selected_raw_sha256']):
            raise ValueError('invalid_selected_quality')
        raw[policy.QUALITY_KEY] = deepcopy(quality)
    return raw


def reconstruct(records, *, at):
    """Return retained local versions whose observation time is strictly < at.

    Records can be bare ``fixture_import_policy.observation`` dictionaries or
    SQL envelopes ``{record: dict, match_id, observed_at_utc, digest, sequence}``.
    Present SQL index values must agree with their integrity-checked payload. The
    hash is an integrity check, not a provider signature or historical timestamp
    certification. Input ordering is irrelevant: chains define append order.

    Quarantine annotations are preserved on reconstructed raw values. The caller
    must apply normal projection/quality gates before treating them as training
    results. Exclusions are per match; unknown availability never becomes zero.
    """
    cutoff = utc_instant(at)
    if isinstance(records, (str, bytes, dict)):
        raise ValueError('Une liste d’observations est nécessaire.')
    try:
        records = list(records)
    except TypeError:
        raise ValueError('Une liste d’observations est nécessaire.') from None
    groups, errors, seen_matches = defaultdict(dict), {}, set()
    for envelope in records:
        mid = None
        try:
            record = _event(envelope)
            if not isinstance(record, dict):
                raise ValueError('invalid_record')
            mid = _identifier(record.get('match_id'))
            seen_matches.add(mid)
            try:
                instant = utc_instant(record.get('observed_at_utc'))
            except ValueError:
                raise ValueError('invalid_observation_time') from None
            # Do not validate, follow links into, or use a future correction to
            # invalidate a past value. Its contents were not admissible at T.
            if instant >= cutoff:
                continue
            sequence = _indexes(envelope, record, instant)
            try:
                policy.validate_record(record)
            except (ValueError, TypeError, KeyError, AttributeError):
                raise ValueError('invalid_observation_integrity') from None
            if record.get('time_kind') != 'local_import_recorded':
                raise ValueError('unknown_time_kind')
            if 'previous_observation' not in record:
                raise ValueError('incomplete_chain')
            identity = record['id']
            prior = groups[mid].get(identity)
            current = dict(record=record, instant=instant, sequence=sequence)
            if prior is not None and prior != current:
                raise ValueError('conflicting_observation_index')
            groups[mid][identity] = current
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as error:
            # An unknown timestamp cannot safely be assumed to be after T.
            reason = str(error) if str(error).isascii() else 'invalid_observation'
            errors[mid] = reason or 'invalid_observation'

    selected, excluded = [], []
    for mid in sorted(seen_matches | {key for key in errors if key is not None}, key=int):
        if mid in errors:
            excluded.append(dict(match_id=mid, reason=errors[mid]))
            continue
        known = groups.get(mid, {})
        if not known:
            excluded.append(dict(match_id=mid, reason='not_observed_before_cutoff'))
            continue
        try:
            children, roots = {}, []
            for identity, node in known.items():
                predecessor = node['record']['previous_observation']
                if predecessor is None:
                    roots.append(identity)
                elif not isinstance(predecessor, str) or predecessor not in known:
                    raise ValueError('incomplete_chain')
                elif predecessor in children:
                    raise ValueError('ambiguous_chain')
                else:
                    children[predecessor] = identity
            if len(roots) != 1:
                raise ValueError('ambiguous_chain')
            current_id, prior_node, raw, visited = roots[0], None, None, set()
            while current_id is not None:
                if current_id in visited:
                    raise ValueError('cyclic_chain')
                visited.add(current_id)
                node = known[current_id]
                if prior_node is not None:
                    if node['instant'] < prior_node['instant']:
                        raise ValueError('retrograde_observation_time')
                    if (node['sequence'] is not None and prior_node['sequence'] is not None
                            and node['sequence'] <= prior_node['sequence']):
                        raise ValueError('retrograde_sequence')
                raw = _selected(node['record'], raw)
                prior_node = node
                current_id = children.get(current_id)
            if len(visited) != len(known):
                raise ValueError('disconnected_chain')
            event = prior_node['record']
            selected.append(dict(match_id=mid, raw=raw, observation_id=event['id'],
                                 observed_at_utc=prior_node['instant'].isoformat(),
                                 time_kind=event['time_kind']))
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as error:
            reason = str(error) if str(error).isascii() else 'invalid_observation'
            excluded.append(dict(match_id=mid, reason=reason or 'invalid_observation'))
    if None in errors:
        excluded.append(dict(match_id=None, reason=errors[None]))
    return dict(selected=selected, excluded=excluded)
