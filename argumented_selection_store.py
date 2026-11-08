"""Immutable, prospective observations of the fixed personal selection rule.

Provider access belongs to the caller's quota-aware gate. This module only
accepts a fresh confirmation through ``refresh`` and reads indexed local
fixtures when reconciling results. It never trains or reconstructs selections.
"""
from collections import Counter, defaultdict
from contextlib import closing
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from math import fsum, isclose, log
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

import api_football as api
import api_match_workspace as calendar
import high_probability_archive as archive
import probability_implementation as implementation
from league_model import outcome, score
from halftime_domain import validate_probabilities


VERSION = 'argumented-selection-store-v1'
VALUES = ('1st', 'Égal', '2nd')
STATUSES = ('pending', 'settled', 'unresolved', 'void')
METHODS = ('selection', 'clubs', 'baseline')
ZONE = ZoneInfo('Africa/Kinshasa')
SELECTION_SOURCES = ('argumented_selection_engine.py', 'argumented_selection_store.py')


class SelectionStoreError(ValueError):
    pass


def _root(root=None):
    return Path(api.DATA_DIR if root is None else root).resolve()


def database_path(root=None):
    return _root(root) / 'argumented_selection.sqlite3'


def signature(root=None):
    """Read-only cache token, including uncheckpointed result revisions."""
    path = database_path(root)
    return tuple((str(item), item.stat().st_mtime_ns, item.stat().st_size)
                 for item in (path, Path(str(path) + '-wal')) if item.exists())


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _digest(value):
    return sha256(_json(value).encode()).hexdigest()


def _stamp(value):
    if not isinstance(value, str):
        raise SelectionStoreError('Un horodatage explicite est nécessaire.')
    try:
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise SelectionStoreError('Horodatage invalide.') from None
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None
            else value.astimezone(timezone.utc))


def _time(value):
    if (not isinstance(value, datetime) or value.tzinfo is None
            or value.utcoffset() is None):
        raise SelectionStoreError('Une horloge avec fuseau horaire est nécessaire.')
    return value.astimezone(timezone.utc)


def _now(root=None, clock=None, value=None):
    # An isolated test may supply an historical clock; production cannot.
    if _root(root) == Path(api.DATA_DIR).resolve():
        return datetime.now(timezone.utc)
    return _time(value if value is not None else clock() if clock else datetime.now(timezone.utc))


def _read_time(now=None):
    return _time(datetime.now(timezone.utc) if now is None else now)


def _day(value):
    try:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError:
        raise SelectionStoreError('Date de calendrier invalide.') from None
    return value


def _probabilities(value):
    try:
        return validate_probabilities(value)
    except ValueError:
        raise SelectionStoreError('Probabilités invalides ou non normalisées.') from None


def _fixture(value):
    fixture = archive._fixture(value)
    if type(fixture.get('season')) is not int or not 1900 <= fixture['season'] <= 2100:
        raise SelectionStoreError('La saison du match doit être confirmée.')
    return fixture


def _hashes(value, names):
    if (not isinstance(value, dict) or set(value) != set(names)
            or any(not isinstance(digest, str) or len(digest) != 64
                   or any(char not in '0123456789abcdef' for char in digest)
                   for digest in value.values())):
        raise SelectionStoreError('Empreintes de l’implémentation invalides.')
    return dict(value)


def _implementation():
    try:
        model = implementation.validate_hashes(implementation.code_hashes(), current=True)
        source_root = Path(__file__).resolve().parent
        selection = {name: sha256((source_root / name).read_bytes()).hexdigest()
                     for name in SELECTION_SOURCES}
        return dict(model_code_sha256=model, selection_code_sha256=selection,
                    implementation_sha256=dict(model, **selection))
    except (OSError, ValueError):
        raise SelectionStoreError('Impossible de vérifier le code de la sélection.') from None


def _identity(selection_version, match_id):
    return _digest([VERSION, selection_version, match_id])


def _procedure(value):
    if (not isinstance(value, dict) or not isinstance(value.get('selection_version'), str)
            or not value['selection_version'] or not isinstance(value.get('criteria'), dict)):
        raise SelectionStoreError('Les critères figés de sélection sont invalides.')
    try:
        model = implementation.validate_hashes(value.get('model_code_sha256'))
    except ValueError:
        raise SelectionStoreError('Empreintes de l’implémentation invalides.') from None
    selection = _hashes(value.get('selection_code_sha256'), SELECTION_SOURCES)
    if value.get('implementation_sha256') != dict(model, **selection):
        raise SelectionStoreError('La provenance du code de sélection est incohérente.')
    return value


def _validate(value, procedure):
    """Validate frozen contents without depending on a future engine version."""
    fixture = _fixture(value['fixture'])
    created, calculated = _stamp(value['created_at']), _stamp(value['calculated_at'])
    kickoff = _stamp(fixture['timestamp'])
    if (value.get('version') != VERSION or value.get('source') != 'API-Football'
            or value.get('id') != _identity(value['selection_version'], fixture['match_id'])
            or value.get('captured_at') != value['created_at']
            or value['day'] != kickoff.astimezone(ZONE).date().isoformat()
            or calculated > created or created >= kickoff
            or _day(value['before']) > min(created.date(), kickoff.date()).isoformat()):
        raise SelectionStoreError('Identité ou chronologie de sélection invalide.')
    for key in ('selection_version', 'criteria', 'model_code_sha256',
                'selection_code_sha256', 'implementation_sha256'):
        if value.get(key) != procedure.get(key):
            raise SelectionStoreError('La sélection diffère de sa procédure figée.')
    probabilities, baseline = _probabilities(value['probabilities']), _probabilities(value['baseline'])
    choice = value.get('choice')
    if (choice not in VALUES or choice != max(VALUES, key=probabilities.get)
            or choice != max(VALUES, key=baseline.get)
            or not isclose(value['selection_probability'], probabilities[choice], abs_tol=1e-12)
            or not isclose(value['baseline_probability'], baseline[choice], abs_tol=1e-12)
            or not isclose(value['delta_points'], (probabilities[choice] - baseline[choice]) * 100,
                           abs_tol=1e-10)):
        raise SelectionStoreError('La catégorie sélectionnée ou ses probabilités sont incohérentes.')
    confirmation = value.get('confirmation') or {}
    confirmed = _stamp(confirmation.get('checked_at'))
    raw = confirmation.get('raw')
    if (confirmation.get('provider') != 'api-football' or confirmation.get('status') != 'NS'
            or not calculated <= confirmed <= created
            or not isinstance(raw, dict) or confirmation.get('raw_sha256') != _digest(raw)):
        raise SelectionStoreError('La confirmation avant match est invalide.')
    _confirmed(fixture, raw, confirmed)
    original = value.get('engine_row')
    if (not isinstance(original, dict) or value.get('engine_row_sha256') != _digest(original)
            or _fixture(original['fixture']) != fixture):
        raise SelectionStoreError('La preuve calculée de sélection est altérée.')
    for key in ('choice', 'probabilities', 'baseline', 'selection_version', 'criteria',
                'selection_probability', 'baseline_probability', 'delta_points',
                'recent_home', 'recent_away', 'reasons'):
        if original.get(key) != value.get(key):
            raise SelectionStoreError('Les preuves de sélection sont contradictoires.')
    return value


def _connect(root=None, *, create=False):
    path = database_path(root)
    if not create and not path.exists():
        return None
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path.as_uri() + ('?mode=rwc' if create else '?mode=ro'),
                          uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    if create:
        con.executescript('''
            CREATE TABLE IF NOT EXISTS procedures(
                selection_version TEXT PRIMARY KEY, payload TEXT NOT NULL, digest TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS selections(
                id TEXT PRIMARY KEY, selection_version TEXT NOT NULL REFERENCES procedures(selection_version),
                match_id TEXT NOT NULL, day TEXT NOT NULL, created_at TEXT NOT NULL,
                payload TEXT NOT NULL, digest TEXT NOT NULL, UNIQUE(selection_version,match_id));
            CREATE TABLE IF NOT EXISTS results(
                selection_id TEXT NOT NULL REFERENCES selections(id), revision INTEGER NOT NULL,
                checked_at TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL,
                PRIMARY KEY(selection_id,revision));
            CREATE INDEX IF NOT EXISTS selection_day ON selections(day,match_id);
            CREATE INDEX IF NOT EXISTS selection_results ON results(selection_id,revision);
            CREATE TRIGGER IF NOT EXISTS procedure_no_update BEFORE UPDATE ON procedures
                BEGIN SELECT RAISE(ABORT,'Selection procedures are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS procedure_no_delete BEFORE DELETE ON procedures
                BEGIN SELECT RAISE(ABORT,'Selection procedures are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS selection_no_update BEFORE UPDATE ON selections
                BEGIN SELECT RAISE(ABORT,'Selections are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS selection_no_delete BEFORE DELETE ON selections
                BEGIN SELECT RAISE(ABORT,'Selections are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS selection_result_no_update BEFORE UPDATE ON results
                BEGIN SELECT RAISE(ABORT,'Selection results are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS selection_result_no_delete BEFORE DELETE ON results
                BEGIN SELECT RAISE(ABORT,'Selection results are append-only'); END;
        ''')
    else:
        con.execute('PRAGMA query_only=ON')
    return con


def _decode(row):
    if sha256(row['payload'].encode()).hexdigest() != row['digest']:
        raise SelectionStoreError('Un enregistrement de sélection a été modifié.')
    return json.loads(row['payload'])


def _confirmed(fixture, raw, current):
    if raw.get('provider') not in (None, 'api-football', 'API-Football'):
        raise SelectionStoreError('La source de confirmation a changé.')
    from fixture_import_policy import QUALITY_KEY
    if QUALITY_KEY in raw:
        raise SelectionStoreError('Un relevé local annoté ne confirme pas le fournisseur.')
    fresh = _fixture(calendar.fixture_from_raw(raw))
    if (any(str(fresh[key]) != str(fixture[key]) for key in
            ('match_id', 'league', 'home_id', 'away_id', 'season'))
            or _stamp(fresh['timestamp']) != _stamp(fixture['timestamp'])):
        raise SelectionStoreError('L’identité, la saison ou l’horaire a changé depuis le calcul.')
    if api._status_short(raw) != 'NS' or _stamp(fresh['timestamp']) <= current:
        raise SelectionStoreError('Le match n’est plus confirmé à venir.')
    return dict(provider='api-football', status='NS', checked_at=current.isoformat(),
                raw_sha256=_digest(raw), raw=deepcopy(raw))


def _counts(rows):
    counts = Counter(row['status'] for row in rows)
    return dict(matches=len(rows), **{status: counts[status] for status in STATUSES})


def capture(report, root=None, *, clock=None, refresh=None, check_time=None):
    """Capture every qualifying future row once after a fresh provider check.

    ``refresh(ids='1-2', timezone='UTC')`` is required and is called in batches
    of at most twenty. Provider and scheduler exceptions propagate unchanged.
    """
    import argumented_selection_engine as engine
    current = _now(root, clock)
    if (not isinstance(report, dict) or report.get('version') != engine.VERSION
            or report.get('source', 'API-Football') != 'API-Football'
            or report.get('criteria') != engine.CRITERIA or not isinstance(report.get('rows'), list)):
        raise SelectionStoreError('Un rapport de sélection de la procédure fixe est nécessaire.')
    day, before = _day(report.get('day')), _day(report.get('before'))
    if day < current.astimezone(ZONE).date().isoformat():
        raise SelectionStoreError('Une sélection passée ne peut pas être reconstruite.')
    calculated = _stamp(report['generated_at']) if report.get('generated_at') else current
    if calculated > current:
        raise SelectionStoreError('Le calcul de sélection est daté dans le futur.')
    output = dict(status='completed', created=0, existing=0, skipped=0, unavailable=0, issues=[])
    available = {(row['selection_version'], row['fixture']['match_id'])
                 for row in entries(root, now=current)}
    unique, conflicts = {}, set()
    for row in report['rows']:
        try:
            fixture = _fixture(row['fixture'])
            mid = fixture['match_id']
            if mid in unique:
                if _json(unique[mid]) != _json(row):
                    conflicts.add(mid)
                else:
                    output['skipped'] += 1
            else:
                unique[mid] = row
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
            output['skipped'] += 1
    pending = []
    for mid, row in sorted(unique.items(), key=lambda item: int(item[0])):
        if (engine.VERSION, mid) in available:
            output['existing'] += 1
            continue
        try:
            if mid in conflicts:
                raise SelectionStoreError('Plusieurs calculs contradictoires pour ce match.')
            engine.validate_row(row, day=day, before=before, now=current)
            if row.get('selection_version') != engine.VERSION or row.get('criteria') != engine.CRITERIA:
                raise SelectionStoreError('La ligne ne respecte pas la procédure fixe.')
            if _stamp(row['fixture']['timestamp']) <= current:
                raise SelectionStoreError('Le coup d’envoi est déjà passé.')
            pending.append(deepcopy(row))
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as error:
            output['skipped'] += 1
            if str(error) not in output['issues']:
                output['issues'].append(str(error))
    if not pending:
        return output
    if refresh is None:
        return dict(output, status='unavailable', unavailable=len(pending),
                    issues=output['issues'] + ['Une confirmation API récente est nécessaire avant capture.'])
    provenance = _implementation()
    procedure = _procedure(dict(selection_version=engine.VERSION,
                                criteria=deepcopy(engine.CRITERIA), **provenance))
    for start in range(0, len(pending), 20):
        if check_time:
            check_time()
        batch = pending[start:start + 20]
        fresh = refresh(ids='-'.join(row['fixture']['match_id'] for row in batch), timezone='UTC')
        # Validate provider error/paging envelopes before inspecting row data.
        if isinstance(fresh, dict):
            fresh = calendar._response_fixtures(fresh)
        if not isinstance(fresh, (list, tuple)):
            raise api.ApiFootballError('Liste de confirmations API-Football invalide.')
        indexed, conflict = {}, set()
        for raw in fresh:
            try:
                mid = api._numeric_id(raw['fixture']['id'], 'du match')
            except (ValueError, KeyError, TypeError, AttributeError):
                raise api.ApiFootballError('Confirmation API-Football invalide.') from None
            if mid in indexed and indexed[mid] != raw:
                conflict.add(mid)
            indexed[mid] = raw
        values = []
        for row in batch:
            mid = row['fixture']['match_id']
            try:
                confirmed = _now(root, clock)
                if confirmed < calculated:
                    raise SelectionStoreError('La confirmation précède le calcul.')
                if mid in conflict or mid not in indexed:
                    raise SelectionStoreError('Confirmation API absente ou contradictoire.')
                confirmation = _confirmed(_fixture(row['fixture']), indexed[mid], confirmed)
                values.append(dict(row=deepcopy(row), confirmation=confirmation))
            except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as error:
                output['unavailable'] += 1
                if str(error) not in output['issues']:
                    output['issues'].append(str(error))
        if not values:
            continue
        if check_time:
            check_time()
        try:
            with closing(_connect(root, create=True)) as con:
                con.execute('BEGIN IMMEDIATE')
                prior = con.execute('SELECT * FROM procedures WHERE selection_version=?',
                                    (engine.VERSION,)).fetchone()
                if prior is not None and _procedure(_decode(prior)) != procedure:
                    raise SelectionStoreError('Le code ou les critères ont changé sans nouvelle version de sélection.')
                if prior is None:
                    encoded = _json(procedure)
                    con.execute('INSERT INTO procedures VALUES(?,?,?)',
                                (engine.VERSION, encoded, sha256(encoded.encode()).hexdigest()))
                inserted = []
                for item in values:
                    row = item['row']
                    fixture = _fixture(row['fixture'])
                    if con.execute('SELECT 1 FROM selections WHERE selection_version=? AND match_id=?',
                                   (engine.VERSION, fixture['match_id'])).fetchone():
                        output['existing'] += 1
                        continue
                    captured = _now(root, clock)
                    if captured >= _stamp(fixture['timestamp']):
                        output['unavailable'] += 1
                        continue
                    value = dict(deepcopy(row), version=VERSION, source='API-Football',
                                 fixture=fixture, selection_version=engine.VERSION, day=day, before=before,
                                 created_at=captured.isoformat(), captured_at=captured.isoformat(),
                                 calculated_at=calculated.isoformat(), confirmation=item['confirmation'],
                                 engine_row=deepcopy(row), engine_row_sha256=_digest(row),
                                 **provenance)
                    value['id'] = _identity(engine.VERSION, fixture['match_id'])
                    _validate(value, procedure)
                    encoded = _json(value)
                    con.execute('INSERT INTO selections VALUES(?,?,?,?,?,?,?)',
                                (value['id'], engine.VERSION, fixture['match_id'], day,
                                 value['created_at'], encoded, sha256(encoded.encode()).hexdigest()))
                    inserted.append(value)
                if check_time:
                    check_time()
                final = _now(root, clock)
                if any(final < _stamp(value['created_at']) or
                       final >= _stamp(value['fixture']['timestamp']) for value in inserted):
                    raise SelectionStoreError('Un coup d’envoi est passé pendant la capture ; le lot est annulé.')
                con.commit()
                output['created'] += len(inserted)
        except (sqlite3.Error, OSError):
            raise SelectionStoreError('Impossible d’enregistrer la sélection locale.') from None
    return output


def _result(value):
    if value.get('status') not in STATUSES:
        raise SelectionStoreError('État de résultat invalide.')
    if value['status'] == 'settled':
        if outcome(score(value.get('ht')), score(value.get('ft'))) != value.get('actual'):
            raise SelectionStoreError('Le résultat ne correspond pas aux scores à 90 minutes.')
    elif any(value.get(key) is not None for key in ('ht', 'ft', 'actual')):
        raise SelectionStoreError('Une rencontre non réglée ne peut recevoir de verdict.')
    return value


def entries(root=None, now=None):
    """Read verified observations as of now without creating or updating files."""
    if not database_path(root).exists():
        return []
    current = _read_time(now)
    try:
        with closing(_connect(root)) as con:
            con.execute('BEGIN')
            procedures = {}
            for record in con.execute('SELECT * FROM procedures'):
                procedure = _procedure(_decode(record))
                if procedure['selection_version'] != record['selection_version']:
                    raise SelectionStoreError('Identité de procédure altérée.')
                procedures[record['selection_version']] = procedure
            output = []
            for row in con.execute('SELECT * FROM selections ORDER BY created_at,id'):
                value = _validate(_decode(row), procedures[row['selection_version']])
                if (any(value[key] != row[key] for key in ('id', 'selection_version', 'day', 'created_at'))
                        or value['fixture']['match_id'] != row['match_id']):
                    raise SelectionStoreError('Métadonnées de sélection altérées.')
                history, all_events = [], []
                for expected, event in enumerate(con.execute(
                        'SELECT * FROM results WHERE selection_id=? ORDER BY revision', (value['id'],)), 1):
                    result = _result(_decode(event))
                    checked = _stamp(event['checked_at'])
                    if event['revision'] != expected:
                        raise SelectionStoreError('Chronologie des résultats invalide.')
                    decorated = dict(result, revision=expected, checked_at=event['checked_at'])
                    all_events.append(decorated)
                    if checked <= current:
                        history.append(decorated)
                archive._validate_events(all_events, value)
                if _stamp(value['created_at']) > current:
                    continue
                state = history[-1] if history else dict(status='pending', actual=None, ht=None, ft=None,
                                                        issues=[], revision=0, checked_at=None)
                correct = value['choice'] == state['actual'] if state['status'] == 'settled' else None
                output.append(dict(value, **state, correct=correct, selected_correct=correct,
                                   result_history=history))
            return output
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError) as error:
        if isinstance(error, SelectionStoreError):
            raise
        raise SelectionStoreError('Impossible de lire une sélection archivée valide.') from None


def _settlement(entry, raw, current):
    if raw is not None:
        try:
            if str(raw['league']['season']) != str(entry['fixture']['season']):
                raise SelectionStoreError('La saison du résultat diffère de la sélection.')
        except (KeyError, TypeError):
            raise SelectionStoreError('La saison du résultat est absente.') from None
    state = archive._settlement(entry, raw, current)
    if state['status'] == 'pending' and current >= _stamp(entry['fixture']['timestamp']) + timedelta(hours=6):
        state = dict(status='unresolved', ht=None, ft=None, actual=None,
                     issues=['Résultat final absent plus de six heures après le coup d’envoi.'])
    return state


def reconcile(root=None, now=None):
    """Append local result corrections, using indexed fixture IDs and no API."""
    current = _now(root, value=now)
    saved = entries(root, now=current)
    if not saved:
        return dict(entries=0, changed=0, **{status: 0 for status in STATUSES})
    raw_fixtures = api.load_fixtures(_root(root) / 'fixtures.json',
                                   ids=sorted({row['fixture']['match_id'] for row in saved}, key=int))
    indexed, conflicts = {}, set()
    for raw in raw_fixtures:
        try:
            mid = api._numeric_id(raw['fixture']['id'], 'du match')
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if mid in indexed and indexed[mid] != raw:
            conflicts.add(mid)
        indexed[mid] = raw
    changed = 0
    try:
        with closing(_connect(root, create=True)) as con:
            con.execute('BEGIN IMMEDIATE')
            for entry in saved:
                mid = entry['fixture']['match_id']
                try:
                    if mid in conflicts:
                        raise SelectionStoreError('Relevés locaux contradictoires.')
                    state = _settlement(entry, indexed.get(mid), current)
                except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as error:
                    state = dict(status='unresolved', ht=None, ft=None, actual=None, issues=[str(error)])
                _result(state)
                encoded = _json(state)
                prior = con.execute('SELECT * FROM results WHERE selection_id=? ORDER BY revision DESC LIMIT 1',
                                    (entry['id'],)).fetchone()
                if prior is not None:
                    old = _result(_decode(prior))
                    if (current < _stamp(prior['checked_at']) or prior['payload'] == encoded
                            or old['status'] == 'settled' and state['status'] == 'pending'):
                        continue
                revision = prior['revision'] + 1 if prior else 1
                con.execute('INSERT INTO results VALUES(?,?,?,?,?)',
                            (entry['id'], revision, current.isoformat(), encoded,
                             sha256(encoded.encode()).hexdigest()))
                changed += 1
            con.commit()
    except (sqlite3.Error, OSError):
        raise SelectionStoreError('Impossible de rapprocher les résultats des sélections.') from None
    result = entries(root, now=current)
    return dict(entries=len(result), changed=changed,
                **{status: sum(row['status'] == status for row in result) for status in STATUSES})


def _first_matches(rows):
    selected = {}
    for row in sorted(rows, key=lambda item: (_stamp(item['created_at']), item['id'])):
        selected.setdefault(row['fixture']['match_id'], row)
    return list(selected.values())


def _metrics(rows, method):
    def chosen(row):
        return row['choice'] if method == 'selection' else max(
            VALUES, key=row['probabilities' if method == 'clubs' else 'baseline'].get)
    correct = sum(chosen(row) == row['actual'] for row in rows)
    result = dict(matches=len(rows), evaluated=len(rows), correct=correct,
                  accuracy=correct / len(rows) if rows else None)
    if method != 'selection':
        key = 'probabilities' if method == 'clubs' else 'baseline'
        zeros = sum(row[key][row['actual']] == 0 for row in rows)
        # A zero-probability realised category has infinite log loss. Expose
        # that fact explicitly without emitting Infinity into JSON or exports.
        result.update(zero_probability_actual=zeros, logloss_defined=bool(rows) and zeros == 0,
                      logloss=fsum(-log(row[key][row['actual']]) for row in rows) / len(rows)
                      if rows and not zeros else None,
                      brier=fsum(sum((row[key][label] - int(label == row['actual'])) ** 2
                                    for label in VALUES) for row in rows) / len(rows) if rows else None)
    return result


def _aggregate(rows):
    evaluated = [row for row in rows if row['status'] == 'settled']
    return dict(counts=_counts(rows), evaluated=len(evaluated),
                by_method={method: _metrics(evaluated, method) for method in METHODS})


def summary(root=None, now=None):
    """Descriptive accuracy on the same unique first matches for each method."""
    current = _read_time(now)
    rows = entries(root, now=current)
    first = _first_matches(rows)
    daily, versions = defaultdict(list), defaultdict(list)
    for row in first:
        daily[row['day']].append(row)
    for row in rows:
        versions[row['selection_version']].append(row)
    aggregate = _aggregate(first)
    return dict(version=VERSION, as_of=current.isoformat(), rows=rows, first_matches=first,
                captures=len(rows), **aggregate, metrics=aggregate['by_method'],
                by_day=[dict(day=day, **_aggregate(values)) for day, values in sorted(daily.items())],
                by_version=[dict(selection_version=version, **_aggregate(_first_matches(values)))
                            for version, values in sorted(versions.items())],
                interpretation='Descriptive prospective evidence only. Each fixture counts once, using its first '
                'capture across versions; all three methods use the same settled matches. Missing, postponed, '
                'unresolved and void matches are excluded from accuracy, never counted as losses. '
                'Repeated clubs and competitions remain dependent; this is not a validation or a gain guarantee.')
