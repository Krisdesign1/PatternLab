"""Offline chronological reconstruction of the fixed argumented shortlist.

This module has no I/O, fitting, API requests or production persistence.  NS
fixtures are synthesized from finished identities; these are not archived
prematch forecasts and their historical availability cannot be established.
Scores of a local day's targets are inspected only after its predictions and
all fixed selection flags have been committed.
"""
from collections import Counter, defaultdict, deque
from copy import deepcopy
from datetime import date, datetime, time, timezone
from hashlib import sha256
import json

import argumented_selection_engine as argumented
import high_probability_engine as hp
from club_identity import ClubProfiles, fixture_identity


VERSION = 'argumented-selection-historical-replay-v1'
VALUES = argumented.VALUES
ZONE = argumented.ZONE
PROTOCOL = dict(
    selection_version=argumented.VERSION,
    source='API-Football', timezone=ZONE.key,
    period='Kinshasa local dates [start,end); UTC kickoff strictly before end 00:00',
    cutoff='strictly before earliest target UTC date in each Kinshasa day',
    prediction='synthesized NS identities; virtual prediction at cutoff 00:00 UTC',
    calendar='finished identities reconstructed offline; no archived NS availability',
    common_pool='at least 30 league matches, 3 UTC days, and 10 exact-ID role matches each',
    full_rule='model and baseline argmax agree; last ten in both roles have >=5 matching and modal',
    agreement_only='same common pool and argmax agreement, without recent confirmation',
    min7='full rule with >=7 of ten matching in each role',
    recent365='full rule; all ten samples in each role within 365 UTC calendar days inclusive',
    variant_thresholds_frozen=True, recent_days=365, strict_matching=7,
    tuning=False, fitting=False, synthesized_NS=True,
    tie_order=list(VALUES), controls='same Kinshasa day and common pool; exactly the variant count',
)
_DISPLAY = ('country', 'competition', 'season', 'source')
_UNKNOWN = {'tbd', 'unknown', 'to be determined', 'à déterminer', 'a determiner', '?'}


def _date(value):
    if type(value) is date:
        return value
    if not isinstance(value, str):
        raise ValueError('A calendar date is required.')
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError('Dates must use YYYY-MM-DD.')
    return parsed


def _issue(diagnostics, reason, mid=None):
    diagnostics['issue_counts'][reason] += 1
    if len(diagnostics['issue_examples']) < 100:
        diagnostics['issue_examples'].append(dict(match_id=mid, reason=reason))


def _fixture(row, identity):
    """Whitelist identity fields; never copy or inspect target result fields."""
    mid, league, home_id, away_id, stamp = identity
    result = {key: row[key] for key in _DISPLAY if key in row}
    result.update(match_id=mid, league=league, competition_code=league,
                  home=row['home'], away=row['away'], home_id=home_id,
                  away_id=away_id, provider='api-football',
                  timestamp=stamp.replace(tzinfo=None).isoformat(timespec='seconds'),
                  day=stamp.date().isoformat(), status='TIMED', raw_status='NS',
                  is_upcoming=True, is_historical=False, selectable=True)
    return result


def _prepare(rows, end, diagnostics):
    prepared = []
    boundary = datetime.combine(end, time.min, timezone.utc)
    for row in rows:
        mid = None
        try:
            if not isinstance(row, dict):
                raise argumented._Invalid('invalid_identity', 'Invalid row.')
            # Identity/date/provider checks are deliberately score-free.
            identity = argumented._identity(row)
            mid, _, _, _, stamp = identity
            if stamp >= boundary:
                diagnostics['outside_end_count'] += 1
                continue
            if (row.get('raw_status', 'FT') not in ('FT', 'AET', 'PEN')
                    or row.get('status', 'FINISHED') not in ('FT', 'AET', 'PEN', 'FINISHED')
                    or row.get('is_upcoming', False) is not False
                    or row.get('is_historical', True) is not True):
                raise argumented._Invalid('unfinished_history', 'Not a finished result.')
            prepared.append(dict(raw=row, match_id=mid, stamp=stamp,
                                 day=stamp.astimezone(ZONE).date(),
                                 fixture=_fixture(row, identity)))
        except argumented._Invalid as error:
            _issue(diagnostics, error.reason, mid)
    prepared.sort(key=lambda item: (item['stamp'], int(item['match_id'])))
    diagnostics['identity_valid_count'] = len(prepared)
    return prepared


def _empty(league):
    return dict(league=league, profiles=ClubProfiles(provider='api-football'),
                training=0, days=Counter(), counts=Counter(), totals=[[0, 0], [0, 0]],
                fingerprint=sha256(), last_training_day=None)


class _History:
    """One chronological pass with incremental league and exact-role summaries.

    A conflicting duplicate is removed only when both versions are in the
    available past.  A rare conflict rebuilds its affected league; ordinary
    predictions never rescan the historical corpus.
    """
    def __init__(self, prepared, diagnostics):
        self.prepared, self.diagnostics = prepared, diagnostics
        self.pointer = 0
        self.before = None
        self.states = {}
        self.recent = defaultdict(lambda: deque(maxlen=10))
        self.seen, self.active, self.blocked = {}, {}, set()

    def _add(self, row):
        league = row['league']
        state = self.states.get(league)
        if state is None:
            state = self.states[league] = _empty(league)
        goals = hp.predictor.goals(row)
        state['profiles'].add(row, goals)
        state['training'] += 1
        state['days'][row['day']] += 1
        state['counts'][row['value']] += 1
        state['last_training_day'] = row['day']
        for side in range(2):
            for half in range(2):
                state['totals'][side][half] += goals[side][half]
        # A streaming context signature, not the production JSON-array digest.
        state['fingerprint'].update(json.dumps(row, sort_keys=True, ensure_ascii=False,
            separators=(',', ':'), allow_nan=False).encode()+b'\n')
        for role in ('home', 'away'):
            self.recent[(league, row[f'{role}_id'], role)].append(row)

    def _rebuild(self, league):
        self.states[league] = _empty(league)
        for key in [key for key in self.recent if key[0] == league]:
            del self.recent[key]
        members = [row for row in self.active.values() if row['league'] == league]
        for row in sorted(members, key=lambda row: (row['timestamp'], int(row['match_id']))):
            self._add(row)
        self.diagnostics['conflict_rebuild_count'] += 1

    def advance(self, before):
        if self.before is not None and before < self.before:
            raise ValueError('Historical cutoffs must advance chronologically.')
        while (self.pointer < len(self.prepared)
               and self.prepared[self.pointer]['stamp'].date() < before):
            item = self.prepared[self.pointer]
            self.pointer += 1
            mid, raw = item['match_id'], item['raw']
            if mid in self.blocked:
                continue
            if mid in self.seen:
                if self.seen[mid] == raw:
                    self.diagnostics['history_duplicate_count'] += 1
                else:
                    self.blocked.add(mid)
                    previous = self.active.pop(mid, None)
                    _issue(self.diagnostics, 'conflicting_history_duplicate', mid)
                    if previous is not None:
                        self._rebuild(previous['league'])
                continue
            self.seen[mid] = raw
            try:
                checked, _ = argumented._history_row(raw)
            except argumented._Invalid as error:
                _issue(self.diagnostics, error.reason, mid)
                continue
            self.active[mid] = checked
            self._add(checked)
            self.diagnostics['history_validated_count'] += 1
        self.before = before

    def estimate(self, fixture, before):
        state = self.states.get(fixture['league'])
        if state is None:
            state = _empty(fixture['league'])
        n = state['training']
        inputs = dict(league=fixture['league'], profiles=state['profiles'], training=n,
            history_days=len(state['days']),
            means=[[value/n for value in side] for side in state['totals']] if n else None,
            baseline={value: (state['counts'][value]+1)/(n+3) for value in VALUES},
            history_fingerprint='stream-'+state['fingerprint'].hexdigest(),
            last_training_day=state['last_training_day'])
        return hp._estimate(inputs, fixture['home'], fixture['away'], before.isoformat(),
                            **fixture_identity(fixture))

    def last_ten(self, fixture, role, choice):
        rows = self.recent[(fixture['league'], fixture[f'{role}_id'], role)]
        counts = {value: sum(row['value'] == value for row in rows) for value in VALUES}
        first = rows[0]['timestamp'] if rows else None
        last = rows[-1]['timestamp'] if rows else None
        return dict(matches=len(rows), counts=counts, matching=counts[choice],
                    first_at=first, last_at=last, ids=[row['match_id'] for row in rows],
                    span_days=(_date(last[:10])-_date(first[:10])).days if rows else None)


def _prediction(fixture, history, before):
    estimate = history.estimate(fixture, before)
    if (not estimate['eligible'] or not estimate['personalized']
            or min(estimate['home_profile']['matches'], estimate['away_profile']['matches']) < 10):
        return None
    p, baseline = estimate['probabilities'], estimate['baseline']
    choice, baseline_choice = max(VALUES, key=p.get), max(VALUES, key=baseline.get)
    home, away = (history.last_ten(fixture, role, choice) for role in ('home', 'away'))
    if min(home['matches'], away['matches']) != 10:
        return None
    agreement = choice == baseline_choice
    full = agreement and all(recent['matching'] >= 5
        and recent['matching'] == max(recent['counts'].values()) for recent in (home, away))
    flags = dict(full_rule=full, agreement_only=agreement,
                 min7=full and min(home['matching'], away['matching']) >= 7,
                 recent365=full and all((before-_date(recent['first_at'][:10])).days <= 365
                                       for recent in (home, away)))
    cutoff_at = datetime.combine(before, time.min, timezone.utc).isoformat(timespec='seconds')
    return dict(match_id=fixture['match_id'], day=argumented._stamp(fixture['timestamp']).astimezone(ZONE).date().isoformat(),
        timestamp=fixture['timestamp'], league=fixture['league'], source='API-Football',
        provider='api-football', fixture=fixture, **estimate, choice=choice,
        baseline_choice=baseline_choice, max_probability=p[choice],
        baseline_max_probability=baseline[baseline_choice], selection_probability=p[choice],
        baseline_probability=baseline[choice], recent_home=home, recent_away=away,
        flags=flags, history_before=before.isoformat(),
        cutoff_exclusive=before.isoformat(), cutoff_at=cutoff_at,
        prediction_at=cutoff_at, virtual_prediction_at=cutoff_at,
        synthesized_NS=True, no_future_target_history=True)


def _outcome(items, diagnostics):
    """Called only after every prediction and flag for the local day is fixed."""
    first = items[0]['raw']
    if any(item['raw'] != first for item in items[1:]):
        _issue(diagnostics, 'conflicting_target_duplicate', items[0]['match_id'])
        return None
    try:
        checked, _ = argumented._history_row(first)
        return checked['value']
    except argumented._Invalid as error:
        _issue(diagnostics, 'invalid_target_'+error.reason, items[0]['match_id'])
        return None


def replay(rows, *, start='2024-01-01', end='2026-10-30', progress=None):
    """Return all common-pool forecasts and fixed-rule flags, without I/O.

    Warmup uses the entire eligible past, including before ``start``.  The
    target interval uses local dates [start,end), additionally excluding every
    UTC kickoff >= end 00:00.  Each local day uses one conservative common
    cutoff: the earliest UTC date of that day's finished fixture identities.
    """
    start, end = _date(start), _date(end)
    if start >= end:
        raise ValueError('The exclusive end must follow start.')
    if not isinstance(rows, (list, tuple)):
        raise ValueError('Normalized API-Football history must be a list or tuple.')
    diagnostics = dict(input_count=len(rows), identity_valid_count=0, outside_end_count=0,
        history_validated_count=0, history_duplicate_count=0, conflict_rebuild_count=0,
        target_identity_count=0, target_duplicate_count=0, target_invalid_identity_count=0,
        common_pool_count=0, invalid_actual_count=0, issue_counts=Counter(), issue_examples=[])
    prepared = _prepare(rows, end, diagnostics)
    days = defaultdict(list)
    for item in prepared:
        if start <= item['day'] < end:
            days[item['day']].append(item)
    history = _History(prepared, diagnostics)
    records, day_counts = [], []
    for number, day in enumerate(sorted(days), 1):
        candidates = days[day]
        before = min(item['stamp'].date() for item in candidates)
        history.advance(before)
        groups = defaultdict(list)
        for item in candidates:
            groups[item['match_id']].append(item)
        diagnostics['target_identity_count'] += len(groups)
        predictions, labels = [], {}
        for mid, members in sorted(groups.items(), key=lambda pair: int(pair[0])):
            fixture = members[0]['fixture']
            if mid in history.seen:
                diagnostics['target_invalid_identity_count'] += 1
                _issue(diagnostics, 'target_identity_already_in_past', mid)
                continue
            if (any(item['fixture'] != fixture for item in members[1:])
                    or any(fixture[role].strip().casefold() in _UNKNOWN for role in ('home', 'away'))):
                diagnostics['target_invalid_identity_count'] += 1
                _issue(diagnostics, 'unconfirmed_or_conflicting_target_identity', mid)
                continue
            diagnostics['target_duplicate_count'] += len(members)-1
            record = _prediction(fixture, history, before)
            if record is not None:
                predictions.append(record)
                labels[mid] = members
        # The complete candidate pool and every rule decision now exist.
        for record in predictions:
            record['actual'] = _outcome(labels[record['match_id']], diagnostics)
            diagnostics['invalid_actual_count'] += record['actual'] is None
        predictions.sort(key=lambda row: (row['timestamp'], int(row['match_id'])))
        records.extend(predictions)
        day_counts.append(dict(day=day.isoformat(), cutoff_exclusive=before.isoformat(),
            target_count=len(groups), common_pool_count=len(predictions),
            **{name: sum(row['flags'][name] for row in predictions)
               for name in ('full_rule', 'agreement_only', 'min7', 'recent365')}))
        if progress:
            progress(dict(stage='replay', completed=number, total=len(days), day=day.isoformat(),
                          records=len(records), common_pool_count=len(predictions)))
    diagnostics['common_pool_count'] = len(records)
    diagnostics['issue_counts'] = dict(sorted(diagnostics['issue_counts'].items()))
    diagnostics['warmup_count_before_start'] = sum(item['day'] < start for item in prepared)
    return dict(version=VERSION, start=start.isoformat(), end=end.isoformat(),
                protocol=deepcopy(PROTOCOL), records=records, day_counts=day_counts,
                diagnostics=diagnostics)
