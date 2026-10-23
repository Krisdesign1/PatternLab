"""Append-only user scenario choices linked to immutable forecast snapshots.

Several choices may refer to one forecast. They never change that forecast or
become additional independent observations in a model performance calculation.
Only the already frozen Clubs distribution supplies probabilities and baselines.
The caller performs the fresh API-Football status preflight before ``record``.
"""

import cloud_storage
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import isclose, isfinite, prod
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

import api_football as api
import prediction_journal as journal
import prospective_models as models
from league_model import VALUES


VERSION = 'probability-scenario-intent-v1'
POLICY = 'high-probability-v3'
ZONE = ZoneInfo('Africa/Kinshasa')


class ScenarioStoreError(ValueError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _digest(value):
    return sha256(_json(value).encode()).hexdigest()


def _path(path):
    return Path(path) if path is not None else Path(api.DATA_DIR)/'probability_scenarios.sqlite3'


def _now(clock):
    value = datetime.now(timezone.utc) if clock is None else clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ScenarioStoreError('Une horloge avec fuseau horaire est nécessaire.')
    return value.astimezone(timezone.utc)


def _distribution(value):
    """Validate a frozen, ordinary three-outcome probability distribution."""
    if (not isinstance(value, dict) or set(value) != set(VALUES)
            or any(isinstance(p, bool) or not isinstance(p, (int, float))
                   or not isfinite(p) or not 0 <= p <= 1 for p in value.values())
            or not isclose(sum(value.values()), 1, rel_tol=1e-10, abs_tol=1e-12)):
        raise ScenarioStoreError('Une distribution figée par match est invalide.')
    return {label: value[label] for label in VALUES}


def _positions(method, size):
    distribution = method['distribution']
    if distribution['type'] == 'independent':
        positions = [_distribution(row) for row in distribution['distributions']]
        estimates = method.get('club_estimates')
        details = estimates if isinstance(estimates, list) and len(estimates) == size else [{} for _ in range(size)]
        return positions, details
    if distribution['type'] != 'block_product':
        raise ScenarioStoreError('Ce constructeur nécessite les probabilités Clubs indépendantes figées.')
    positions, details = [None]*size, [None]*size
    for component in distribution['components']:
        child = component['method']
        if child['distribution']['type'] != 'independent':
            raise ScenarioStoreError('Chaque compétition doit conserver ses probabilités Clubs indépendantes.')
        estimates = child.get('club_estimates')
        indices = component['indices']
        estimates = estimates if isinstance(estimates, list) and len(estimates) == len(indices) else [{} for _ in indices]
        for index, p, estimate in zip(indices, child['distribution']['distributions'], estimates):
            positions[index], details[index] = _distribution(p), estimate
    return positions, details


def _snapshot(entry):
    if not isinstance(entry, dict) or entry.get('source') != 'API-Football':
        raise ScenarioStoreError('Seules les prévisions API-Football peuvent recevoir un scénario.')
    fixtures = journal._fixtures(entry.get('fixtures', []), allow_mixed=True)
    size = len(fixtures)
    if entry.get('size') != size or entry.get('id') != journal._group_key(fixtures):
        raise ScenarioStoreError('L’identité de la prévision ne correspond pas aux matchs.')
    created = datetime.fromisoformat(entry['created_at'].replace('Z', '+00:00'))
    if created.tzinfo is None or created.utcoffset() is None:
        raise ScenarioStoreError('La date de la prévision doit préciser son fuseau horaire.')
    if any(created >= journal._stamp(row['timestamp']) for row in fixtures):
        raise ScenarioStoreError('La prévision doit avoir été enregistrée avant chaque coup d’envoi.')
    methods = entry.get('methods')
    if not isinstance(methods, dict) or 'clubs' not in methods or set(methods)-set(models.METHODS):
        raise ScenarioStoreError('La prévision Clubs figée est absente.')
    for method in methods.values():
        models.validate_method(method, size)
    method = methods['clubs']
    leagues = sorted({row['league'] for row in fixtures}, key=int)
    if method.get('source') != 'API-Football':
        raise ScenarioStoreError('La provenance Clubs doit être API-Football.')
    if method['distribution']['type'] == 'block_product':
        journal._mixed_provenance(method, fixtures, fixtures[0]['timestamp'][:10], leagues)
    elif len(leagues) != 1 or str(method.get('league')) != leagues[0]:
        raise ScenarioStoreError('La compétition de la prévision Clubs est incohérente.')
    positions, details = _positions(method, size)
    frozen = {key: deepcopy(entry.get(key)) for key in
              ('id', 'created_at', 'source', 'size', 'fixtures', 'methods', 'metadata')}
    return fixtures, positions, details, _digest(frozen), created.astimezone(timezone.utc)


def forecast_digest(entry):
    """Verify and identify the frozen forecast, excluding result revisions."""
    try:
        return _snapshot(entry)[3]
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        if isinstance(error, ScenarioStoreError):
            raise
        raise ScenarioStoreError('La prévision liée au choix est invalide.') from None


def _check_time(fixtures, current, forecast_created):
    if current < forecast_created:
        raise ScenarioStoreError('Le scénario ne peut pas précéder la prévision enregistrée.')
    day = current.astimezone(ZONE).date()
    if any(journal._stamp(row['timestamp']) <= current for row in fixtures):
        raise ScenarioStoreError('Un coup d’envoi est passé : nouveau choix non enregistré.')
    if any(journal._stamp(row['timestamp']).astimezone(ZONE).date() != day for row in fixtures):
        raise ScenarioStoreError('Les nouveaux choix concernent uniquement les matchs du jour à Kinshasa.')


def _make_selection(entry, selection, fixtures, positions, details):
    if not isinstance(selection, dict):
        raise ScenarioStoreError('Le scénario choisi est absent.')
    requested = selection.get('requested_scenario')
    sequence = requested.get('sequence') if isinstance(requested, dict) else None
    if (not isinstance(sequence, list) or len(sequence) != len(fixtures)
            or any(label not in VALUES for label in sequence)):
        raise ScenarioStoreError('Un résultat choisi est nécessaire pour chaque match du groupe.')
    metadata = entry.get('metadata') or {}
    origin = metadata.get('selection') or {}
    saved_rows = origin.get('match_probabilities') or []
    if not isinstance(saved_rows, list):
        raise ScenarioStoreError('Les références de la prévision sont invalides.')
    saved = {}
    for row in saved_rows:
        if not isinstance(row, dict) or not isinstance(row.get('match_id'), str) or row['match_id'] in saved:
            raise ScenarioStoreError('Les références de la prévision sont ambiguës.')
        saved[row['match_id']] = row
    rows = []
    for fixture, probabilities, detail, label in zip(fixtures, positions, details, sequence):
        original = saved.get(fixture['match_id'], {})
        if original.get('probabilities') is not None:
            recorded = _distribution(original['probabilities'])
            if any(not isclose(recorded[key], probabilities[key], rel_tol=1e-10, abs_tol=1e-12) for key in VALUES):
                raise ScenarioStoreError('Les probabilités du classement contredisent la prévision figée.')
        baseline = original.get('baseline')
        if baseline is None and isinstance(detail, dict):
            baseline = detail.get('baseline')
        baseline = _distribution(baseline) if baseline is not None else None
        choice = max(VALUES, key=probabilities.get)
        rows.append(dict(match_id=fixture['match_id'], probabilities=probabilities,
                         baseline=baseline, choice=choice, max_probability=probabilities[choice],
                         search_outcome=label, search_probability=probabilities[label],
                         delta_points=100*(probabilities[label]-baseline[label]) if baseline is not None else None))
    labels = set(sequence)
    outcome = sequence[0] if len(labels) == 1 else 'mixed'
    query = selection.get('search') or {}
    search = {key: deepcopy(query[key]) for key in ('minimum', 'query') if key in query}
    search['outcome'] = outcome
    if 'minimum' in search:
        value = search['minimum']
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not isfinite(value) or not 0 <= value <= 1):
            raise ScenarioStoreError('Le seuil de recherche doit être compris entre zéro et un.')
    return dict(policy=POLICY, searched_outcome=outcome, ranking_metric='chosen_clubs_probability',
                requested_scenario=dict(sequence=list(sequence), probability=models.probability_for_sequence(
                    entry['methods']['clubs'], sequence)), match_probabilities=rows, search=search,
                minimum_match_probability=min(row['search_probability'] for row in rows),
                maximum_match_probability=max(row['search_probability'] for row in rows),
                before=origin.get('before', entry['methods']['clubs'].get('history_cutoff_exclusive')),
                reason='Résultats choisis pour chaque match, toutes compétitions confondues. '
                       'Les probabilités proviennent de la prévision Clubs déjà figée, '
                       'sous une hypothèse d’indépendance des rencontres.')


def _identity(entry_id, rows):
    return _digest([entry_id, sorted((row['match_id'], row['search_outcome']) for row in rows)])


def _connect(path, *, create=False):
    if not create:
        if not path.exists():
            return None
        con = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=15)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    if create:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS intents (
            id TEXT PRIMARY KEY, entry_id TEXT NOT NULL, created_at TEXT NOT NULL,
            payload TEXT NOT NULL, digest TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS intents_no_update BEFORE UPDATE ON intents
            BEGIN SELECT RAISE(ABORT, 'Scenario choices are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS intents_no_delete BEFORE DELETE ON intents
            BEGIN SELECT RAISE(ABORT, 'Scenario choices are immutable'); END;
        ''')
    return con


def _read(row):
    if sha256(row['payload'].encode()).hexdigest() != row['digest']:
        raise ScenarioStoreError('Un choix enregistré a été modifié : historique invalide.')
    item = json.loads(row['payload'])
    if (item.get('version') != VERSION or item.get('id') != row['id']
            or item.get('entry_id') != row['entry_id'] or item.get('created_at') != row['created_at']
            or not isinstance(item.get('forecast_digest'), str) or len(item['forecast_digest']) != 64):
        raise ScenarioStoreError('L’identité du choix enregistré est invalide.')
    selection = item['selection']
    rows = selection['match_probabilities']
    sequence = selection['requested_scenario']['sequence']
    if (selection.get('policy') != POLICY or not isinstance(rows, list) or not 2 <= len(rows) <= 20
            or len(rows) != len(sequence) or any(label not in VALUES for label in sequence)
            or len({value['match_id'] for value in rows}) != len(rows)
            or _identity(item['entry_id'], rows) != item['id']):
        raise ScenarioStoreError('Le scénario enregistré est invalide.')
    for row, label in zip(rows, sequence):
        probabilities = _distribution(row['probabilities'])
        if row.get('search_outcome') != label or row.get('search_probability') != probabilities[label]:
            raise ScenarioStoreError('Les résultats choisis contredisent les probabilités figées.')
        baseline = _distribution(row['baseline']) if row.get('baseline') is not None else None
        delta = 100*(probabilities[label]-baseline[label]) if baseline is not None else None
        if row.get('delta_points') != delta:
            raise ScenarioStoreError('La comparaison historique enregistrée est invalide.')
    p = selection['requested_scenario']['probability']
    if (isinstance(p, bool) or not isinstance(p, (int, float)) or not isfinite(p)
            or not isclose(p, prod(value['search_probability'] for value in rows), rel_tol=1e-9, abs_tol=1e-12)):
        raise ScenarioStoreError('La probabilité du scénario enregistré est invalide.')
    stamp = datetime.fromisoformat(item['created_at'].replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ScenarioStoreError('La date du choix enregistré est invalide.')
    return item


def list_intents(path=None):
    """Read and verify choices without creating or modifying any local file."""
    con = None
    try:
        con = _connect(_path(path))
        if con is None:
            return []
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'intents' not in tables:
            if tables:
                raise ScenarioStoreError('Le schéma de l’historique des choix est invalide.')
            return []
        return [_read(row) for row in con.execute('SELECT * FROM intents ORDER BY created_at,id')]
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, AttributeError):
        raise ScenarioStoreError('Impossible de lire un historique de choix valide.') from None
    finally:
        if con is not None:
            con.close()


@cloud_storage.persisted
def record(entry, selection, *, path=None, clock=None):
    """Save a distinct pre-kickoff choice without mutating its forecast.

    Even an identical duplicate click is forbidden after kickoff. Reading a
    previously saved choice remains available through ``list_intents``.
    """
    con = None
    try:
        fixtures, positions, details, fingerprint, forecast_created = _snapshot(entry)
        current = _now(clock)
        _check_time(fixtures, current, forecast_created)
        saved = _make_selection(entry, selection, fixtures, positions, details)
        key = _identity(entry['id'], saved['match_probabilities'])
        metadata = entry.get('metadata') or {}
        competitions = deepcopy(metadata.get('competitions') or {
            row['league']: dict(competition=row['league']) for row in fixtures})
        con = _connect(_path(path), create=True)
        con.execute('BEGIN IMMEDIATE')
        # Check every saved row before extending this append-only history.
        existing = None
        for row in con.execute('SELECT * FROM intents ORDER BY created_at,id'):
            intent = _read(row)
            if intent['id'] == key:
                existing = intent
        before_insert = _now(clock)
        _check_time(fixtures, before_insert, forecast_created)
        if existing is not None:
            if existing['forecast_digest'] != fingerprint:
                raise ScenarioStoreError('La prévision liée au choix enregistré a changé.')
            intent, created = existing, False
        else:
            intent = dict(version=VERSION, id=key, entry_id=entry['id'],
                          created_at=before_insert.isoformat(timespec='microseconds'),
                          forecast_digest=fingerprint, selection=saved, competitions=competitions)
            encoded = _json(intent)
            con.execute('INSERT INTO intents VALUES(?,?,?,?,?)',
                        (key, entry['id'], intent['created_at'], encoded, sha256(encoded.encode()).hexdigest()))
            created = True
        _check_time(fixtures, _now(clock), forecast_created)
        con.commit()
        return dict(intent, created=created)
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        if con is not None:
            con.rollback()
        if isinstance(error, ScenarioStoreError):
            raise
        raise ScenarioStoreError('Impossible d’enregistrer ce scénario avant match.') from None
    finally:
        if con is not None:
            con.close()
