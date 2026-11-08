"""Independently verify a saved individual-probability report, without APIs.

Only Python's standard library is imported. The source is a list of validated
archive records, optionally gzip-compressed; no application module is imported
and no forecast or result is recomputed from current provider data.
"""
import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import gzip
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo


LABELS = ('1st', 'Égal', '2nd')
METHODS = ('frequency', 'clubs')
MODEL = 'half-goals-club-ids-v2'
ENGINE = 'high-probability-club-ids-v2'
VERSION = 'individual-probability-validation-v1'
ROOT = Path(__file__).resolve().parents[1]


def instant(text, *, provider_schedule=False):
    value = datetime.fromisoformat(text.replace('Z', '+00:00'))
    if value.tzinfo is None or value.utcoffset() is None:
        if not provider_schedule:
            raise ValueError('An aware information-time clock is required.')
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def distribution(value):
    if not isinstance(value, dict) or set(value) != set(LABELS):
        raise ValueError('Incomplete distribution.')
    vector = [value[label] for label in LABELS]
    if any(isinstance(p, bool) or not isinstance(p, (int, float))
           or not math.isfinite(p) or not 0 < p <= 1 for p in vector):
        raise ValueError('Invalid probability.')
    if not math.isclose(math.fsum(vector), 1, rel_tol=1e-9, abs_tol=1e-10):
        raise ValueError('Probabilities do not sum to one.')
    return {label: value[label] for label in LABELS}


def own_result(record, reading):
    history = record.get('result_history')
    if history is None:
        revision = (record if record.get('checked_at') and instant(record['checked_at']) <= reading else None)
    else:
        available = [revision for revision in history if instant(revision['checked_at']) <= reading]
        revision = sorted(available, key=lambda item: (item['revision'], instant(item['checked_at'])))[-1] if available else None
    if revision is None:
        return 'pending', None
    state = revision.get('status', 'pending')
    if state not in ('pending', 'settled', 'unresolved', 'void'):
        raise ValueError('Unknown result state.')
    if state != 'settled':
        return state, None
    kickoff = instant(record['fixture']['timestamp'])
    correction = revision.get('schedule_resolution')
    if correction is not None:
        if (correction.get('original_timestamp') != record['fixture']['timestamp']
                or correction.get('forecast_created_at') != record['created_at']):
            raise ValueError('Schedule correction belongs to another forecast.')
        kickoff = instant(correction['effective_timestamp'], provider_schedule=True)
    if (instant(record['created_at']) >= kickoff or kickoff > reading
            or date.fromisoformat(record['before']) > kickoff.date()
            or instant(revision['checked_at']) < kickoff):
        raise ValueError('Result predates the effective kickoff.')
    scores = []
    for field in ('ht', 'ft'):
        match = re.fullmatch(r'\s*\(?\s*(\d+)\s*-\s*(\d+)\s*\)?\s*', revision.get(field) or '')
        if not match:
            raise ValueError('Missing regulation score.')
        scores.append(tuple(int(number) for number in match.groups()))
    halftime, fulltime = scores
    if any(first > final for first, final in zip(halftime, fulltime)):
        raise ValueError('Halftime exceeds fulltime.')
    balance = 2 * sum(halftime) - sum(fulltime)
    actual = '1st' if balance > 0 else '2nd' if balance < 0 else 'Égal'
    if revision.get('actual') != actual:
        raise ValueError('Result label contradicts own score.')
    return state, actual


def scores(rows, method):
    if not rows:
        return dict(matches=0, correct=0, accuracy=None, logloss=None, brier=None)
    losses, squared, correct = [], [], 0
    for row in rows:
        probabilities = [row[method][label] for label in LABELS]
        actual = LABELS.index(row['actual'])
        correct += probabilities.index(max(probabilities)) == actual
        losses.append(-math.log(probabilities[actual]))
        squared.append(math.fsum((p - (index == actual)) ** 2
                                 for index, p in enumerate(probabilities)))
    count = len(rows)
    return dict(matches=count, correct=correct, accuracy=correct/count,
                logloss=math.fsum(losses)/count, brier=math.fsum(squared)/count)


def summary(rows):
    paired = [row for row in rows if row['state'] == 'settled' and row['paired']]
    metrics = {method: scores(paired, method) for method in METHODS}
    return dict(matches=len(rows), evaluated=len(paired),
                counts=dict(Counter(row['state'] for row in rows)),
                missing_baseline=sum(not row['paired'] for row in rows), metrics=metrics,
                delta_logloss=(metrics['clubs']['logloss'] - metrics['frequency']['logloss']) if paired else None)


def project(archive, reading, protocol=None):
    """Rebuild cohorts before inspecting baselines or own result availability."""
    excluded = Counter()
    fixtures = defaultdict(list)
    valid_captures = 0
    for record in archive:
        try:
            if record.get('source') != 'API-Football' or record['fixture'].get('provider') != 'api-football':
                raise ValueError('Wrong source.')
            if (record.get('model_version'), record.get('engine_version')) != (MODEL, ENGINE):
                excluded['other_version'] += 1
                continue
            if not isinstance(record.get('id'), str) or not record['id']:
                raise ValueError('Missing archive identity.')
            created, kickoff = instant(record['created_at']), instant(record['fixture']['timestamp'])
            if not created <= reading or not created < kickoff:
                raise ValueError('Invalid capture clock.')
            before = date.fromisoformat(record['before'])
            if before > created.date() or before > kickoff.date():
                raise ValueError('History cutoff is too late.')
            ids = []
            for field in ('match_id', 'league', 'home_id', 'away_id'):
                raw = record['fixture'][field]
                token = str(raw)
                if isinstance(raw, bool) or not token.isascii() or not token.isdigit() or int(token) < 1:
                    raise ValueError('Invalid provider identity.')
                ids.append(str(int(token)))
            if str(record['fixture']['home_id']) == str(record['fixture']['away_id']):
                raise ValueError('Identical teams.')
            clubs = distribution(record['probabilities'])
            fixtures[(ids[0], MODEL, ENGINE)].append((created, record['id'], record, clubs))
            valid_captures += 1
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            excluded['invalid_snapshot'] += 1
    first = [min(captures, key=lambda capture: capture[:2]) for captures in fixtures.values()]
    first.sort(key=lambda capture: capture[:2])
    start = instant(protocol['start_at']) if protocol else None
    rows = []
    for created, forecast_id, record, clubs in first:
        kickoff = instant(record['fixture']['timestamp'])
        if start and (created < start or kickoff < start):
            excluded['before_protocol_start'] += 1
            continue
        if protocol and record.get('model_code_sha256') != protocol.get('model_code_sha256'):
            excluded['incompatible_model_code'] += 1
            continue
        try:
            frequency = distribution(record.get('baseline'))
        except ValueError:
            frequency = None
        try:
            state, actual = own_result(record, reading)
        except (ValueError, TypeError, KeyError, AttributeError):
            state, actual = 'unresolved', None
            excluded['invalid_result'] += 1
        rows.append(dict(match_id=str(int(record['fixture']['match_id'])), forecast_id=forecast_id,
                         created_at=created.isoformat(), kickoff=kickoff.isoformat(),
                         day=kickoff.astimezone(ZoneInfo('Africa/Kinshasa')).date().isoformat(),
                         week=(kickoff.date()-timedelta(days=kickoff.weekday())).isoformat(),
                         league=str(record['fixture']['league']), state=state, actual=actual,
                         clubs=clubs, frequency=frequency, paired=frequency is not None))
    evaluated = [row for row in rows if row['state'] == 'settled' and row['paired']]
    thresholds = []
    for cutoff in (0.5, 0.6, 0.7):
        selected = [row for row in evaluated if max(row['clubs'].values()) >= cutoff]
        thresholds.append(dict(threshold=cutoff, **summary(selected),
                               coverage=len(selected)/len(evaluated) if evaluated else None,
                               mean_confidence=math.fsum(max(row['clubs'].values()) for row in selected)/len(selected) if selected else None))
    calibration = []
    for method in METHODS:
        for label in LABELS:
            buckets = [[] for _ in range(10)]
            for row in evaluated:
                buckets[min(9, int(row[method][label]*10))].append(row)
            for index, members in enumerate(buckets):
                calibration.append(dict(method=method, label=label, lower=index/10, upper=(index+1)/10,
                                        matches=len(members),
                                        announced=math.fsum(row[method][label] for row in members)/len(members) if members else None,
                                        observed=sum(row['actual'] == label for row in members)/len(members) if members else None))
    daily = [dict(day=day, **summary([row for row in rows if row['day'] == day]))
             for day in sorted({row['day'] for row in rows}, reverse=True)]
    weekly = []
    for week in sorted({row['week'] for row in rows}):
        members = [row for row in rows if row['week'] == week]
        opening = instant(week+'T00:00:00+00:00')
        closed = opening+timedelta(days=7) <= reading and (start is None or opening >= start)
        complete = all(row['state'] == 'void' or (row['state'] == 'settled' and row['paired']) for row in members)
        weekly.append(dict(week=week, calendar_closed=closed, eligible=closed and complete, **summary(members)))
    return dict(version=VERSION, as_of=reading.isoformat(), scope='prospective' if protocol else 'descriptive',
                protocol_id=protocol['id'] if protocol else None, starts_at=protocol['start_at'] if protocol else None,
                **summary(rows), rows=rows, daily=daily, weekly=weekly, thresholds=thresholds,
                calibration=calibration, closed_weeks=sum(week['eligible'] for week in weekly),
                duplicates_removed=valid_captures-len(first), exclusions=dict(excluded), decision='no_automatic_promotion')


def differences(expected, actual, path='report'):
    """Check every reconstructed field; tolerate only floating-point roundoff."""
    errors = []
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [path+': expected an object']
        extra = set(actual)-set(expected)
        if path in ('descriptive', 'prospective'):
            extra.discard('limitations')  # Narrative text has no recomputed statistic.
        if extra:
            errors.append(path+': unexpected keys '+','.join(sorted(extra)))
        for key, value in expected.items():
            if key not in actual:
                errors.append(path+'.'+key+': missing')
            else:
                errors.extend(differences(value, actual[key], path+'.'+key))
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            return [path+': list length or type differs']
        for index, (left, right) in enumerate(zip(expected, actual)):
            errors.extend(differences(left, right, path+'['+str(index)+']'))
    elif isinstance(expected, float):
        if (isinstance(actual, bool) or not isinstance(actual, (int, float))
                or not math.isfinite(actual) or not math.isclose(expected, actual, rel_tol=1e-11, abs_tol=1e-12)):
            errors.append(path+': numeric value differs')
    elif (expected != actual or (isinstance(expected, bool) != isinstance(actual, bool))
          or (isinstance(expected, int) and not isinstance(expected, bool)
              and (not isinstance(actual, int) or isinstance(actual, bool)))):
        errors.append(path+': value differs')
    return errors


def verify(archive, report):
    if not isinstance(archive, list):
        raise ValueError('Source must be a JSON archive list.')
    required = {'as_of', 'protocol', 'compatibility', 'descriptive', 'prospective'}
    if not isinstance(report, dict) or not required.issubset(report):
        raise ValueError('Incomplete validation report envelope.')
    reading = instant(report['as_of'])
    protocol = report.get('protocol')
    errors = []
    if protocol is not None:
        unsigned = {key: value for key, value in protocol.items() if key != 'id'}
        canonical = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        expected_id = sha256(canonical.encode()).hexdigest()
        errors.extend(differences(expected_id, protocol.get('id'), 'protocol.id'))
        if (protocol.get('version') != VERSION or protocol.get('model_version') != MODEL
                or protocol.get('engine_version') != ENGINE or protocol.get('methods') != list(METHODS)
                or instant(protocol['created_at']) >= instant(protocol['start_at'])):
            errors.append('protocol: incompatible rules')
        changed = [name for name, digest in protocol['code_sha256'].items()
                   if not (ROOT/name).is_file() or sha256((ROOT/name).read_bytes()).hexdigest() != digest]
        artifact = ROOT/protocol['enriched']['artifact']
        if not artifact.is_file() or sha256(artifact.read_bytes()).hexdigest() != protocol['enriched']['sha256']:
            changed.append(protocol['enriched']['artifact'])
        errors.extend(differences(dict(compatible=not changed, changed=changed), report.get('compatibility'), 'compatibility'))
    elif report.get('compatibility') is not None:
        errors.append('compatibility: must be null without a protocol')
    scopes = {}
    for scope, plan in (('descriptive', None), ('prospective', protocol)):
        if scope == 'prospective' and protocol is None:
            if report.get(scope) is not None:
                errors.append('prospective: must be null without a protocol')
            continue
        rebuilt = project(archive, reading, plan)
        errors.extend(differences(rebuilt, report.get(scope), scope))
        cohort = '\n'.join(row['match_id']+'\t'+row['forecast_id'] for row in rebuilt['rows'])+'\n'
        scopes[scope] = dict(**summary(rebuilt['rows']), cohort_sha256=sha256(cohort.encode()).hexdigest(),
                             daily=len(rebuilt['daily']), weekly=len(rebuilt['weekly']),
                             thresholds=len(rebuilt['thresholds']), calibration_bins=len(rebuilt['calibration']))
    return dict(status='verified' if not errors else 'failed', independent=True,
                project_modules_imported=False, as_of=reading.isoformat(), source_records=len(archive),
                scopes=scopes, differences=errors)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--output', type=Path, help='Create this new verification JSON file exclusively.')
    args = parser.parse_args(argv)
    try:
        with (gzip.open(args.source, 'rt', encoding='utf-8') if args.source.suffix == '.gz'
              else args.source.open(encoding='utf-8')) as handle:
            archive = json.load(handle)
        result = verify(archive, json.loads(args.report.read_text(encoding='utf-8')))
        result.update(source=str(args.source), source_sha256=sha256(args.source.read_bytes()).hexdigest(),
                      report=str(args.report), report_sha256=sha256(args.report.read_bytes()).hexdigest())
        encoded = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+'\n'
        if args.output:
            with args.output.open('x', encoding='utf-8') as handle:
                handle.write(encoded)
        else:
            print(encoded, end='')
        return 0 if result['status'] == 'verified' else 1
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
        print(json.dumps(dict(status='error', kind=type(error).__name__, message=str(error)), ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
