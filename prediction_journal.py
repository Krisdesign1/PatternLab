"""Immutable API-Football forecasts and append-only result reconciliation."""

import cloud_storage
from datetime import datetime, timezone
from hashlib import sha256
from itertools import groupby
import json
from math import fsum, log
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

import api_football as api
import api_match_workspace as workspace
import prospective_models as models

DB_PATH = Path(__file__).resolve().parent / 'data' / 'api_football' / 'predictions.sqlite3'
VERSION = 'prediction-journal-v1'
MIXED_VERSION = 'prediction-journal-mixed-v1'


class JournalError(ValueError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _stamp(value):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        raise JournalError('Date du journal ou du match invalide.') from None


def _now(now=None):
    if now is None:
        return datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise JournalError('L’heure de contrôle doit préciser son fuseau.')
    return now.astimezone(timezone.utc)


def _fixtures(fixtures, *, allow_mixed=False):
    fixtures = list(fixtures)
    models.validate_size(len(fixtures))
    rows, seen, leagues = [], set(), set()
    for fixture in fixtures:
        if any(k in fixture for k in ('value', 'ht', 'ft', 'score', 'goals', 'elapsed', 'minute')):
            raise JournalError('Les affiches à enregistrer doivent être dépourvues de scores.')
        try:
            row = {k: fixture[k] for k in ('match_id', 'league', 'home_id', 'away_id', 'home', 'away', 'timestamp', 'provider')}
            if row['provider'] != 'api-football':
                raise JournalError('Le journal accepte uniquement API-Football.')
            for key in ('match_id', 'league', 'home_id', 'away_id'):
                row[key] = api._numeric_id(row[key], key)
            if row['match_id'] in seen or row['home_id'] == row['away_id']:
                raise JournalError('Les matchs et leurs deux clubs doivent être distincts.')
            if not all(isinstance(row[k], str) and row[k].strip() for k in ('home', 'away')):
                raise JournalError('Les noms des clubs sont nécessaires.')
            row['timestamp'] = _stamp(row['timestamp']).replace(tzinfo=None).isoformat(timespec='seconds')
            if 'season' in fixture:
                row['season'] = fixture['season']
            if fixture.get('status', 'TIMED') != 'TIMED' or fixture.get('raw_status', 'NS') != 'NS' or fixture.get('is_upcoming') is False:
                raise JournalError('Seuls les matchs confirmés et non commencés peuvent être enregistrés.')
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, JournalError):
                raise
            raise JournalError('Affiche API-Football incomplète ou invalide.') from None
        seen.add(row['match_id'])
        leagues.add(row['league'])
        rows.append(row)
    if len(leagues) != 1 and not allow_mixed:
        raise JournalError('Un groupe doit conserver une seule compétition.')
    ordered = sorted(rows, key=lambda r: (r['timestamp'], int(r['match_id'])))
    if rows != ordered:
        raise JournalError('Le journal conserve l’ordre chronologique du groupe.')
    return rows


def _group_key(fixtures):
    # Ignore names, model generation and rescheduling: the same match set cannot
    # acquire a new first prediction by renaming/reordering/retraining it.
    if len({row['league'] for row in fixtures}) > 1:
        return sha256(_json(['API-Football','mixed-v1',sorted((int(r['match_id']),r['league']) for r in fixtures)]).encode()).hexdigest()
    return sha256(_json(['API-Football', fixtures[0]['league'], sorted(int(r['match_id']) for r in fixtures)]).encode()).hexdigest()


def _connect(path, create=False):
    path = Path(path or DB_PATH)
    if not create and not path.exists():
        return None
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    if create:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS predictions (
            id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS results (
            prediction_id TEXT NOT NULL, revision INTEGER NOT NULL, checked_at TEXT NOT NULL,
            payload TEXT NOT NULL, PRIMARY KEY(prediction_id, revision),
            FOREIGN KEY(prediction_id) REFERENCES predictions(id));
        CREATE TRIGGER IF NOT EXISTS predictions_no_update BEFORE UPDATE ON predictions
            BEGIN SELECT RAISE(ABORT, 'Forecasts are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS predictions_no_delete BEFORE DELETE ON predictions
            BEGIN SELECT RAISE(ABORT, 'Forecasts are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS results_no_update BEFORE UPDATE ON results
            BEGIN SELECT RAISE(ABORT, 'Result history is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS results_no_delete BEFORE DELETE ON results
            BEGIN SELECT RAISE(ABORT, 'Result history is append-only'); END;
        ''')
    return con


def list_entries(path=None):
    try:
        con = _connect(path)
        if con is None:
            return []
        try:
            output = []
            # A concurrent first writer may have created the SQLite file but
            # not committed its first schema statement yet. No forecast can
            # exist at that point; do not turn this brief empty state into a
            # spurious error or create storage from a read path.
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'predictions' not in tables:
                if tables:
                    raise JournalError('Le schéma du journal local est invalide.')
                return []
            for row in con.execute('SELECT * FROM predictions ORDER BY created_at, id'):
                if sha256(row['payload'].encode()).hexdigest() != row['digest']:
                    raise JournalError('Une prévision enregistrée a été modifiée : journal invalide.')
                item = json.loads(row['payload'])
                events = [dict(revision=r['revision'], checked_at=r['checked_at'], **json.loads(r['payload']))
                          for r in con.execute('SELECT * FROM results WHERE prediction_id=? ORDER BY revision', (row['id'],))]
                latest = events[-1] if events else dict(status='pending', evaluations={}, actual=None, issues=[])
                output.append(dict(item, id=row['id'], created_at=row['created_at'], **latest, result_history=events))
            return output
        finally:
            con.close()
    except (sqlite3.Error, OSError, ValueError, TypeError) as error:
        if isinstance(error, JournalError):
            raise
        raise JournalError('Impossible de lire le journal local des prévisions.') from None


def find(fixtures, path=None):
    key = _group_key(_fixtures(fixtures))
    return next((entry for entry in list_entries(path) if entry['id'] == key), None)


def find_mixed(fixtures, path=None):
    fixtures = _fixtures(fixtures,allow_mixed=True)
    key = _group_key(fixtures)
    return next((entry for entry in list_entries(path) if entry['id']==key),None)


@cloud_storage.persisted
def record(fixtures, methods, *, source='API-Football', now=None, path=None, metadata=None, raw_fixtures=None):
    return _record(fixtures,methods,source=source,now=now,path=path,metadata=metadata,raw_fixtures=raw_fixtures)


@cloud_storage.persisted
def record_mixed(fixtures, methods, *, source='API-Football', now=None, path=None, metadata=None, raw_fixtures=None, clock=None):
    """Explicit daily mixed-group registration; ordinary ``record`` stays strict."""
    return _record(fixtures,methods,source=source,now=now,path=path,metadata=metadata,
                   raw_fixtures=raw_fixtures,allow_mixed=True,clock=clock)


def _mixed_provenance(payload,fixtures,before,leagues):
    if (payload.get('group_kind') != 'mixed' or payload.get('leagues') != leagues
            or payload.get('provider') != 'api-football' or payload.get('assumes_block_independence') is not True
            or payload.get('correction_scope') != 'within_each_league_only'
            or payload.get('distribution',{}).get('type') != 'block_product'):
        raise JournalError('La provenance du produit de compétitions est invalide.')
    saved = payload.get('fixtures')
    if not isinstance(saved,list) or len(saved)!=len(fixtures):
        raise JournalError('Chaque affiche mixte doit conserver sa provenance.')
    fields = ('match_id','league','provider','home','away','home_id','away_id','timestamp')
    for actual,stored in zip(fixtures,saved):
        if not isinstance(stored,dict) or any(str(actual[key])!=str(stored.get(key)) for key in fields):
            raise JournalError('La prévision mixte ne correspond pas aux affiches enregistrées.')
    covered=set()
    for component in payload['distribution']['components']:
        indices,method=component['indices'],component['method']
        code=str(component.get('league'))
        if (code in covered or code not in leagues or any(fixtures[index]['league']!=code for index in indices)
                or method.get('source')!='API-Football' or str(method.get('league'))!=code
                or method.get('training_before')!=before):
            raise JournalError('Les composants mixtes doivent utiliser leur compétition et une coupure UTC commune.')
        covered.add(code)
    if covered!=set(leagues):
        raise JournalError('Chaque compétition du groupe mixte doit conserver son composant.')


def _check_time(fixtures,current,allow_mixed):
    if any(_stamp(row['timestamp'])<=current for row in fixtures):
        raise JournalError('Un coup d’envoi est passé : prévision non enregistrée.')
    if allow_mixed:
        day=current.astimezone(ZoneInfo('Africa/Kinshasa')).date()
        if any(_stamp(row['timestamp']).astimezone(ZoneInfo('Africa/Kinshasa')).date()!=day for row in fixtures):
            raise JournalError('Les nouvelles analyses mixtes doivent contenir uniquement les matchs du jour à Kinshasa.')


@cloud_storage.persisted
def _record(fixtures,methods,*,source='API-Football',now=None,path=None,metadata=None,raw_fixtures=None,allow_mixed=False,clock=None):
    if source != 'API-Football':
        raise JournalError('Le journal accepte uniquement API-Football.')
    if clock is not None and (not callable(clock) or now is not None):
        raise JournalError('Choisissez une seule horloge de contrôle pour le groupe mixte.')
    current = (lambda:_now(clock())) if clock is not None else (lambda:_now(now))
    fixtures = _fixtures(fixtures,allow_mixed=allow_mixed)
    leagues=sorted({row['league'] for row in fixtures},key=int)
    existing = find_mixed(fixtures,path) if allow_mixed else find(fixtures,path)
    if existing:
        return dict(existing, created=False)
    now_value = current()
    _check_time(fixtures,now_value,allow_mixed)
    if not isinstance(methods, dict) or not methods or set(methods) - set(models.METHODS):
        raise JournalError('Aucune méthode valide à enregistrer.')
    before = fixtures[0]['timestamp'][:10]
    for payload in methods.values():
        models.validate_method(payload, len(fixtures))
        if payload.get('source') != source or str(payload.get('league')) != ('mixed' if allow_mixed else fixtures[0]['league']) or payload.get('training_before', '') != before:
            raise JournalError('La provenance de la prévision ne correspond pas au groupe.')
        if allow_mixed:_mixed_provenance(payload,fixtures,before,leagues)
    raw_fixtures = api.load_fixtures(api.STORE_PATH) if raw_fixtures is None else raw_fixtures
    raw = {str(r.get('fixture', {}).get('id')): r for r in raw_fixtures}
    for row in fixtures:
        item = raw.get(row['match_id'])
        try:
            check = workspace.fixture_from_raw(item)
            if api._status_short(item) != 'NS' or any(str(check[k]) != str(row[k]) for k in ('league', 'home_id', 'away_id', 'timestamp')):
                raise JournalError('Le statut, les clubs ou l’horaire ont changé : actualisez le groupe avant de le prévoir.')
        except (TypeError, ValueError):
            raise JournalError('API-Football ne confirme plus toutes les affiches avant leur coup d’envoi.') from None
    safe_meta = metadata or {}
    allowed = ('selected_engine', 'unavailable', 'calendar_checked_at', 'competition', 'country',
               'automation_policy', 'automation_day', 'automation_timezone')
    if allow_mixed:allowed += ('daily_policy','selection','competitions')
    safe_meta = {k: safe_meta[k] for k in allowed if k in safe_meta}
    if allow_mixed:
        if (('daily_policy' in safe_meta and not isinstance(safe_meta['daily_policy'],str))
                or any(name in safe_meta and not isinstance(safe_meta[name],dict) for name in ('selection','competitions'))):
            raise JournalError('Les métadonnées de sélection du groupe mixte sont invalides.')
    payload = dict(version=VERSION, source=source, league=fixtures[0]['league'], size=len(fixtures),
                   fixtures=fixtures, methods=methods, metadata=safe_meta)
    if allow_mixed:
        payload.update(version=MIXED_VERSION,league='mixed',leagues=leagues,group_kind='mixed')
    encoded = _json(payload)
    key = _group_key(fixtures)
    con = None
    try:
        con = _connect(path, create=True)
        con.execute('BEGIN IMMEDIATE')
        if not con.execute('SELECT 1 FROM predictions WHERE id=?', (key,)).fetchone():
            commit_time = current()
            _check_time(fixtures,commit_time,allow_mixed)
            con.execute('INSERT INTO predictions VALUES(?,?,?,?)',
                        (key, commit_time.isoformat(timespec='microseconds'), encoded, sha256(encoded.encode()).hexdigest()))
            created = True
        else:
            created = False
        if created:_check_time(fixtures,current(),allow_mixed)
        con.commit()
    except (sqlite3.Error, OSError):
        raise JournalError('Impossible d’enregistrer la prévision dans le journal local.') from None
    finally:
        if con:
            con.close()
    return dict(next(e for e in list_entries(path) if e['id'] == key), created=created)


def _settle(entry, raw, now):
    actual, observed, issues, statuses = [], [], [], []
    for fixture in entry['fixtures']:
        item = raw.get(fixture['match_id'])
        if item is None:
            issues.append(f"Match {fixture['match_id']} absent du relevé local.")
            statuses.append('unresolved')
            continue
        try:
            check = workspace.fixture_from_raw(item)
            if any(str(check[k]) != str(fixture[k]) for k in ('league', 'home_id', 'away_id', 'timestamp')):
                raise ValueError('Les clubs, la compétition ou l’horaire ont changé.')
            short = api._status_short(item)
            if short in ('CANC', 'ABD', 'AWD', 'WO'):
                statuses.append('void')
                issues.append(f"Match {fixture['match_id']} annulé, abandonné ou attribué.")
            elif short in ('PST', 'SUSP', 'INT'):
                statuses.append('unresolved')
                issues.append(f"Match {fixture['match_id']} reporté ou interrompu.")
            elif short not in api.FINISHED_STATUSES:
                statuses.append('pending')
            else:
                if _stamp(fixture['timestamp']) > now:
                    raise ValueError('Résultat annoncé avant le coup d’envoi enregistré.')
                result = api.normalize_fixture(item)
                actual.append(result['value'])
                observed.append({k: result[k] for k in ('match_id', 'ht', 'ft', 'value')})
                statuses.append('settled')
        except (ValueError, TypeError, KeyError) as error:
            statuses.append('unresolved')
            issues.append(f"Match {fixture['match_id']} : {error}")
    status = 'void' if 'void' in statuses else 'unresolved' if 'unresolved' in statuses else 'pending' if 'pending' in statuses else 'settled'
    evaluations = {}
    if status == 'settled':
        for name, payload in entry['methods'].items():
            models.validate_method(payload, entry['size'])
            probability = models.probability_for_sequence(payload, actual)
            ranking = [row['sequence'] for row in payload['ranking']]
            evaluations[name] = dict(top1=actual == ranking[0], top3=actual in ranking,
                                      probability=probability, loss=-log(probability))
    return dict(status=status, actual=actual if status == 'settled' else None,
                observed=observed, evaluations=evaluations, issues=issues)


@cloud_storage.persisted
def reconcile(raw_fixtures=None, *, now=None, path=None):
    entries = list_entries(path)
    if not entries:
        return dict(entries=0, changed=0, pending=0, settled=0, void=0, unresolved=0)
    now_value = _now(now)
    raw_fixtures = api.load_fixtures(api.STORE_PATH) if raw_fixtures is None else raw_fixtures
    raw = {str(r.get('fixture', {}).get('id')): r for r in raw_fixtures}
    changed = 0
    con = None
    try:
        con = _connect(path)
        con.execute('BEGIN IMMEDIATE')
        for entry in entries:
            state = _settle(entry, raw, now_value)
            latest = con.execute('SELECT revision,payload FROM results WHERE prediction_id=? ORDER BY revision DESC LIMIT 1', (entry['id'],)).fetchone()
            encoded = _json(state)
            if latest and latest['payload'] == encoded:
                continue
            # Do not turn an older provider status into a reversal of a final
            # assessment. API storage also protects finished→nonfinished updates.
            if latest and json.loads(latest['payload'])['status'] == 'settled' and state['status'] == 'pending':
                continue
            revision = latest['revision'] + 1 if latest else 1
            con.execute('INSERT INTO results VALUES(?,?,?,?)', (entry['id'], revision, now_value.isoformat(), encoded))
            changed += 1
        con.commit()
    except (sqlite3.Error, OSError):
        raise JournalError('Impossible de rapprocher les résultats dans le journal local.') from None
    finally:
        if con:
            con.close()
    entries = list_entries(path)
    return dict(entries=len(entries), changed=changed,
                **{s: sum(e['status'] == s for e in entries) for s in ('pending', 'settled', 'void', 'unresolved')})


def summarize(entries=None, *, path=None, required_methods=None):
    entries = list_entries(path) if entries is None else entries
    rows = []
    for entry in entries:
        if entry['status'] != 'settled' or required_methods and not set(required_methods).issubset(entry['evaluations']):
            continue
        for name, evaluation in entry['evaluations'].items():
            if required_methods and name not in required_methods:
                continue
            payload = entry['methods'][name]
            identity = (entry['league'], entry['size'], name, payload['version'], payload.get('generation', ''))
            rows.append((identity, entry['id'], evaluation))
    output = []
    for identity, group in groupby(sorted(rows, key=lambda row: row[0]), key=lambda row: row[0]):
        group = list(group)
        n = len(group)
        first = sum(r[2]['top1'] for r in group)
        top3 = sum(r[2]['top3'] for r in group)
        output.append(dict(league=identity[0], size=identity[1], method=identity[2], version=identity[3], generation=identity[4],
                           tested=n, top1=first, top3=top3, top1_rate=first/n, top3_rate=top3/n,
                           loss=fsum(r[2]['loss'] for r in group)/n, group_ids=[r[1] for r in group]))
    return output
