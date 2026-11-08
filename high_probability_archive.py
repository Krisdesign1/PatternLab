"""Append-only, per-match snapshots of the entire probability ranking.

Captures contain only values calculated before kickoff. Result revisions are
separate and use API-Football's regulation-time scores, never a current model.
"""
from copy import deepcopy
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

import api_football as api
import api_match_workspace as calendar
import probability_implementation as implementation
from league_model import outcome, score
from match_predictor import VERSION as MODEL_VERSION
from halftime_domain import CLASS_ORDER as VALUES, validate_probabilities
import probability_time_evidence as time_evidence


VERSION = 'high-probability-match-archive-v1'
ZONE = ZoneInfo('Africa/Kinshasa')


class ArchiveError(ValueError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return sha256(_json(value).encode()).hexdigest()


def _root(root):
    return Path(api.DATA_DIR if root is None else root)


def database_path(root=None):
    return _root(root) / 'high_probability_archive.sqlite3'


def _stamp(value):
    value = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _clock(root, clock=None):
    # An injected historical clock is useful in isolated tests. Production
    # captures always use the real wall clock, regardless of caller input.
    value = (datetime.now(timezone.utc) if _root(root).resolve() == Path(api.DATA_DIR).resolve()
             or clock is None else clock())
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ArchiveError('Une horloge avec fuseau horaire est nécessaire.')
    return value.astimezone(timezone.utc)


def _identifier(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ArchiveError('Identifiant API-Football invalide.')
    value = str(value)
    if not value.isascii() or not value.isdecimal() or int(value) <= 0:
        raise ArchiveError('Identifiant API-Football invalide.')
    return str(int(value))


def _probabilities(value):
    try:
        return validate_probabilities(value)
    except ValueError as error:
        raise ArchiveError(str(error)) from None


def _fixture(value):
    if not isinstance(value, dict) or value.get('provider') != 'api-football':
        raise ArchiveError('Une affiche API-Football confirmée est nécessaire.')
    fixture = {key: _identifier(value.get(key)) for key in ('match_id', 'league', 'home_id', 'away_id')}
    if fixture['home_id'] == fixture['away_id']:
        raise ArchiveError('Les identifiants des deux clubs doivent être distincts.')
    for field in ('home', 'away'):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise ArchiveError('Les noms des clubs sont absents.')
        fixture[field] = value[field]
    fixture.update(provider='api-football', timestamp=_stamp(value['timestamp']).isoformat())
    for field in ('competition', 'country', 'season'):
        if value.get(field) is not None:
            fixture[field] = deepcopy(value[field])
    return fixture


def _identity(value):
    return _digest({key: item for key, item in value.items() if key not in ('id', 'created_at')})


def _content(value):
    """Probability snapshot contents, excluding its temporal chain anchor."""
    result={key: item for key, item in value.items()
            if key not in ('id', 'created_at', 'previous_snapshot', 'temporal_provenance')}
    if 'temporal_provenance' in value:
        result['temporal_provenance']=time_evidence.content_key(value['temporal_provenance'])
    return result


def _validate(value):
    if (value.get('version') != VERSION or value.get('source') != 'API-Football'
            or not isinstance(value.get('model_version'), str) or not value['model_version']
            or not isinstance(value.get('engine_version'), str) or not value['engine_version']
            or not isinstance(value.get('history_fingerprint'), str) or not value['history_fingerprint']):
        raise ArchiveError('La provenance de la prévision archivée est invalide.')
    if 'model_code_sha256' in value:
        try:
            implementation.validate_hashes(value['model_code_sha256'])
        except ValueError as error:
            raise ArchiveError(str(error)) from None
    if 'previous_snapshot' in value:
        previous = value['previous_snapshot']
        if (not isinstance(previous, str) or len(previous) != 64
                or any(char not in '0123456789abcdef' for char in previous)):
            raise ArchiveError('L’ancre chronologique de la prévision est invalide.')
    fixture = _fixture(value['fixture'])
    created, kickoff = _stamp(value['created_at']), _stamp(fixture['timestamp'])
    if (value['day'] != kickoff.astimezone(ZONE).date().isoformat() or created >= kickoff
            or created.astimezone(ZONE).date().isoformat() > value['day']):
        raise ArchiveError('La prévision n’est pas antérieure au match de cette journée.')
    before = value['before']
    if date.fromisoformat(before).isoformat() != before or before > min(created.date(), kickoff.date()).isoformat():
        raise ArchiveError('La coupure historique de la prévision est invalide.')
    if 'temporal_provenance' in value:
        time_evidence.validate_saved(value['temporal_provenance'],created_at=value['created_at'],
            before=before,league=fixture['league'],history_fingerprint=value['history_fingerprint'])
    _probabilities(value['probabilities'])
    if value.get('baseline') is not None:
        _probabilities(value['baseline'])
    for key, minimum in (('training', 30), ('history_days', 3)):
        if type(value.get(key)) is not int or value[key] < minimum:
            raise ArchiveError('Les effectifs historiques ne permettent pas ce calcul.')
    for side in ('home_profile', 'away_profile'):
        if not isinstance(value.get(side), dict) or type(value[side].get('matches')) is not int or value[side]['matches'] < 3:
            raise ArchiveError('Les précédents des clubs sont insuffisants.')
    if value.get('id') != _identity(value):
        raise ArchiveError('L’identité de la prévision archivée est invalide.')
    return value


def _snapshot(report, row, current, *, isolated_clock=False):
    fixture = row.get('fixture') or {}
    if (fixture.get('raw_status', 'NS') != 'NS' or fixture.get('status', 'TIMED') not in ('NS', 'TIMED')
            or fixture.get('is_upcoming', True) is not True or row.get('personalized') is not True):
        raise ArchiveError('Seules les rencontres confirmées à venir sont archivées.')
    fixture = _fixture(fixture)
    if _stamp(fixture['timestamp']) <= current:
        return None
    value = dict(version=VERSION, source='API-Football', model_version=MODEL_VERSION,
        engine_version=report.get('version'), created_at=current.isoformat(), day=report['day'],
        fixture=fixture, before=row.get('before', report.get('before')),
        history_fingerprint=row.get('history_fingerprint') or report.get('history_fingerprint'),
        probabilities=_probabilities(row.get('probabilities')),
        baseline=_probabilities(row['baseline']) if row.get('baseline') is not None else None,
        training=row.get('training'), history_days=row.get('history_days'),
        home_profile=deepcopy(row.get('home_profile')), away_profile=deepcopy(row.get('away_profile')))
    value['temporal_provenance']=time_evidence.qualify(report,row,current,isolated_clock=isolated_clock)
    value['id'] = _identity(value)
    return _validate(value)


def _connect(root=None, *, create=False):
    path = database_path(root)
    if not create:
        if not path.exists():
            return None
        con = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    if create:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS forecasts (
            id TEXT PRIMARY KEY, match_id TEXT NOT NULL, day TEXT NOT NULL,
            created_at TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS forecasts_day ON forecasts(day,match_id,created_at);
        CREATE TABLE IF NOT EXISTS results (
            forecast_id TEXT NOT NULL, revision INTEGER NOT NULL, checked_at TEXT NOT NULL,
            payload TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(forecast_id,revision));
        CREATE TRIGGER IF NOT EXISTS forecasts_no_update BEFORE UPDATE ON forecasts
            BEGIN SELECT RAISE(ABORT, 'Forecasts are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS forecasts_no_delete BEFORE DELETE ON forecasts
            BEGIN SELECT RAISE(ABORT, 'Forecasts are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS results_no_update BEFORE UPDATE ON results
            BEGIN SELECT RAISE(ABORT, 'Results are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS results_no_delete BEFORE DELETE ON results
            BEGIN SELECT RAISE(ABORT, 'Results are append-only'); END;
        ''')
    return con


def capture(report, root=None, clock=None):
    """Archive every calculable scan row, independently of filters and groups."""
    current = _clock(root, clock)
    if (not isinstance(report, dict) or report.get('source') != 'API-Football'
            or not isinstance(report.get('day'), str)
            or report['day'] < current.astimezone(ZONE).date().isoformat()
            or not isinstance(report.get('rows'), list)):
        raise ArchiveError('Seul un classement API-Football avant match, à une date à venir, peut être archivé.')
    try:
        if date.fromisoformat(report['day']).isoformat() != report['day']:
            raise ValueError
    except ValueError:
        raise ArchiveError('La date du classement à archiver est invalide.') from None
    values, skipped = [], 0
    for row in report['rows']:
        try:
            value = _snapshot(report, row, current,
                isolated_clock=clock is not None and _root(root).resolve()!=Path(api.DATA_DIR).resolve())
            if value is not None:
                values.append(value)
            else:
                skipped += 1
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
            skipped += 1
    # Conflicting rows for the same provider fixture are not two forecasts.
    # Reject the entire identity for this capture, rather than choosing by
    # input order or giving contradictory probabilities the same timestamp.
    groups = {}
    for value in values:
        groups.setdefault(value['fixture']['match_id'], []).append(value)
    values = []
    for candidates in groups.values():
        if len({value['id'] for value in candidates}) != 1:
            skipped += len(candidates)
        else:
            values.append(candidates[0])
            skipped += len(candidates)-1
    if not values:
        return dict(created=0, existing=0, skipped=skipped)
    # Hash our own source files once for the validated batch. A caller-supplied
    # map cannot attest which implementation produced a genuine new capture.
    try:
        model_code = implementation.validate_hashes(implementation.code_hashes(), current=True)
    except (OSError, ValueError):
        raise ArchiveError('Impossible de vérifier l’implémentation des probabilités à archiver.') from None
    for value in values:
        value['model_code_sha256'] = dict(model_code)
        value['id'] = _identity(value)
        _validate(value)
    con = None
    created = existing = 0
    inserted = []
    try:
        con = _connect(root, create=True)
        con.execute('BEGIN IMMEDIATE')
        for value in values:
            present = con.execute('SELECT * FROM forecasts WHERE match_id=? AND day=? '
                                  'ORDER BY created_at DESC,id DESC LIMIT 1',
                                  (value['fixture']['match_id'], value['day'])).fetchone()
            previous = None
            if present:
                previous = _read_forecast(present)
                if _content(previous) == _content(value):
                    existing += 1
                    continue
            actual = _clock(root, clock)
            if actual.astimezone(ZONE).date().isoformat() > value['day'] or actual >= _stamp(value['fixture']['timestamp']):
                skipped += 1
                continue
            if previous is not None:
                if actual <= _stamp(previous['created_at']):
                    raise ArchiveError('L’horloge doit avancer avant de conserver un nouveau calcul différent.')
                value['previous_snapshot'] = previous['id']
            value['created_at'] = actual.isoformat()
            value['id'] = _identity(value)
            _validate(value)
            encoded = _json(value)
            con.execute('INSERT INTO forecasts VALUES(?,?,?,?,?,?)', (value['id'], value['fixture']['match_id'],
                value['day'], value['created_at'], encoded, sha256(encoded.encode()).hexdigest()))
            inserted.append(value)
            created += 1
        final = _clock(root, clock)
        if any(final >= _stamp(value['fixture']['timestamp']) or
               final.astimezone(ZONE).date().isoformat() > value['day'] for value in inserted):
            raise ArchiveError('Un coup d’envoi est passé pendant l’archivage ; le lot n’est pas enregistré.')
        con.commit()
        return dict(created=created, existing=existing, skipped=skipped)
    except (sqlite3.Error, OSError):
        raise ArchiveError('Impossible d’archiver le classement local des probabilités.') from None
    finally:
        if con is not None:
            con.close()


def _read_forecast(row):
    if sha256(row['payload'].encode()).hexdigest() != row['digest']:
        raise ArchiveError('Une prévision archivée a été modifiée.')
    value = _validate(json.loads(row['payload']))
    if any(value[key] != row[key] for key in ('id', 'created_at', 'day')) or value['fixture']['match_id'] != row['match_id']:
        raise ArchiveError('Une identité archivée a été modifiée.')
    return value


def _read_result(row):
    if sha256(row['payload'].encode()).hexdigest() != row['digest']:
        raise ArchiveError('Un résultat archivé a été modifié.')
    value = json.loads(row['payload'])
    if value.get('status') not in ('pending', 'settled', 'unresolved', 'void'):
        raise ArchiveError('État de résultat archivé invalide.')
    if value['status'] == 'settled':
        if outcome(score(value.get('ht')), score(value.get('ft'))) != value.get('actual'):
            raise ArchiveError('Scores et résultat archivés contradictoires.')
    elif any(value.get(key) is not None for key in ('ht', 'ft', 'actual')):
        raise ArchiveError('Un résultat non final ne peut pas recevoir de verdict.')
    if type(row['revision']) is not int or row['revision'] < 1:
        raise ArchiveError('La révision du résultat est invalide.')
    return dict(value, revision=row['revision'], checked_at=row['checked_at'])


def _validate_events(events, entry):
    previous = _stamp(entry['created_at'])
    fixture = entry['fixture']
    for revision, event in enumerate(events, 1):
        checked = _stamp(event['checked_at'])
        if event['revision'] != revision or checked < previous:
            raise ArchiveError('La chronologie des résultats archivés est invalide.')
        if event['status'] == 'settled':
            original = _stamp(fixture['timestamp'])
            audit = event.get('schedule_resolution')
            effective = original
            if audit is not None:
                if (not isinstance(audit, dict) or audit.get('original_timestamp') != fixture['timestamp']
                        or audit.get('forecast_created_at') != entry['created_at']
                        or audit.get('raw_status') not in api.FINISHED_STATUSES):
                    raise ArchiveError('La correction horaire archivée est invalide.')
                effective = _stamp(audit['effective_timestamp'])
                if (_stamp(entry['created_at']) >= min(original, effective)
                        or entry['before'] > min(original, effective).date().isoformat()):
                    raise ArchiveError('La correction horaire contredit la prévision avant match.')
            if checked < effective:
                raise ArchiveError('Résultat enregistré avant le coup d’envoi effectif.')
        previous = checked


def list_entries(root=None):
    """Read verified snapshots and their last result revision; create nothing."""
    con = None
    try:
        con = _connect(root)
        if con is None:
            return []
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not tables:
            return []
        if not {'forecasts', 'results'}.issubset(tables):
            raise ArchiveError('Schéma de l’archive des probabilités invalide.')
        output = []
        for row in con.execute('SELECT * FROM forecasts ORDER BY created_at,id'):
            value = _read_forecast(row)
            events = [_read_result(event) for event in con.execute(
                'SELECT * FROM results WHERE forecast_id=? ORDER BY revision', (value['id'],))]
            _validate_events(events, value)
            state = events[-1] if events else dict(status='pending', ht=None, ft=None, actual=None,
                                                  issues=[], revision=0, checked_at=None)
            output.append(dict(value, **state, result_history=events))
        return output
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError) as error:
        if isinstance(error, ArchiveError):
            raise
        raise ArchiveError('Impossible de lire une archive de probabilités valide.') from None
    finally:
        if con is not None:
            con.close()


def latest(rows):
    """Keep the last genuine pre-match snapshot for each fixture and local day."""
    selected = {}
    for row in sorted(rows, key=lambda value: (_stamp(value['created_at']), value['id'])):
        if _stamp(row['created_at']) >= _stamp(row['fixture']['timestamp']):
            continue
        selected[(row['fixture']['match_id'], row['day'])] = deepcopy(row)
    return sorted(selected.values(), key=lambda value: (value['day'], value['fixture']['timestamp'],
                                                       int(value['fixture']['match_id'])), reverse=True)


def _settlement(entry, item, current):
    empty = dict(status='pending', ht=None, ft=None, actual=None, issues=[])
    if item is None:
        return dict(empty, status='unresolved', issues=['Match absent du relevé API-Football local.'])
    try:
        if item.get('provider') not in (None, 'api-football', 'API-Football'):
            raise ValueError('La source du résultat a changé.')
        actual = calendar.fixture_from_raw(item)
        fixture = entry['fixture']
        if any(str(actual[key]) != str(fixture[key]) for key in ('match_id', 'league', 'home_id', 'away_id')):
            raise ValueError('Les identifiants des clubs ou de la compétition ont changé.')
        short = api._status_short(item)
        if short in ('CANC', 'ABD', 'AWD', 'WO'):
            return dict(empty, status='void', issues=['Match annulé, abandonné ou attribué.'])
        if short in ('PST', 'SUSP', 'INT'):
            return dict(empty, status='unresolved', issues=['Match reporté ou interrompu.'])
        original, effective = _stamp(fixture['timestamp']), _stamp(actual['timestamp'])
        if short not in api.FINISHED_STATUSES:
            if original != effective:
                raise ValueError('Horaire modifié : résultat final confirmé nécessaire.')
            return empty
        if current < effective:
            raise ValueError('Résultat annoncé avant le coup d’envoi.')
        audit = None
        if original != effective:
            if (_stamp(entry['created_at']) >= min(original, effective)
                    or entry['before'] > min(original, effective).date().isoformat()):
                raise ValueError('Prévision incompatible avec le nouvel horaire.')
            audit = dict(original_timestamp=fixture['timestamp'], effective_timestamp=actual['timestamp'],
                         forecast_created_at=entry['created_at'], raw_status=short)
        result = api.normalize_fixture(item)
        return dict(status='settled', ht=result['ht'], ft=result['ft'], actual=result['value'],
                    issues=[], schedule_resolution=audit)
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as error:
        return dict(empty, status='unresolved', issues=[str(error)])


def reconcile(raw_fixtures=None, root=None, now=None):
    """Append only changed observations; no API request, fit or new forecast."""
    entries = list_entries(root)
    if not entries:
        return dict(entries=0, changed=0, pending=0, settled=0, unresolved=0, void=0)
    current = _clock(root, (lambda: now) if now is not None else None)
    raw_fixtures = (api.load_fixtures(_root(root)/'fixtures.json',
        ids=sorted({entry['fixture']['match_id'] for entry in entries}, key=int))
        if raw_fixtures is None else raw_fixtures)
    indexed, conflicts = {}, set()
    for item in raw_fixtures:
        mid = str(item.get('fixture', {}).get('id'))
        if mid in indexed and indexed[mid] != item:
            conflicts.add(mid)
        indexed[mid] = item
    con = None
    changed = 0
    try:
        con = _connect(root, create=True)
        con.execute('BEGIN IMMEDIATE')
        for entry in entries:
            mid = entry['fixture']['match_id']
            state = (_settlement(entry, indexed.get(mid), current) if mid not in conflicts else
                     dict(status='unresolved', ht=None, ft=None, actual=None, issues=['Relevés API contradictoires.']))
            previous = con.execute('SELECT * FROM results WHERE forecast_id=? ORDER BY revision DESC LIMIT 1',
                                   (entry['id'],)).fetchone()
            encoded = _json(state)
            if previous:
                old = _read_result(previous)
                if (current < _stamp(previous['checked_at']) or previous['payload'] == encoded
                        or old['status'] == 'settled' and state['status'] == 'pending'):
                    continue
            revision = previous['revision'] + 1 if previous else 1
            con.execute('INSERT INTO results VALUES(?,?,?,?,?)', (entry['id'], revision, current.isoformat(),
                                                                 encoded, sha256(encoded.encode()).hexdigest()))
            changed += 1
        con.commit()
    except (sqlite3.Error, OSError):
        raise ArchiveError('Impossible de rapprocher les résultats des probabilités archivées.') from None
    finally:
        if con is not None:
            con.close()
    result = list_entries(root)
    return dict(entries=len(result), changed=changed,
                **{state: sum(row['status'] == state for row in result)
                   for state in ('pending', 'settled', 'unresolved', 'void')})
