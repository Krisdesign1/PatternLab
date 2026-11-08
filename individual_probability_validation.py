"""Paired, first-capture match evaluation and an explicit prospective protocol.

No fitting, provider calls, forecast reconstruction or automatic promotion.
The archive already records all calculable matches before UI filters. A saved
protocol restricts confirmation to genuinely later captures and kickoff dates.
"""
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from math import fsum, isclose, isfinite, log
from pathlib import Path
from zoneinfo import ZoneInfo

from league_model import outcome, score

VERSION = 'individual-probability-validation-v1'
LABELS = ('1st', 'Égal', '2nd')
MODEL_VERSION = 'half-goals-club-ids-v2'
ENGINE_VERSION = 'high-probability-club-ids-v2'
ROOT = Path(__file__).resolve().parent
ZONE = ZoneInfo('Africa/Kinshasa')
SOURCE_FILES = ('match_predictor.py', 'club_identity.py', 'league_model.py', 'high_probability_engine.py',
                'probability_implementation.py', 'halftime_domain.py',
                'high_probability_archive.py', 'individual_probability_validation.py',
                'high_probability_workspace.py', 'probability_time_evidence.py',
                'fixture_temporal.py', 'fixture_temporal_store.py',
                'fixture_import_policy.py', 'fixture_store.py', 'api_football.py')
ENRICHMENT = 'data/research/halftime_enrichment/2026-10-26-complet-v1/evaluation/validation_selection.json'


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError('Un instant daté est nécessaire.')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('Un fuseau horaire est nécessaire.')
    return result.astimezone(timezone.utc)


def _now(now):
    result = datetime.now(timezone.utc) if now is None else now
    if not isinstance(result, datetime) or result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('Une horloge avec fuseau est nécessaire.')
    return result.astimezone(timezone.utc)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def protocol_path(root=None):
    return Path(root or ROOT/'data/api_football')/'individual_validation'/'protocol.json'


def _seal(value):
    result = dict(value)
    result['id'] = sha256(_json(value).encode()).hexdigest()
    return result


def _validate_protocol(value):
    from probability_implementation import validate_hashes
    raw = {key: item for key, item in value.items() if key != 'id'}
    if value.get('id') != sha256(_json(raw).encode()).hexdigest() or value.get('version') != VERSION:
        raise ValueError('Le protocole de validation a été modifié ou est incompatible.')
    if (_stamp(value['created_at']) >= _stamp(value['start_at'])
            or value['model_version'] != MODEL_VERSION or value['engine_version'] != ENGINE_VERSION
            or value['methods'] != ['frequency', 'clubs']):
        raise ValueError('Les règles du protocole sont incohérentes.')
    validate_hashes(value.get('model_code_sha256'))
    return value


def prepare(root=None, *, now=None):
    """Explicit exclusive creation; never called while rendering the app."""
    current = _now(now)
    if protocol_path(root).resolve() == protocol_path().resolve():
        current = _now(None)  # A production protocol cannot be backdated.
    selected = json.loads((ROOT/ENRICHMENT).read_text())
    # The historical selection failed. Do not resurrect that candidate by
    # looking at its later 2026 result or treating a fixed head as selected.
    if selected.get('policies', {}).get('rich') != 'clubs':
        raise ValueError('La décision historique du candidat enrichi nécessite une revue.')
    from probability_implementation import code_hashes
    tomorrow = current.astimezone(ZONE).date()+timedelta(days=1)
    start = datetime.combine(tomorrow, datetime.min.time(), tzinfo=ZONE)
    value = _seal(dict(version=VERSION, created_at=current.isoformat(), start_at=start.isoformat(),
        model_version=MODEL_VERSION, engine_version=ENGINE_VERSION, methods=['frequency', 'clubs'],
        model_code_sha256=code_hashes(),
        population='All API-Football ranking-archive fixtures for the pinned model and engine; no league, probability, odds or user-selection filter.',
        capture='First genuine pre-kickoff archive capture per fixture, captured at/after protocol start. Missing baseline excludes the pair; never substitute a later capture.',
        outcome='Latest own verified result revision available as of reading. Regulation HT/FT only; pending, unresolved and void are never misses.',
        primary='Paired mean logloss: Clubs minus same-capture league frequency, lower is better.',
        diagnostics=['Brier sum', 'favorite accuracy', 'fixed decile calibration for all three classes',
                     'fixed confidence thresholds .50/.60/.70 with reference on identical fixtures', 'daily and UTC-week stability'],
        temporal_blocks='Only complete UTC calendar weeks after start; a week with pending or unresolved captured fixtures remains open. Void results are reported and excluded.',
        decision='Descriptive monitoring only. No automatic promotion, no threshold tuning, no promised accuracy or betting return.',
        enriched=dict(status='rejected_on_historical_validation', selected_policy='clubs',
            artifact=ENRICHMENT, sha256=sha256((ROOT/ENRICHMENT).read_bytes()).hexdigest(),
            validation_logloss={key: selected['metrics'][key]['logloss'] for key in ('frequency', 'clubs', 'scores', 'rich')},
            note='Enriched heads did not pass the frozen 2025 selection. No new fit and no prospective claim for the rejected head.'),
        code_sha256={name: sha256((ROOT/name).read_bytes()).hexdigest() for name in SOURCE_FILES}))
    path = protocol_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
    return value


def load_protocol(root=None):
    path = protocol_path(root)
    if not path.exists():
        return None
    return _validate_protocol(json.loads(path.read_text()))


def compatibility(plan):
    _validate_protocol(plan)
    changed = [name for name, digest in plan['code_sha256'].items()
               if not (ROOT/name).is_file() or sha256((ROOT/name).read_bytes()).hexdigest() != digest]
    artifact = ROOT/plan['enriched']['artifact']
    if not artifact.is_file() or sha256(artifact.read_bytes()).hexdigest() != plan['enriched']['sha256']:
        changed.append(plan['enriched']['artifact'])
    return dict(compatible=not changed, changed=changed)


def _probabilities(value):
    if (not isinstance(value, dict) or set(value) != set(LABELS)
            or any(isinstance(p, bool) or not isinstance(p, (int, float))
                   or not isfinite(p) or not 0 < p <= 1 for p in value.values())
            or not isclose(fsum(value.values()), 1, abs_tol=1e-10)):
        raise ValueError('Distribution incomplète ou invalide.')
    return dict(value)


def _result(entry, now):
    # An as-of reading must not use a result revision from the future.
    events = entry.get('result_history')
    if events is not None:
        known = [event for event in events if _stamp(event['checked_at']) <= now]
        event = max(known, key=lambda e: (e['revision'], _stamp(e['checked_at']))) if known else {}
    else:
        event = entry if entry.get('checked_at') and _stamp(entry['checked_at']) <= now else {}
    state = event.get('status', 'pending')
    if state not in ('pending', 'settled', 'unresolved', 'void'):
        raise ValueError('État du résultat inconnu.')
    actual = None
    if state == 'settled':
        effective = _stamp(entry['fixture']['timestamp'])
        audit = event.get('schedule_resolution')
        if audit is not None:
            if (audit.get('original_timestamp') != entry['fixture']['timestamp']
                    or audit.get('forecast_created_at') != entry['created_at']):
                raise ValueError('Correction horaire incompatible.')
            # API-Football's saved normalized schedule uses naive UTC. This
            # exception is restricted to that verified provider audit field;
            # capture, observation and protocol clocks must remain aware.
            effective = datetime.fromisoformat(audit['effective_timestamp'].replace('Z', '+00:00'))
            effective = (effective.replace(tzinfo=timezone.utc) if effective.tzinfo is None
                         else effective.astimezone(timezone.utc))
        if (_stamp(entry['created_at']) >= effective or effective > now
                or date.fromisoformat(entry['before']) > effective.date()
                or _stamp(event['checked_at']) < effective):
            raise ValueError('Le résultat ne suit pas le coup d’envoi effectif.')
        actual = outcome(score(event.get('ht')), score(event.get('ft')))
        if actual != event.get('actual'):
            raise ValueError('Le score et le résultat sont contradictoires.')
    return state, actual


def _metrics(rows, method):
    n = len(rows)
    if not n:
        return dict(matches=0, correct=0, accuracy=None, logloss=None, brier=None)
    correct = sum(max(LABELS, key=r[method].get) == r['actual'] for r in rows)
    return dict(matches=n, correct=correct, accuracy=correct/n,
        logloss=fsum(-log(r[method][r['actual']]) for r in rows)/n,
        brier=fsum(fsum((r[method][label]-int(r['actual'] == label))**2 for label in LABELS) for r in rows)/n)


def _paired(rows):
    evaluated = [row for row in rows if row['state'] == 'settled' and row['paired']]
    metrics = {method: _metrics(evaluated, method) for method in ('frequency', 'clubs')}
    delta = (metrics['clubs']['logloss']-metrics['frequency']['logloss']) if evaluated else None
    return dict(matches=len(rows), evaluated=len(evaluated),
        counts=dict(Counter(row['state'] for row in rows)),
        missing_baseline=sum(not row['paired'] for row in rows), metrics=metrics, delta_logloss=delta)


def build(archived, *, now=None, protocol=None):
    """Pure descriptive projection. Caller supplies verified archive records.

    First capture is chosen before result availability or baseline completeness.
    Repeating a match in groups or later captures never adds a scored example.
    The prospective protocol never incorporates the older diagnostic period.
    """
    current = _now(now)
    start = _stamp(_validate_protocol(protocol)['start_at']) if protocol is not None else None
    excluded = Counter(); candidates = []
    for entry in archived:
        try:
            if entry.get('source') != 'API-Football' or entry['fixture'].get('provider') != 'api-football':
                raise ValueError('source')
            if entry.get('model_version') != MODEL_VERSION or entry.get('engine_version') != ENGINE_VERSION:
                excluded['other_version'] += 1
                continue
            if not isinstance(entry.get('id'), str) or not entry['id']:
                raise ValueError('identity')
            created, kickoff = _stamp(entry['created_at']), _stamp(entry['fixture']['timestamp'])
            if created > current or created >= kickoff:
                raise ValueError('capture_time')
            before = date.fromisoformat(entry['before'])
            if before > min(created.date(), kickoff.date()):
                raise ValueError('history_cutoff')
            fixture = entry['fixture']
            for field in ('match_id', 'league', 'home_id', 'away_id'):
                token = str(fixture[field])
                if isinstance(fixture[field], bool) or not token.isascii() or not token.isdigit() or int(token) <= 0:
                    raise ValueError('identity')
            if str(fixture['home_id']) == str(fixture['away_id']):
                raise ValueError('identity')
            candidates.append((created, entry['id'], entry, _probabilities(entry['probabilities'])))
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
            excluded['invalid_snapshot'] += 1
    first = {}
    for created, identity, entry, probabilities in sorted(candidates, key=lambda r: (r[0], r[1])):
        first.setdefault(str(int(entry['fixture']['match_id'])), (created, entry, probabilities))
    rows = []
    for match_id, (created, entry, probabilities) in first.items():
        if start is not None and (created < start or _stamp(entry['fixture']['timestamp']) < start):
            excluded['before_protocol_start'] += 1
            continue
        if protocol is not None and entry.get('model_code_sha256') != protocol.get('model_code_sha256'):
            excluded['incompatible_model_code'] += 1
            continue
        try:
            baseline = _probabilities(entry.get('baseline'))
        except ValueError:
            baseline = None
        try:
            state, actual = _result(entry, current)
        except (ValueError, KeyError, TypeError, AttributeError):
            state, actual = 'unresolved', None
            excluded['invalid_result'] += 1
        kickoff = _stamp(entry['fixture']['timestamp'])
        monday = kickoff.date()-timedelta(days=kickoff.weekday())
        rows.append(dict(match_id=match_id, forecast_id=entry['id'], created_at=created.isoformat(),
            kickoff=kickoff.isoformat(), day=kickoff.astimezone(ZONE).date().isoformat(), week=monday.isoformat(),
            league=str(entry['fixture']['league']), state=state, actual=actual,
            clubs=probabilities, frequency=baseline, paired=baseline is not None))
    evaluated = [row for row in rows if row['state'] == 'settled' and row['paired']]
    thresholds = []
    for threshold in (.5, .6, .7):
        selected = [r for r in evaluated if max(r['clubs'].values()) >= threshold]
        values = _paired(selected)
        thresholds.append(dict(threshold=threshold, coverage=len(selected)/len(evaluated) if evaluated else None,
            mean_confidence=fsum(max(r['clubs'].values()) for r in selected)/len(selected) if selected else None,
            **values))
    calibration = []
    for method in ('frequency', 'clubs'):
        for label in LABELS:
            for index in range(10):
                selected = [r for r in evaluated if min(9, int(r[method][label]*10)) == index]
                calibration.append(dict(method=method, label=label, lower=index/10, upper=(index+1)/10,
                    matches=len(selected), announced=fsum(r[method][label] for r in selected)/len(selected) if selected else None,
                    observed=sum(r['actual'] == label for r in selected)/len(selected) if selected else None))
    weekly = []
    for week in sorted({r['week'] for r in rows}):
        members = [r for r in rows if r['week'] == week]
        opening = _stamp(week+'T00:00:00+00:00')
        calendar_closed = opening+timedelta(days=7) <= current and (start is None or opening >= start)
        resolved = not any(r['state'] != 'void' and
                           (r['state'] in ('pending', 'unresolved') or not r['paired']) for r in members)
        weekly.append(dict(week=week, calendar_closed=calendar_closed, eligible=calendar_closed and resolved,
                           **_paired(members)))
    daily = [dict(day=day, **_paired([r for r in rows if r['day'] == day])) for day in sorted({r['day'] for r in rows}, reverse=True)]
    return dict(version=VERSION, as_of=current.isoformat(), scope='prospective' if protocol else 'descriptive',
        protocol_id=protocol['id'] if protocol else None, starts_at=protocol['start_at'] if protocol else None,
        **_paired(rows), rows=rows, daily=daily, weekly=weekly, thresholds=thresholds, calibration=calibration,
        closed_weeks=sum(r['eligible'] for r in weekly), duplicates_removed=len(candidates)-len(first),
        exclusions=dict(excluded), decision='no_automatic_promotion',
        limitations=['Archive coverage is calculable personalized matches, not all API fixtures.',
                     'Descriptive comparisons and repeated teams do not prove a durable advantage.',
                     'No rejected enriched head is presented as a selected candidate.'])
