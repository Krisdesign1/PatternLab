"""Read-only inventory and quality audit of optional football context.

Run against an existing fixture SQLite store. No API client, training, serving
or application module is imported. One SQLite read transaction defines the
snapshot. Output is a new audit receipt; no source data are repaired.
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import time

from context_event_quality import audit_events
from prematch_context_quality import audit_injuries, audit_lineups

VERSION = 'stored-football-context-quality-v1'
BLOCKS = ('events', 'lineups', 'injuries')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode()).hexdigest()


def stamp(value):
    try:
        value = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def identifier(value):
    if (type(value) not in (str, int) or not str(value).isascii()
            or not str(value).isdecimal() or int(value) <= 0):
        return None
    return str(int(value))


def identity(raw):
    try:
        result = tuple(identifier(value) for value in (
            raw['fixture']['id'], raw['league']['id'],
            raw['teams']['home']['id'], raw['teams']['away']['id']))
        return result if None not in result and result[2] != result[3] else None
    except (KeyError, TypeError):
        return None


def timing(raw):
    fixture = raw.get('fixture', {})
    return stamp(fixture.get('date')) if isinstance(fixture, dict) else None


def status(raw):
    fixture = raw.get('fixture', {})
    state = fixture.get('status', {}) if isinstance(fixture, dict) else {}
    return state.get('short', '').upper() if isinstance(state, dict) and isinstance(state.get('short'), str) else None


def availability(raw, block):
    if block not in raw:
        return 'absent'
    value = raw[block]
    if value is None:
        return 'null'
    if not isinstance(value, list):
        return 'malformed'
    return 'present' if value else 'empty'


def validate_observation(row):
    """Validate the local envelope, indexes and received-payload integrity.

    This checks local consistency, not a signature by the provider. Baseline
    recovery is not used to backdate endpoint observations.
    """
    sequence, mid, indexed_at, payload, indexed_digest = row
    try:
        if type(sequence) is not int or sequence <= 0:
            return None, 'invalid_sequence'
        record = json.loads(payload)
        if not isinstance(record, dict):
            return None, 'record_not_object'
        if record.get('version') != 'fixture-import-quality-v1':
            return None, 'unsupported_record_version'
        if record.get('decision') not in ('received', 'quarantined', 'retained_final',
                'status_correction', 'reviewed', 'review_retained'):
            return None, 'invalid_import_decision'
        previous = record.get('previous_observation', '__missing__')
        if previous is not None and (not isinstance(previous, str) or re.fullmatch('[0-9a-f]{64}', previous) is None):
            return None, 'invalid_previous_observation'
        if digest({k: v for k, v in record.items() if k != 'id'}) != record.get('id'):
            return None, 'record_digest_mismatch'
        if indexed_digest != record['id']:
            return None, 'indexed_digest_mismatch'
        if identifier(mid) != identifier(record.get('match_id')) or identifier(mid) is None:
            return None, 'indexed_match_mismatch'
        observed = stamp(record.get('observed_at_utc'))
        if observed is None or stamp(indexed_at) != observed:
            return None, 'indexed_time_invalid_or_mismatch'
        if record.get('time_kind') != 'local_import_recorded':
            return None, 'unsupported_observation_time_kind'
        raw = record.get('received_raw')
        if not isinstance(raw, dict) or digest(raw) != record.get('received_raw_sha256'):
            return None, 'received_digest_mismatch'
        if raw.get('provider') not in (None, 'api-football', 'API-Football'):
            return None, 'foreign_provider'
        if identity(raw) is None or identity(raw)[0] != identifier(mid):
            return None, 'received_identity_invalid'
        if record.get('decision') == 'quarantined':
            return None, 'quarantined_observation'
        quality = record.get('selected_quality')
        if quality is not None:
            if (not isinstance(quality, dict) or quality.get('version') != 'fixture-import-quality-v1'
                    or quality.get('quarantined') is not True
                    or quality.get('previous_raw_sha256') != record.get('selected_raw_sha256')
                    or quality.get('received_raw_sha256') != record.get('received_raw_sha256')):
                return None, 'malformed_selected_quality'
            return None, 'quarantined_observation'
        # A previously rejected incoming version is not the retained fixture.
        if record.get('selected_raw_sha256') != record.get('received_raw_sha256'):
            return None, 'received_version_not_retained'
        return (record, raw, observed), None
    except (ValueError, KeyError, TypeError, AttributeError):
        return None, 'malformed_or_noncanonical_observation'


def new_group(league, season):
    return dict(league=str(league), season=season, matches=0, regulation_ft=0,
                availability={b: Counter() for b in BLOCKS},
                events_goals_compatible=0, events_nonempty_goals_compatible=0, events_process_eligible=0,
                lineups_complete_xi=0, lineups_current_version_known_by_h30=0,
                lineups_current_version_known_before_kickoff=0)


def run_audit(database, *, source_label):
    started = time.monotonic()
    database = Path(database).resolve(strict=True)
    uri = database.as_uri() + '?mode=ro'
    connection = sqlite3.connect(uri, uri=True, timeout=15)
    connection.execute('PRAGMA query_only=ON')
    connection.execute('BEGIN')
    stream_hash = sha256()
    metadata = dict(connection.execute('SELECT key,value FROM metadata'))
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    total = 0
    groups = {}
    states = {b: Counter() for b in BLOCKS}
    event_issues, lineup_issues, injury_issues = Counter(), Counter(), Counter()
    statuses, type_counts = Counter(), Counter()
    current_identity = {}
    qualified_current = {}
    event_summary = Counter()
    lineup_summary = Counter()
    anomalies = []
    try:
        # Read compact identities for all fixtures, raw payloads only for rows
        # having one of the optional context blocks. No full JSON inventory.
        query = """SELECT match_id,league,season,status,kickoff_utc,issue,
            json_extract(raw_json,'$.teams.home.id'),
            json_extract(raw_json,'$.teams.away.id'),
            CASE WHEN json_type(raw_json,'$.events') IS NOT NULL
                   OR json_type(raw_json,'$.lineups') IS NOT NULL
                   OR json_type(raw_json,'$.injuries') IS NOT NULL
                 THEN raw_json ELSE NULL END
            FROM fixtures ORDER BY ordinal"""
        for row in connection.execute(query):
            mid, league, season, fixture_status, kickoff, issue, home, away, raw_json = row
            stream_hash.update(canonical(row).encode() + b'\n')
            total += 1
            group = groups.setdefault((league, season), new_group(league, season))
            group['matches'] += 1
            ft = str(fixture_status).upper() == 'FT'
            group['regulation_ft'] += int(ft)
            statuses[str(fixture_status)] += 1
            # The kickoff index is naive UTC in this store; only use it to
            # compare identity/schedule with provider-aware dates, never as receipt.
            index_kickoff = stamp(kickoff + '+00:00') if kickoff and stamp(kickoff) is None else stamp(kickoff)
            current_identity[str(mid)] = dict(identity=(identifier(mid), identifier(league), identifier(home), identifier(away)),
                                               kickoff=index_kickoff, issue=issue)
            raw = json.loads(raw_json) if raw_json is not None else {}
            for block in BLOCKS:
                state = availability(raw, block)
                states[block][state] += 1
                group['availability'][block][state] += 1
            if raw_json is None:
                continue
            current_consistent = (index_kickoff is not None
                and identity(raw) == current_identity[str(mid)]['identity']
                and timing(raw) == index_kickoff and status(raw) == str(fixture_status).upper())
            current_usable = current_consistent and not issue
            if not current_consistent:
                anomalies.append(dict(match_id=str(mid), league=str(league), season=season,
                    kind='raw_index_identity_schedule_or_status_mismatch', issue_codes=['raw_index_mismatch']))
            events = audit_events(raw)
            lineups = audit_lineups(raw)
            injuries = audit_injuries(raw)
            event_issues.update(events['issue_codes'])
            lineup_issues.update(lineups['issues'])
            injury_issues.update(injuries['issues'])
            type_counts.update(events['type_counts'])
            for flag in ('goal_compatible', 'event_process_eligible'):
                event_summary[flag] += int(events[flag] and current_usable)
            nonempty_compatible = events['goal_compatible'] and events['availability'] == 'present' and current_usable
            event_summary['nonempty_goal_compatible'] += int(nonempty_compatible)
            group['events_goals_compatible'] += int(events['goal_compatible'] and current_usable)
            group['events_nonempty_goals_compatible'] += int(nonempty_compatible)
            group['events_process_eligible'] += int(events['event_process_eligible'] and current_usable)
            if events['availability'] == 'present' and ft and not events['goal_compatible']:
                anomalies.append(dict(match_id=str(mid), league=str(league), season=season,
                    kind='events_goal_reconciliation_failed', issue_codes=events['issue_codes']))
            lineup_summary['complete_starting_elevens'] += int(lineups['complete_starting_elevens'] and current_usable)
            lineup_summary['coach_identity_rows'] += lineups['coach_count']
            lineup_summary['two_distinct_coaches'] += int(lineups['coach_count'] == 2 and not lineups['coach_issues'] and current_usable)
            group['lineups_complete_xi'] += int(lineups['complete_starting_elevens'] and current_usable)
            if lineups['availability'] == 'present':
                qualified_current[str(mid)] = dict(block_sha256=digest(raw['lineups']),
                    complete=lineups['complete_starting_elevens'] and current_usable, group=(league, season),
                    current_receipts_before_kickoff=[], current_receipts_by_h30=[])
            if lineups['availability'] == 'present' and not lineups['complete_starting_elevens']:
                anomalies.append(dict(match_id=str(mid), league=str(league), season=season,
                    kind='lineups_not_two_complete_identified_elevens', issue_codes=lineups['issues']))

        # Receipt timestamps belong to full fixture import observations. Check
        # the exact block included in that observed payload, not current metadata.
        revision = dict(rows=0, first_observed_at=None, last_observed_at=None,
                        available='fixture_import_revisions' in tables,
                        valid_retained_context_observations=0)
        revision_issues = Counter()
        observed_ids = {b: set() for b in BLOCKS}
        complete_before, complete_h30, before_any = set(), set(), set()
        mismatched_schedule_ids = set()
        latest = {'before_kickoff': {}, 'by_h30': {}}
        chains, unknown_time_ids = {}, set()
        if revision['available']:
            query = """SELECT sequence,match_id,observed_at_utc,payload,digest
                FROM fixture_import_revisions ORDER BY sequence"""
            for row in connection.execute(query):
                stream_hash.update(canonical(row).encode() + b'\n')
                revision['rows'] += 1
                sequence, indexed_mid, indexed_at, payload, indexed_digest = row
                mid, observed = str(indexed_mid), stamp(indexed_at)
                if observed is not None:
                    text = observed.isoformat()
                    revision['first_observed_at'] = min(revision['first_observed_at'] or text, text)
                    revision['last_observed_at'] = max(revision['last_observed_at'] or text, text)
                else:
                    unknown_time_ids.add(mid)
                validated, reason = validate_observation(row)
                try:
                    record = json.loads(payload)
                    previous_id = record.get('previous_observation')
                    prior = chains.get(mid)
                    chain_bad = (not isinstance(record, dict) or 'previous_observation' not in record
                        or previous_id != (prior['id'] if prior else None)
                        or prior is not None and (prior['bad'] or sequence <= prior['sequence']
                            or observed is None or prior['observed'] is None or observed < prior['observed']))
                    if chain_bad:
                        reason = 'invalid_or_disconnected_observation_chain'
                    chains[mid] = dict(id=record.get('id'), sequence=sequence, observed=observed,
                        bad=chain_bad or reason not in (None, 'quarantined_observation', 'received_version_not_retained'))
                except (ValueError, TypeError, AttributeError):
                    reason = 'malformed_observation_chain'
                    chains[mid] = dict(id=None, sequence=sequence, observed=observed, bad=True)
                # Latest retained observation before each cutoff wins, including
                # an explicit empty or absent block. A later good receipt cannot
                # backdate that restoration; a later error cannot erase a past
                # valid receipt. Unknown observation time fails closed for its ID.
                known = current_identity.get(mid)
                if known and known['kickoff'] and observed is not None:
                    for name, cutoff in (('before_kickoff', known['kickoff'] - timedelta(microseconds=1)),
                            ('by_h30', known['kickoff'] - timedelta(minutes=30))):
                        if observed < cutoff:
                            entry = (observed, sequence, validated if reason is None else None, reason)
                            old = latest[name].get(mid)
                            if old is None or entry[:2] > old[:2]:
                                latest[name][mid] = entry
                if reason:
                    revision_issues[reason] += 1
                    continue
                record, raw, observed = validated
                mid = record['match_id']
                if any(block in raw for block in BLOCKS):
                    revision['valid_retained_context_observations'] += 1
                for block in BLOCKS:
                    if availability(raw, block) == 'present':
                        observed_ids[block].add(mid)
                known = current_identity.get(mid)
                kick = timing(raw)
                if (not known or identity(raw) != known['identity'] or kick != known['kickoff'] or known['issue']):
                    revision_issues['current_identity_schedule_or_quality_mismatch'] += 1
                    mismatched_schedule_ids.add(mid)
                    continue
                if availability(raw, 'lineups') == 'present' and kick is not None and observed < kick:
                    before_any.add(mid)
        cutoff_issues = {name: Counter() for name in latest}
        for name, values in latest.items():
            for mid, (observed, sequence, validated, reason) in values.items():
                if mid in unknown_time_ids:
                    cutoff_issues[name]['unknown_observation_time_for_fixture'] += 1
                    continue
                if reason or validated is None:
                    cutoff_issues[name][reason or 'unusable_observation'] += 1
                    continue
                record, raw, observed = validated
                known = current_identity[mid]
                if (identity(raw) != known['identity'] or timing(raw) != known['kickoff'] or known['issue']):
                    cutoff_issues[name]['current_identity_schedule_or_quality_mismatch'] += 1
                    continue
                decision = known['kickoff'] - (timedelta(minutes=30) if name == 'by_h30' else timedelta(microseconds=1))
                qualified = audit_lineups(raw, observed_at=observed, decision_at=decision)
                if not qualified['usable_before_decision']:
                    cutoff_issues[name].update(qualified['issues'] or ['lineups_not_structurally_available'])
                    continue
                (complete_h30 if name == 'by_h30' else complete_before).add(mid)
                current = qualified_current.get(mid)
                if current and current['block_sha256'] == digest(raw['lineups']) and current['complete']:
                    current['current_receipts_' + name].append(observed.isoformat())
        for mid, item in qualified_current.items():
            group = groups[item['group']]
            if item['current_receipts_before_kickoff']:
                group['lineups_current_version_known_before_kickoff'] += 1
                lineup_summary['current_version_known_before_kickoff'] += 1
            if item['current_receipts_by_h30']:
                group['lineups_current_version_known_by_h30'] += 1
                lineup_summary['current_version_known_by_h30'] += 1
        revision.update(issue_counts=dict(revision_issues),
            cutoff_exclusion_counts={name: dict(counts) for name, counts in cutoff_issues.items()},
            observation_chains_checked=True, latest_observation_at_cutoff_required=True,
            ids_with_nonempty_received_block={b: len(ids) for b, ids in observed_ids.items()},
            lineups_any_observation_before_announced_kickoff=len(before_any),
            lineups_complete_NS_consistent_before_kickoff=len(complete_before),
            lineups_complete_NS_consistent_by_h30=len(complete_h30),
            ids_rejected_by_current_identity_schedule_or_quality=len(mismatched_schedule_ids))
        metadata_after = dict(connection.execute('SELECT key,value FROM metadata'))
        if metadata != metadata_after:
            raise ValueError('Snapshot metadata changed inside read transaction.')
    finally:
        connection.rollback()
        connection.close()
    rows = []
    for key, group in sorted(groups.items(), key=lambda item: (str(item[0][0]), str(item[0][1]))):
        group['availability'] = {block: dict(counts) for block, counts in group['availability'].items()}
        for block in BLOCKS:
            present = group['availability'][block].get('present', 0)
            group[block + '_nonempty_fraction_of_all_matches'] = present / group['matches']
        rows.append(group)
    return dict(version=VERSION, created_at=datetime.now(timezone.utc).isoformat(), source_label=source_label,
        source_database=str(database), snapshot_metadata=metadata, snapshot_is_read_transaction=True,
        audit_input_stream_sha256=stream_hash.hexdigest(), database_writes=0, api_calls=0,
        fixture_count=total, statuses=dict(statuses), availability={b: dict(c) for b, c in states.items()},
        events=dict(event_summary, type_counts=dict(type_counts), issue_counts=dict(event_issues),
            completeness_of_all_events_proven=False),
        lineups=dict(lineup_summary, issue_counts=dict(lineup_issues), provider_completeness_proven=False),
        injuries=dict(issue_counts=dict(injury_issues), no_injuries_proven=False),
        revisions=revision, by_league_season=rows, anomalies=anomalies,
        elapsed_seconds=round(time.monotonic()-started, 3),
        interpretation=dict(absent_is_not_empty=True, empty_is_not_zero_injuries=True,
            timestamps_are_local_receipts_not_provider_publication=True,
            old_corrected_data_are_not_historical_prematch_snapshots=True,
            eligibility_is_only_for_isolated_research_not_serving=True,
            contextual_model_training_ready=False,
            automatic_correction=False, model_training=False, production_model_changed=False))
