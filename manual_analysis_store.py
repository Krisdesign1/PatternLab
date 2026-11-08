"""Shared, append-only manual calculations, separate from official forecasts.

These snapshots preserve what the server calculated. Recalculations of the same
fixtures may coexist and must never be counted as independent observations.
Results are a read-only projection of verified API-Football data.
"""
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from math import fsum, isfinite
from pathlib import Path
import re
import sqlite3
from zoneinfo import ZoneInfo

import api_football as api
import api_match_workspace as workspace
import cloud_storage
import prediction_journal as journal
from halftime_domain import CLASS_ORDER


VERSION = 'manual-analysis-v1'
COMPARISON_VERSION = 'manual-analysis-v2'
SELECTION_VERSION = 'manual-analysis-v3'
MAX_BYTES = 512 * 1024
ENGINES = frozenset(('statistical', 'catboost', 'context_neighbors', 'mixed_clubs'))
ZONE = ZoneInfo('Africa/Kinshasa')
_DROP = frozenset(('result', 'results', 'result_history', 'actual', 'evaluations',
    'matches', 'blocks', 'rows', 'history', 'historical_rows', 'training_rows',
    'raw_fixtures', 'email', 'user', 'user_id', 'session_id', 'ip_address',
    'password', 'token', 'access_token', 'authorization'))
_CAPTURE_TIMES = frozenset(('created_at', 'captured_at', 'capture_time', 'generated_at', 'computed_at'))
_TABLE = '''CREATE TABLE manual_analyses (
    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, day TEXT NOT NULL,
    payload TEXT NOT NULL, digest TEXT NOT NULL)'''
_TRIGGERS = {
    'manual_analyses_no_update': '''CREATE TRIGGER manual_analyses_no_update
        BEFORE UPDATE ON manual_analyses
        BEGIN SELECT RAISE(ABORT, 'Manual analyses are immutable'); END''',
    'manual_analyses_no_delete': '''CREATE TRIGGER manual_analyses_no_delete
        BEFORE DELETE ON manual_analyses
        BEGIN SELECT RAISE(ABORT, 'Manual analyses are immutable'); END''',
    'manual_analyses_no_replace': '''CREATE TRIGGER manual_analyses_no_replace
        BEFORE INSERT ON manual_analyses
        WHEN EXISTS (SELECT 1 FROM manual_analyses WHERE id=NEW.id)
        BEGIN SELECT RAISE(ABORT, 'Manual analyses are immutable'); END''',
}


class ManualAnalysisError(ValueError):
    pass


def database_path(path=None):
    return Path(path) if path is not None else Path(api.DATA_DIR) / 'manual_analyses.sqlite3'


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return sha256(_json(value).encode('utf-8')).hexdigest()


def _instant(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ManualAnalysisError('Une heure UTC avec fuseau horaire est nécessaire.')
    return value.astimezone(timezone.utc)


def _stamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        # Contextual API fixtures use naive timestamps with an explicit UTC contract.
        return _instant(parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed)
    except (TypeError, AttributeError, ValueError):
        raise ManualAnalysisError('La date du calcul ou du match est invalide.') from None


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ManualAnalysisError('La date limite de l’historique est invalide.')
    return value


def _now(clock):
    return _instant(datetime.now(timezone.utc) if clock is None else clock())


def _safe(value):
    """Copy JSON values, omitting result rows and identity data at every depth."""
    budget = [0, 0]

    def visit(item, depth):
        budget[0] += 1
        if depth > 32 or budget[0] > 100000:
            raise ManualAnalysisError('Le calcul est trop volumineux pour être archivé.')
        if item is None or type(item) in (bool, int):
            return item
        if type(item) is float:
            if not isfinite(item):
                raise ManualAnalysisError('Le calcul contient une valeur numérique invalide.')
            return item
        if isinstance(item, str):
            budget[1] += len(item.encode('utf-8'))
            if budget[1] > MAX_BYTES:
                raise ManualAnalysisError('Le calcul est trop volumineux pour être archivé.')
            return item
        if isinstance(item, (list, tuple)):
            return [visit(child, depth + 1) for child in item]
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ManualAnalysisError('Le calcul doit contenir uniquement des données JSON.')
            return {key: visit(child, depth + 1) for key, child in item.items()
                    if key.casefold() not in _DROP
                    # Club profiles use "matches" for a scalar sample count;
                    # preserve that diagnostic while excluding match rows.
                    or key.casefold() == 'matches' and type(child) is int}
        raise ManualAnalysisError('Le calcul doit contenir uniquement des données JSON.')

    copied = visit(value, 0)
    if len(_json(copied).encode('utf-8')) > MAX_BYTES:
        raise ManualAnalysisError('Le calcul est trop volumineux pour être archivé.')
    return copied


def _semantic(value, *, root=True):
    if isinstance(value, dict):
        # A repeated click with the same methods, calibrated rule and exact
        # choices reuses the first archive. The envelope's clock and checksum
        # attest that first capture but do not create another calculation.
        if value.get('schema') in ('adaptive-scenario-proposals-v1', 'computed-scenario-proposals-v2'):
            value = {key: child for key, child in value.items() if key not in ('selection_at', 'sha256')}
        return {key: _semantic(child, root=False) for key, child in value.items()
                if key not in _CAPTURE_TIMES and not (root and key in ('id', 'created'))}
    if isinstance(value, list):
        return [_semantic(child, root=False) for child in value]
    return value


def _ranking(forecast, size):
    ranking = forecast.get('ranking')
    if forecast.get('eligible') is not True or not isinstance(ranking, list) or len(ranking) != 3:
        raise ManualAnalysisError('Le classement de référence du moteur doit conserver ses trois premiers scénarios éligibles.')
    seen, probabilities = set(), []
    for row in ranking:
        if not isinstance(row, dict):
            raise ManualAnalysisError('Un scénario du classement est invalide.')
        sequence, probability = row.get('sequence'), row.get('probability')
        if (not isinstance(sequence, list) or len(sequence) != size
                or any(label not in CLASS_ORDER for label in sequence)
                or tuple(sequence) in seen or isinstance(probability, bool)
                or not isinstance(probability, (int, float)) or not isfinite(probability)
                or not 0 <= probability <= 1):
            raise ManualAnalysisError('Les séquences ou probabilités du classement sont invalides.')
        seen.add(tuple(sequence)); probabilities.append(probability)
    if fsum(probabilities) > 1 or probabilities != sorted(probabilities, reverse=True):
        raise ManualAnalysisError('Les probabilités du classement de référence sont incohérentes.')


def _snapshot(payload, current):
    if not isinstance(payload, dict) or payload.get('source') != 'API-Football' or payload.get('engine') not in ENGINES:
        raise ManualAnalysisError('Seuls les calculs manuels API-Football reconnus peuvent être archivés.')
    fixtures = journal._fixtures(payload.get('fixtures', []), allow_mixed=True)
    if not 2 <= len(fixtures) <= 20:
        raise ManualAnalysisError('Un calcul manuel doit comporter de 2 à 20 matchs.')
    forecast = _safe(payload.get('forecast'))
    if not isinstance(forecast, dict):
        raise ManualAnalysisError('L’estimation calculée est absente.')
    _ranking(forecast, len(fixtures))
    if forecast.get('size', len(fixtures)) != len(fixtures):
        raise ManualAnalysisError('La taille de l’estimation ne correspond pas aux matchs.')
    leagues = {row['league'] for row in fixtures}
    mixed = payload['engine'] == 'mixed_clubs'
    if mixed and (forecast.get('league') != 'mixed'
                  or forecast.get('leagues') != sorted(leagues, key=int)
                  or forecast.get('assumes_block_independence') is not True):
        raise ManualAnalysisError('Le calcul de compétitions multiples doit conserver ses compétitions et son hypothèse de composition.')
    if not mixed and (len(leagues) != 1 or forecast.get('league') is not None
                      and leagues != {str(forecast['league'])}):
        raise ManualAnalysisError('La compétition de l’estimation ne correspond pas aux matchs.')
    if forecast.get('source', 'API-Football') != 'API-Football':
        raise ManualAnalysisError('La provenance de l’estimation est invalide.')
    before = _day(forecast.get('before'))
    cutoff = _day(payload.get('history_cutoff_exclusive', before))
    kickoff_day = min(_stamp(row['timestamp']).date().isoformat() for row in fixtures)
    if not cutoff <= min(current.date().isoformat(), before) or before > kickoff_day:
        raise ManualAnalysisError('L’historique doit être strictement antérieur à la limite avant match.')
    if 'training_before' in forecast and _day(forecast['training_before']) > before:
        raise ManualAnalysisError('La limite des caractéristiques dépasse la période avant match.')
    for key in ('training_cutoff', 'history_cutoff_exclusive', 'cutoff_exclusive'):
        if key in forecast and _day(forecast[key]) > min(current.date().isoformat(), kickoff_day):
            raise ManualAnalysisError('La limite d’apprentissage dépasse la période autorisée.')
    for key in ('latest', 'trained_through', 'selection_through'):
        if forecast.get(key) is not None and _stamp(forecast[key]).date().isoformat() >= cutoff:
            raise ManualAnalysisError('L’estimation contient des informations postérieures à sa limite d’historique.')
    local_days = {_stamp(row['timestamp']).astimezone(ZONE).date().isoformat() for row in fixtures}
    if len(local_days) != 1:
        raise ManualAnalysisError('Les matchs du calcul doivent appartenir à une même journée à Kinshasa.')
    competition = payload.get('competition')
    if competition is not None and not isinstance(competition, dict):
        raise ManualAnalysisError('Les informations de compétition sont invalides.')
    country = payload.get('country', competition.get('country') if competition else None)
    if country is not None and (not isinstance(country, str) or not country.strip()):
        raise ManualAnalysisError('Le pays de la compétition est invalide.')
    comparisons = payload.get('comparison_methods')
    if mixed and comparisons is None:
        raise ManualAnalysisError('Le calcul de compétitions multiples doit conserver ses méthodes complètes.')
    declared = payload.get('version')
    selection = payload.get('proposal_selection')
    if declared not in (None, VERSION, COMPARISON_VERSION, SELECTION_VERSION):
        raise ManualAnalysisError('La version de l’archive manuelle est inconnue.')
    if (declared == VERSION and comparisons is not None
            or declared == COMPARISON_VERSION and (comparisons is None or selection is not None)
            or declared == SELECTION_VERSION and (comparisons is None or selection is None)
            or selection is not None and comparisons is None):
        raise ManualAnalysisError('La couverture comparative ne correspond pas à la version archivée.')
    version = SELECTION_VERSION if selection is not None else COMPARISON_VERSION if comparisons is not None else VERSION
    snapshot = dict(version=version, day=next(iter(local_days)), source='API-Football',
        engine=payload['engine'], fixtures=fixtures, forecast=forecast,
        history_cutoff_exclusive=cutoff,
        model_generation=_safe(payload.get('model_generation', payload.get('generation', forecast.get('generation')))),
        competition=_safe(competition), country=country)
    if comparisons is not None:
        import manual_proposal_archive
        comparisons = _safe(comparisons)
        try:
            snapshot['comparison_methods'] = manual_proposal_archive.validate(
                comparisons, forecast, payload['engine'], len(fixtures), cutoff=cutoff, leagues=leagues,
                fixtures=fixtures, captured_at=current)
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise ManualAnalysisError(str(error)) from None
    if selection is not None:
        import adaptive_scenario_proposals
        try:
            # The source methods remain the immutable Top3 reference. Only the
            # separate, equally immutable envelope defines the new proposals.
            safe_selection = _safe(selection)
            adaptive_scenario_proposals.validate(safe_selection, snapshot['comparison_methods'],
                                                fixtures, current)
            snapshot['proposal_selection'] = safe_selection
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise ManualAnalysisError(str(error)) from None
    if len(_json(snapshot).encode('utf-8')) > MAX_BYTES:
        raise ManualAnalysisError('Le calcul est trop volumineux pour être archivé.')
    _check_time(fixtures, current)
    return snapshot


def _check_time(fixtures, current):
    if any(_stamp(row['timestamp']) <= current for row in fixtures):
        raise ManualAnalysisError('Un coup d’envoi est passé : ce calcul ne peut plus être archivé avant match.')


def _sql(value):
    return re.sub(r'\s+', '', value).lower().rstrip(';')


def _schema(con):
    rows = con.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
    if not rows:
        return False
    objects = {(row['type'], row['name']): row['sql'] for row in rows}
    expected = {('table', 'manual_analyses'): _TABLE,
                **{('trigger', name): sql for name, sql in _TRIGGERS.items()}}
    if objects.keys() != expected.keys() or any(_sql(objects[key]) != _sql(value) for key, value in expected.items()):
        raise ManualAnalysisError('Le schéma de l’archive des calculs manuels est invalide.')
    return True


def _connect(path, *, create=False):
    if not create:
        if not path.exists():
            return None
        con = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        if create and not _schema(con):
            con.execute('BEGIN IMMEDIATE')
            if not _schema(con):
                con.execute(_TABLE)
                for statement in _TRIGGERS.values():
                    con.execute(statement)
            con.commit()
        _schema(con)
        return con
    except BaseException:
        con.close()
        raise


def _validate_entry(item):
    if not isinstance(item, dict) or item.get('version') not in (VERSION, COMPARISON_VERSION, SELECTION_VERSION):
        raise ManualAnalysisError('Un calcul archivé est invalide.')
    current = _stamp(item['created_at'])
    if 'T' not in item['created_at'] or not re.fullmatch(r'[0-9a-f]{64}', item.get('id', '')):
        raise ManualAnalysisError('L’identité d’un calcul archivé est invalide.')
    snapshot = _snapshot(item, current)
    if _digest(_semantic(snapshot)) != item['id']:
        raise ManualAnalysisError('Un calcul archivé ne correspond plus à son identité.')
    expected = dict(snapshot, id=item['id'], created_at=item['created_at'])
    if {key: value for key, value in item.items() if key != 'created'} != expected:
        raise ManualAnalysisError('Les données d’un calcul archivé sont incohérentes.')
    return expected


def _read(row):
    if sha256(row['payload'].encode('utf-8')).hexdigest() != row['digest']:
        raise ManualAnalysisError('Un calcul archivé a été modifié : archive invalide.')
    item = json.loads(row['payload'])
    if _json(item) != row['payload']:
        raise ManualAnalysisError('Le format d’un calcul archivé est invalide.')
    item = _validate_entry(item)
    if (item['id'], item['created_at'], item['day']) != (row['id'], row['created_at'], row['day']):
        raise ManualAnalysisError('L’index d’un calcul archivé est invalide.')
    return item


def list_entries(path=None, day=None):
    """Read newest first, without creating a database or modifying results."""
    con = None
    try:
        if day is not None:
            day = _day(day)
        con = _connect(database_path(path))
        if con is None or not _schema(con):
            return []
        if day is None:
            rows = con.execute('SELECT * FROM manual_analyses ORDER BY created_at DESC,id DESC')
        else:
            rows = con.execute('SELECT * FROM manual_analyses WHERE day=? ORDER BY created_at DESC,id DESC', (day,))
        return [_read(row) for row in rows]
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError) as error:
        if isinstance(error, ManualAnalysisError):
            raise
        raise ManualAnalysisError('Impossible de lire une archive de calculs manuels valide.') from None
    finally:
        if con is not None:
            con.close()


def get(id, path=None):
    con = None
    try:
        if not isinstance(id, str) or not re.fullmatch(r'[0-9a-f]{64}', id):
            raise ManualAnalysisError('La référence du calcul manuel est invalide.')
        con = _connect(database_path(path))
        if con is None or not _schema(con):
            return None
        row = con.execute('SELECT * FROM manual_analyses WHERE id=?', (id,)).fetchone()
        return _read(row) if row is not None else None
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError) as error:
        if isinstance(error, ManualAnalysisError):
            raise
        raise ManualAnalysisError('Impossible de lire ce calcul manuel.') from None
    finally:
        if con is not None:
            con.close()


def days(path=None):
    """Count indexed days without decoding forecasts; opening entries verifies them."""
    con = None
    try:
        con = _connect(database_path(path))
        if con is None or not _schema(con):
            return {}
        counts = {}
        for row in con.execute('SELECT day,COUNT(*) AS count FROM manual_analyses GROUP BY day ORDER BY day DESC'):
            day, count = _day(row['day']), row['count']
            if type(count) is not int or count <= 0:
                raise ManualAnalysisError('Le nombre de calculs d’une journée est invalide.')
            counts[day] = count
        return counts
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        if isinstance(error, ManualAnalysisError):
            raise
        raise ManualAnalysisError('Impossible de lire les journées des calculs manuels.') from None
    finally:
        if con is not None:
            con.close()


@cloud_storage.persisted
def record(payload, path=None, clock=None, *, require_complete=False):
    """Append one exact server calculation; identical semantic reruns deduplicate."""
    con = None
    try:
        target = database_path(path)
        if clock is not None and (path is None or target.resolve() == database_path().resolve()):
            raise ManualAnalysisError('L’archive réelle utilise uniquement l’horloge UTC du serveur.')
        current = _now(clock)
        snapshot = _snapshot(payload, current)
        if require_complete:
            import manual_proposal_archive
            if not snapshot.get('comparison_methods') or not manual_proposal_archive.coverage(snapshot)['complete']:
                raise ManualAnalysisError('Toutes les méthodes et propositions affichées doivent être sauvegardées ensemble.')
        key = _digest(_semantic(snapshot))
        con = _connect(target, create=True)
        con.execute('BEGIN IMMEDIATE')
        before_insert = _now(clock)
        if before_insert < current:
            raise ManualAnalysisError('L’horloge a reculé pendant l’archivage du calcul.')
        _check_time(snapshot['fixtures'], before_insert)
        row = con.execute('SELECT * FROM manual_analyses WHERE id=?', (key,)).fetchone()
        if row is not None:
            entry, created = _read(row), False
        else:
            entry = dict(snapshot, id=key, created_at=before_insert.isoformat(timespec='microseconds'))
            encoded = _json(entry)
            if len(encoded.encode('utf-8')) > MAX_BYTES:
                raise ManualAnalysisError('Le calcul est trop volumineux pour être archivé.')
            con.execute('INSERT INTO manual_analyses VALUES(?,?,?,?,?)',
                (key, entry['created_at'], entry['day'], encoded, sha256(encoded.encode('utf-8')).hexdigest()))
            created = True
        after_insert = _now(clock)
        if after_insert < before_insert:
            raise ManualAnalysisError('L’horloge a reculé pendant l’archivage du calcul.')
        _check_time(snapshot['fixtures'], after_insert)
        con.commit()
        return dict(entry, created=created)
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError) as error:
        if con is not None:
            con.rollback()
        if isinstance(error, ManualAnalysisError):
            raise
        raise ManualAnalysisError('Impossible d’archiver ce calcul manuel avant match.') from None
    finally:
        if con is not None:
            con.close()


def evaluate(entry, raw_fixtures, now=None):
    """Pure result projection: only provider-confirmed HT/90-minute scores count."""
    try:
        entry = _validate_entry(entry)
        current = _instant(datetime.now(timezone.utc) if now is None else now)
        observations, duplicates = {}, set()
        iterable = raw_fixtures.values() if isinstance(raw_fixtures, dict) else raw_fixtures
        for raw in iterable:
            try:
                key = api._numeric_id(raw['fixture']['id'], 'du match')
                if key in observations:
                    duplicates.add(key)
                observations[key] = raw
            except (TypeError, KeyError, ValueError):
                continue
        actual, matches, issues = [], [], []
        states = set()
        for fixture in entry['fixtures']:
            key = fixture['match_id']
            kickoff = _stamp(fixture['timestamp'])
            raw = observations.get(key)
            state, reason, label, score = 'pending', 'Résultat API-Football non confirmé.', None, {}
            if raw is None:
                if current >= kickoff + timedelta(hours=6):
                    state, reason = 'non_evaluable', 'Résultat final API-Football indisponible après le délai de contrôle.'
            else:
                try:
                    if key in duplicates:
                        raise ValueError('Plusieurs observations portent le même identifiant de match.')
                    observed = workspace.fixture_from_raw(raw)
                    if any(observed[field] != fixture[field] for field in ('match_id', 'league', 'home_id', 'away_id')):
                        raise ValueError('Les identifiants API-Football ne correspondent pas aux clubs et à la compétition archivés.')
                    short = api._status_short(raw)
                    if short in ('CANC', 'ABD', 'AWD', 'WO'):
                        state, reason = 'void', 'Match annulé, abandonné ou attribué : aucun résultat de scénario.'
                    elif short in api.FINISHED_STATUSES:
                        normalized = api.normalize_fixture(raw)
                        observed_kickoff = _stamp(normalized['timestamp'])
                        if (observed_kickoff > current or _stamp(entry['created_at']) >= observed_kickoff
                                or entry['history_cutoff_exclusive'] > observed_kickoff.date().isoformat()):
                            raise ValueError('Le résultat final ne respecte pas la période avant match de ce calcul.')
                        state, reason, label = 'settled', 'Résultat HT / 90 minutes confirmé par API-Football.', normalized['value']
                        score = dict(ht=normalized['ht'], ft=normalized['ft'])
                    elif current >= kickoff + timedelta(hours=6):
                        state, reason = 'non_evaluable', 'Match sans statut final API-Football confirmé après le délai de contrôle.'
                except (ValueError, TypeError, KeyError, AttributeError) as error:
                    state, reason = 'non_evaluable', str(error) or 'Observation API-Football invalide.'
            states.add(state)
            actual.append(label)
            matches.append(dict(fixture, status=state, reason=reason, actual=label, **score))
            if state != 'settled':
                issues.append(f'Match {key} : {reason}')
        status = ('void' if 'void' in states else 'non_evaluable' if 'non_evaluable' in states
                  else 'pending' if 'pending' in states else 'settled')
        known = status == 'settled'
        actual = actual if known else None
        verdicts = [dict(rank=index + 1, sequence=list(row['sequence']), probability=row['probability'],
                         matches_all=row['sequence'] == actual if known else None)
                    for index, row in enumerate(entry['forecast']['ranking'])]
        labels = dict(settled='Résultats confirmés', pending='En attente des résultats',
                      non_evaluable='Non évaluable', void='Annulé ou attribué')
        import manual_proposal_archive
        proposed = manual_proposal_archive.evaluate(entry, actual)
        if not known and proposed['state'] != 'abstained':
            proposed['state'] = status
        return dict(id=entry['id'], status=status, status_label=labels[status], actual=actual,
                    scenario_verdicts=verdicts, top1=verdicts[0]['matches_all'],
                    top3=any(row['matches_all'] for row in verdicts) if known else None,
                    matches=matches, issues=issues,
                    proposal_evaluation=proposed, proposal_coverage=proposed['coverage'])
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError) as error:
        if isinstance(error, ManualAnalysisError):
            raise
        raise ManualAnalysisError('Impossible d’évaluer ce calcul manuel.') from None
