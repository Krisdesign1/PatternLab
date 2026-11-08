"""Local write-once individual confirmation records with post-commit receipts.

Large history rows and their source references use content-addressed storage.
Readers reconstruct and verify the original complete forecast payload. A saved
forecast without its post-commit receipt is never timing-admissible.
"""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3

import api_football as api
import cloud_storage
from causal_feature_workspace import digest
from fixture_temporal import utc_instant
import prematch_feature_snapshot as snapshots


VERSION = 'causal-confirmation-store-v1'


class StoreError(ValueError):
    pass


def _root(root=None):
    return Path(api.DATA_DIR if root is None else root).resolve()


def database_path(root=None):
    return _root(root)/'causal_confirmation.sqlite3'


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value):
    if not isinstance(value, str) or re.fullmatch('[a-f0-9]{64}', value) is None:
        raise StoreError('Une empreinte du suivi est invalide.')
    return value


def _sealed(value):
    if (not isinstance(value, dict) or value.get('id') !=
            digest({key: item for key, item in value.items() if key != 'id'})):
        raise StoreError('Un contenu du suivi a été modifié.')
    _hash(value['id'])
    return value


def _seal(value):
    return dict(value, id=digest(value))


def _clock(root, clock=None):
    if clock is not None and _root(root) == Path(api.DATA_DIR).resolve():
        raise StoreError('Une capture réelle utilise uniquement l’horloge UTC du processus.')
    value = clock() if clock is not None else datetime.now(timezone.utc)
    return utc_instant(value)


def _kickoff(fixture):
    value = datetime.fromisoformat(fixture['timestamp'].replace('Z', '+00:00'))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _write_allowed():
    cloud_storage._assert_writable()
    if cloud_storage.configured():
        raise StoreError('La confirmation du candidat exige le stockage local.')


def _schema(con):
    con.executescript('''
        CREATE TABLE IF NOT EXISTS protocols(id TEXT PRIMARY KEY, payload TEXT NOT NULL, digest TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS anchors(protocol_id TEXT NOT NULL REFERENCES protocols(id), day TEXT NOT NULL,
            payload TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(protocol_id,day));
        CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY, protocol_id TEXT NOT NULL REFERENCES protocols(id),
            payload TEXT NOT NULL, digest TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sources(digest TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS contexts(digest TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS forecasts(id TEXT PRIMARY KEY, protocol_id TEXT NOT NULL REFERENCES protocols(id),
            match_id TEXT NOT NULL, transaction_at_utc TEXT NOT NULL, context_id TEXT NOT NULL REFERENCES contexts(digest),
            payload TEXT NOT NULL, digest TEXT NOT NULL, UNIQUE(protocol_id,match_id));
        CREATE TABLE IF NOT EXISTS receipts(forecast_id TEXT PRIMARY KEY REFERENCES forecasts(id),
            payload TEXT NOT NULL, digest TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS results(forecast_id TEXT NOT NULL REFERENCES forecasts(id), revision INTEGER NOT NULL,
            payload TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(forecast_id,revision));
        CREATE TABLE IF NOT EXISTS result_receipts(forecast_id TEXT NOT NULL, revision INTEGER NOT NULL,
            payload TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(forecast_id,revision),
            FOREIGN KEY(forecast_id,revision) REFERENCES results(forecast_id,revision));
        CREATE TABLE IF NOT EXISTS endpoints(protocol_id TEXT PRIMARY KEY REFERENCES protocols(id),
            payload TEXT NOT NULL, digest TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS confirmation_forecast_protocol ON forecasts(protocol_id,match_id);
        CREATE INDEX IF NOT EXISTS confirmation_attempt_protocol ON attempts(protocol_id,id);
    ''')
    for table in ('protocols', 'anchors', 'attempts', 'sources', 'contexts', 'forecasts', 'receipts', 'results',
                  'result_receipts', 'endpoints'):
        for action in ('UPDATE', 'DELETE'):
            con.execute(f'''CREATE TRIGGER IF NOT EXISTS confirmation_{table}_no_{action.lower()}
                BEFORE {action} ON {table}
                BEGIN SELECT RAISE(ABORT,'Confirmation records are append-only'); END''')


def _connect(root=None, *, create=False):
    path = database_path(root)
    if not create and not path.exists():
        return None
    if path.is_symlink():
        raise StoreError('Le chemin de la base de confirmation est invalide.')
    if create:
        _write_allowed()
        path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path.as_uri()+'?mode='+('rwc' if create else 'ro'),
                          uri=True, timeout=15, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA busy_timeout=15000')
    if create:
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('PRAGMA synchronous=FULL')
        _schema(con)
    else:
        con.execute('PRAGMA query_only=ON')
    return con


def _decode(row, *, sealed=True, expected_id=None):
    try:
        value = json.loads(row['payload'])
        if digest(value) != row['digest']:
            raise ValueError
        if sealed:
            _sealed(value)
        if expected_id is not None and value['id'] != expected_id:
            raise ValueError
        return value
    except (ValueError, TypeError, KeyError):
        raise StoreError('Un enregistrement de confirmation est altéré.') from None


def _protocol(con, protocol_id):
    _hash(protocol_id)
    row = con.execute('SELECT * FROM protocols WHERE id=?', (protocol_id,)).fetchone()
    if row is None:
        raise StoreError('Le protocole de confirmation est inconnu.')
    return _decode(row, expected_id=protocol_id)


def protocols(root=None):
    con = _connect(root)
    if con is None:
        return []
    with closing(con):
        return [_decode(row, expected_id=row['id']) for row in con.execute('SELECT * FROM protocols ORDER BY rowid')]


def put_protocol(value, root=None):
    _sealed(value)
    with closing(_connect(root, create=True)) as con:
        con.execute('BEGIN IMMEDIATE')
        try:
            prior = con.execute('SELECT * FROM protocols WHERE id=?', (value['id'],)).fetchone()
            if prior is not None:
                saved = _decode(prior, expected_id=value['id'])
                if saved != value:
                    raise StoreError('Le protocole enregistré ne peut pas être remplacé.')
            else:
                con.execute('INSERT INTO protocols VALUES(?,?,?)', (value['id'], _json(value), digest(value)))
                saved = deepcopy(value)
            con.commit()
            return saved
        except BaseException:
            con.rollback(); raise


def anchor(protocol_id, day, value=None, root=None):
    _hash(protocol_id)
    from datetime import date
    if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
        raise StoreError('La journée du recensement est invalide.')
    if value is not None:
        _sealed(value)
        if value.get('protocol_id', protocol_id) != protocol_id or value.get('day', day) != day:
            raise StoreError('Le recensement ne correspond pas à son protocole ou sa journée.')
    con = _connect(root, create=value is not None)
    if con is None:
        return None
    with closing(con):
        if value is not None:
            con.execute('BEGIN IMMEDIATE')
        try:
            if value is not None:
                _protocol(con, protocol_id)
            row = con.execute('SELECT * FROM anchors WHERE protocol_id=? AND day=?', (protocol_id, day)).fetchone()
            if row is not None:
                saved = _decode(row)
                if saved.get('protocol_id', protocol_id) != protocol_id or saved.get('day', day) != day:
                    raise StoreError('Les index du recensement contredisent son contenu.')
                if value is not None and value != saved:
                    raise StoreError('Le recensement quotidien enregistré ne peut pas être remplacé.')
            elif value is not None:
                con.execute('INSERT INTO anchors VALUES(?,?,?,?)', (protocol_id, day, _json(value), digest(value)))
                saved = deepcopy(value)
            else:
                saved = None
            if value is not None:
                con.commit()
            return saved
        except BaseException:
            if value is not None:
                con.rollback()
            raise


def attempts(protocol_id, value=None, root=None):
    _hash(protocol_id)
    if value is not None:
        _sealed(value)
        if value.get('protocol_id', protocol_id) != protocol_id:
            raise StoreError('La tentative ne correspond pas à son protocole.')
    con = _connect(root, create=value is not None)
    if con is None:
        return [] if value is None else None
    with closing(con):
        if value is not None:
            con.execute('BEGIN IMMEDIATE')
        try:
            if value is not None:
                _protocol(con, protocol_id)
                prior = con.execute('SELECT * FROM attempts WHERE id=?', (value['id'],)).fetchone()
                if prior is None:
                    con.execute('INSERT INTO attempts VALUES(?,?,?,?)',
                                (value['id'], protocol_id, _json(value), digest(value)))
                elif prior['protocol_id'] != protocol_id or _decode(prior, expected_id=value['id']) != value:
                    raise StoreError('Une tentative enregistrée ne peut pas être remplacée.')
                con.commit()
            return [_decode(row, expected_id=row['id']) for row in con.execute(
                'SELECT * FROM attempts WHERE protocol_id=? ORDER BY rowid', (protocol_id,))]
        except BaseException:
            if value is not None:
                con.rollback()
            raise


def _validate_endpoint(value, protocol):
    _sealed(value)
    fields = {'id', 'version', 'protocol_id', 'result_cutoff_utc', 'prepared_at',
              'selection', 'days', 'attempt_ids'}
    if (set(value) != fields or value.get('version') != 'causal-confirmation-endpoint-v1'
            or value.get('protocol_id') != protocol['id']):
        raise StoreError('Le bilan final ne correspond pas au protocole inscrit.')
    try:
        cutoff = utc_instant(value['result_cutoff_utc'])
        prepared = utc_instant(value['prepared_at'])
        if cutoff != utc_instant(protocol['followup']['result_cutoff_utc']) or prepared < cutoff:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise StoreError('La coupure du bilan final est absente ou différente du suivi inscrit.') from None
    if (not isinstance(value['selection'], dict) or not isinstance(value['days'], list)
            or not isinstance(value['attempt_ids'], list)
            or any(not isinstance(identity, str) for identity in value['attempt_ids'])
            or len(set(value['attempt_ids'])) != len(value['attempt_ids'])):
        raise StoreError('La sélection conservée dans le bilan final est invalide.')
    for forecast_id, selected in value['selection'].items():
        _hash(forecast_id)
        if not isinstance(selected, dict) or set(selected) != {'revision', 'result_id'}:
            raise StoreError('La révision du bilan final est invalide.')
        revision, identity = selected['revision'], selected['result_id']
        if revision is None:
            if identity is not None:
                raise StoreError('Une révision absente ne peut désigner un résultat.')
        elif type(revision) is not int or revision < 1:
            raise StoreError('La révision du bilan final doit être un entier positif.')
        else:
            _hash(identity)
    for identity in value['attempt_ids']:
        _hash(identity)
    return value


def endpoint(protocol_id, value=None, root=None, *, clock=None):
    """Read or seal one immutable final selection, without repairing its past.

    ``prepared_at`` records when the first selection was actually assembled;
    it is not represented as the earlier fixed result cutoff. Later arrivals,
    receipts and corrections cannot replace a sealed manifest.
    """
    _hash(protocol_id)
    if value is not None:
        _sealed(value)
        if value.get('protocol_id') != protocol_id:
            raise StoreError('Le bilan final ne correspond pas au protocole demandé.')
        if clock is not None and _root(root) == Path(api.DATA_DIR).resolve():
            raise StoreError('Un bilan final réel utilise uniquement l’horloge UTC du processus.')
    con = _connect(root, create=value is not None)
    if con is None:
        return None
    with closing(con):
        if value is None:
            exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='endpoints'").fetchone()
            if exists is None:
                return None
        else:
            con.execute('BEGIN IMMEDIATE')
        try:
            row = con.execute('SELECT * FROM endpoints WHERE protocol_id=?', (protocol_id,)).fetchone()
            if row is None and value is None:
                return None
            protocol = _protocol(con, protocol_id)
            if row is not None:
                saved = _validate_endpoint(_decode(row), protocol)
                if value is not None and value != saved:
                    raise StoreError('Le bilan final enregistré ne peut pas être remplacé.')
            else:
                _validate_endpoint(value, protocol)
                actual = _clock(root, clock)
                if utc_instant(value['prepared_at']) > actual or actual < utc_instant(value['result_cutoff_utc']):
                    raise StoreError('Un bilan final ne peut pas être enregistré avant sa coupure réelle.')
                con.execute('INSERT INTO endpoints VALUES(?,?,?)', (protocol_id, _json(value), digest(value)))
                saved = deepcopy(value)
            if value is not None:
                con.commit()
            return saved
        except BaseException:
            if value is not None:
                con.rollback()
            raise


def _put_document(con, table, value):
    identity = digest(value)
    row = con.execute(f'SELECT * FROM {table} WHERE digest=?', (identity,)).fetchone()
    if row is None:
        con.execute(f'INSERT INTO {table} VALUES(?,?)', (identity, _json(value)))
    elif _decode(row, sealed=False) != value:
        raise StoreError('Un contenu historique partagé est altéré.')
    return identity


def _document(con, table, identity):
    _hash(identity)
    row = con.execute(f'SELECT * FROM {table} WHERE digest=?', (identity,)).fetchone()
    if row is None:
        raise StoreError('Un contenu historique partagé est absent.')
    return _decode(row, sealed=False)


def _history(history_rows, source_refs):
    if not isinstance(history_rows, list) or not isinstance(source_refs, list):
        raise StoreError('Un historique et ses références ordonnés sont nécessaires.')
    try:
        rows = {row['match_id']: row for row in history_rows}
        refs = {row['match_id']: row for row in source_refs}
        if (len(rows) != len(history_rows) or len(refs) != len(source_refs) or set(rows) != set(refs)
                or any(row.get('provider') != 'api-football' for row in history_rows)
                or any(ref['normalized_sha256'] != digest(rows[mid])
                       or ref['time_kind'] != 'local_import_recorded' for mid, ref in refs.items())):
            raise ValueError
        for ref in source_refs:
            _hash(ref['observation_id']); utc_instant(ref['observed_at_utc'])
    except (ValueError, TypeError, KeyError):
        raise StoreError('Les références ne correspondent pas à l’historique partagé.') from None
    return dict(history_rows=history_rows, source_refs=source_refs)


def _put_history(con, history_rows, source_refs):
    history = _history(history_rows, source_refs)
    context = {name: [_put_document(con, 'sources', row) for row in values]
               for name, values in history.items()}
    return _put_document(con, 'contexts', context)


def _get_history(con, context_id):
    context = _document(con, 'contexts', context_id)
    if set(context) != {'history_rows', 'source_refs'} or any(not isinstance(context[k], list) for k in context):
        raise StoreError('Le contexte historique partagé est invalide.')
    return _history(*([_document(con, 'sources', identity) for identity in context[name]]
                      for name in ('history_rows', 'source_refs')))


def put_history(history_rows, source_refs, root=None):
    """Freeze a reusable league history before committing a daily anchor."""
    _history(history_rows, source_refs)
    with closing(_connect(root, create=True)) as con:
        con.execute('BEGIN IMMEDIATE')
        try:
            context_id = _put_history(con, history_rows, source_refs)
            con.commit()
            return context_id
        except BaseException:
            con.rollback(); raise


def get_history(context_id, root=None):
    _hash(context_id)
    con = _connect(root)
    if con is None:
        raise StoreError('Le contexte historique partagé est absent.')
    with closing(con):
        return _get_history(con, context_id)


def _compact(con, payload):
    value = deepcopy(payload)
    snapshot = value['snapshot']
    context_id = _put_history(con, snapshot.pop('history_rows'), snapshot.pop('source_refs'))
    return value, context_id


def _forecast(con, row):
    value = _decode(row, sealed=False)
    context = _get_history(con, row['context_id'])
    if any(name in value['snapshot'] for name in ('history_rows', 'source_refs')):
        raise StoreError('Le contenu compact contient un historique contradictoire.')
    value['snapshot'].update(context)
    _validate_forecast(value)
    protocol = _protocol(con, value['protocol_id'])
    if (value['id'] != row['id'] or value['protocol_id'] != row['protocol_id']
            or value['fixture']['match_id'] != row['match_id']
            or utc_instant(row['transaction_at_utc']) < utc_instant(value['snapshot']['decision_at'])
            or 'registered_at' in protocol and utc_instant(row['transaction_at_utc']) < utc_instant(protocol['registered_at'])):
        raise StoreError('Les index de la prévision contredisent son contenu.')
    receipt = con.execute('SELECT * FROM receipts WHERE forecast_id=?', (row['id'],)).fetchone()
    ack = _decode(receipt) if receipt is not None else None
    if ack is not None:
        _validate_ack(ack, value, row['transaction_at_utc'])
    return dict(payload=value, ack=ack)


def _validate_forecast(value):
    _sealed(value)
    _hash(value['protocol_id'])
    snapshot = snapshots.validate(value['snapshot'])
    if value.get('fixture') != snapshot['fixture'] or 'target_observation' not in snapshot:
        raise StoreError('La prévision ne correspond pas à son affiche figée.')


def _validate_ack(ack, payload, transaction_at):
    before = utc_instant(ack['observed_at_utc']) < _kickoff(payload['fixture'])
    actual = ack.get('time_quality') == 'local_commit_ack'
    reason = ('saved_before_kickoff' if before else 'commit_ack_after_kickoff') if actual else 'synthetic_clock_unattested'
    if (ack.get('version') != VERSION or ack.get('forecast_id') != payload['id']
            or ack.get('time_quality') not in ('local_commit_ack', 'synthetic_unattested')
            or type(ack.get('before_kickoff')) is not bool or ack['before_kickoff'] != before
            or type(ack.get('admissible')) is not bool or ack['admissible'] != (before and actual)
            or ack.get('reason') != reason
            or ack.get('transaction_at_utc') != transaction_at
            or utc_instant(ack['observed_at_utc']) < utc_instant(transaction_at)):
        raise StoreError('L’accusé de sauvegarde de la prévision est incohérent.')


def forecasts(root=None, protocol_id=None):
    if protocol_id is not None:
        _hash(protocol_id)
    con = _connect(root)
    if con is None:
        return []
    with closing(con):
        sql = 'SELECT * FROM forecasts' + (' WHERE protocol_id=?' if protocol_id is not None else '') + ' ORDER BY rowid'
        return [_forecast(con, row) for row in con.execute(sql, () if protocol_id is None else (protocol_id,))]


def heads(root=None, protocol_id=None):
    """Read small immutable capture metadata without rebuilding its features.

    This is the scheduler/report path, not a complete history audit. The writer
    validated the full snapshot before publication. ``forecasts`` remains the
    explicit reconstruction/audit path that also verifies all CAS documents.
    """
    if protocol_id is not None:
        _hash(protocol_id)
    con = _connect(root)
    if con is None:
        return []
    with closing(con):
        sql = 'SELECT * FROM forecasts' + (' WHERE protocol_id=?' if protocol_id is not None else '') + ' ORDER BY rowid'
        output, registered = [], {}
        for row in con.execute(sql, () if protocol_id is None else (protocol_id,)):
            value = _decode(row, sealed=False)
            _hash(row['context_id']); _hash(value['id']); _hash(value['snapshot']['id'])
            if value['protocol_id'] not in registered:
                registered[value['protocol_id']] = _protocol(con, value['protocol_id'])
            protocol = registered[value['protocol_id']]
            if (value['id'] != row['id'] or value['protocol_id'] != row['protocol_id']
                    or value['fixture']['match_id'] != row['match_id']
                    or value.get('fixture') != value['snapshot'].get('fixture')
                    or any(name in value['snapshot'] for name in ('history_rows', 'source_refs'))
                    or utc_instant(row['transaction_at_utc']) < utc_instant(value['snapshot']['decision_at'])
                    or 'registered_at' in protocol and utc_instant(row['transaction_at_utc']) < utc_instant(protocol['registered_at'])):
                raise StoreError('Les index de la prévision compacte contredisent son contenu.')
            receipt = con.execute('SELECT * FROM receipts WHERE forecast_id=?', (row['id'],)).fetchone()
            ack = _decode(receipt) if receipt is not None else None
            if ack is not None:
                _validate_ack(ack, value, row['transaction_at_utc'])
            output.append(dict(payload=value, ack=ack, context_id=row['context_id']))
        return output


def _write_ack(con, value):
    con.execute('BEGIN IMMEDIATE')
    try:
        con.execute('INSERT INTO receipts VALUES(?,?,?)', (value['forecast_id'], _json(value), digest(value)))
        con.commit()
    except BaseException:
        con.rollback(); raise


def put_forecast(payload, root=None, clock=None):
    """Save once; only an actual post-commit clock receipt can attest timing.

    Tests may inject a clock outside the production root. Such receipts always
    remain synthetic and timing-inadmissible, even when before the kickoff.
    An existing forecast with missing receipt is never repaired by this call.
    """
    _validate_forecast(payload)
    # Reject injected production clocks before creating a database or table.
    if clock is not None and _root(root) == Path(api.DATA_DIR).resolve():
        raise StoreError('Une capture réelle utilise uniquement l’horloge UTC du processus.')
    with closing(_connect(root, create=True)) as con:
        con.execute('BEGIN IMMEDIATE')
        try:
            protocol = _protocol(con, payload['protocol_id'])
            prior = con.execute('SELECT * FROM forecasts WHERE protocol_id=? AND match_id=?',
                                (payload['protocol_id'], payload['fixture']['match_id'])).fetchone()
            if prior is not None:
                saved = _forecast(con, prior)
                con.commit()
                return dict(saved, existing=True)
            transaction_at = _clock(root, clock)
            if (transaction_at < utc_instant(payload['snapshot']['decision_at'])
                    or 'registered_at' in protocol and transaction_at < utc_instant(protocol['registered_at'])):
                raise StoreError('La capture ne peut pas précéder sa décision ou son protocole.')
            if transaction_at >= _kickoff(payload['fixture']):
                raise StoreError('Le coup d’envoi est passé avant l’enregistrement transactionnel.')
            compact, context_id = _compact(con, payload)
            con.execute('INSERT INTO forecasts VALUES(?,?,?,?,?,?,?)',
                        (payload['id'], payload['protocol_id'], payload['fixture']['match_id'],
                         transaction_at.isoformat(), context_id, _json(compact), digest(compact)))
            if _clock(root, clock) >= _kickoff(payload['fixture']):
                raise StoreError('Le coup d’envoi est passé pendant la transaction ; capture refusée.')
            con.commit()
        except BaseException:
            con.rollback(); raise
        observed_at = _clock(root, clock)  # Actual upper bound AFTER the forecast COMMIT.
        before = observed_at < _kickoff(payload['fixture'])
        ack = _seal(dict(version=VERSION, forecast_id=payload['id'],
            transaction_at_utc=transaction_at.isoformat(), observed_at_utc=observed_at.isoformat(),
            before_kickoff=before, time_quality='local_commit_ack' if clock is None else 'synthetic_unattested',
            admissible=before and clock is None,
            reason=('saved_before_kickoff' if before else 'commit_ack_after_kickoff') if clock is None else 'synthetic_clock_unattested'))
        _validate_ack(ack, payload, transaction_at.isoformat())
        _write_ack(con, ack)
        return dict(payload=deepcopy(payload), ack=ack, existing=False)


def _validate_result_ack(ack, forecast_id, revision, payload):
    if (ack.get('version') != VERSION or ack.get('forecast_id') != forecast_id
            or type(ack.get('revision')) is not int or ack['revision'] != revision
            or ack.get('result_id') != payload['id']
            or ack.get('time_quality') not in ('local_result_commit_ack', 'synthetic_unattested')):
        raise StoreError('L’accusé de sauvegarde du résultat est incohérent.')
    observed = utc_instant(ack['observed_at_utc'])
    if 'checked_at' in payload and observed < utc_instant(payload['checked_at']):
        raise StoreError('L’accusé de sauvegarde précède le contrôle du résultat.')


def _result_ack(con, forecast_id, revision, payload):
    # Opening a pre-migration database is read-only. Missing historical timing
    # receipts stay unknown; creating the new table cannot attest older rows.
    exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='result_receipts'").fetchone()
    if exists is None:
        return None
    row = con.execute('SELECT * FROM result_receipts WHERE forecast_id=? AND revision=?',
                      (forecast_id, revision)).fetchone()
    if row is None:
        return None
    ack = _decode(row)
    _validate_result_ack(ack, forecast_id, revision, payload)
    return ack


def _write_result_ack(con, value):
    con.execute('BEGIN IMMEDIATE')
    try:
        con.execute('INSERT INTO result_receipts VALUES(?,?,?,?)',
                    (value['forecast_id'], value['revision'], _json(value), digest(value)))
        con.commit()
    except BaseException:
        con.rollback(); raise


def put_result(forecast_id, value, root=None, *, clock=None):
    """Append a result, then record a separate actual post-COMMIT upper bound.

    Replaying an identical result never supplies a missing timing receipt.
    Legacy rows and crashes after COMMIT remain readable with ``ack=None``.
    """
    _hash(forecast_id); _sealed(value)
    if value.get('forecast_id', forecast_id) != forecast_id:
        raise StoreError('Le résultat ne correspond pas à sa prévision.')
    if clock is not None and _root(root) == Path(api.DATA_DIR).resolve():
        raise StoreError('Un résultat réel utilise uniquement l’horloge UTC du processus.')
    with closing(_connect(root, create=True)) as con:
        con.execute('BEGIN IMMEDIATE')
        try:
            if con.execute('SELECT 1 FROM forecasts WHERE id=?', (forecast_id,)).fetchone() is None:
                raise StoreError('La prévision du résultat est inconnue.')
            prior = con.execute('SELECT * FROM results WHERE forecast_id=? ORDER BY revision DESC LIMIT 1',
                                (forecast_id,)).fetchone()
            if prior is not None and _decode(prior) == value:
                ack = _result_ack(con, forecast_id, prior['revision'], value)
                con.commit()
                return dict(revision=prior['revision'], payload=deepcopy(value), ack=ack, existing=True)
            revision = 1 if prior is None else prior['revision']+1
            transaction_at = _clock(root, clock)
            if 'checked_at' in value and utc_instant(value['checked_at']) > transaction_at:
                raise StoreError('Le résultat ne peut pas être contrôlé après sa sauvegarde transactionnelle.')
            con.execute('INSERT INTO results VALUES(?,?,?,?)', (forecast_id, revision, _json(value), digest(value)))
            con.commit()
        except BaseException:
            con.rollback(); raise
        # ``checked_at`` can belong to a long-running reconciliation. Only this
        # upper bound, sampled after its result COMMIT, qualifies a fixed end.
        observed_at = _clock(root, clock)
        if observed_at < transaction_at:
            raise StoreError('L’horloge a reculé pendant la sauvegarde du résultat ; accusé absent.')
        ack = _seal(dict(version=VERSION, forecast_id=forecast_id, revision=revision,
                         result_id=value['id'], observed_at_utc=observed_at.isoformat(),
                         time_quality='local_result_commit_ack' if clock is None else 'synthetic_unattested'))
        _validate_result_ack(ack, forecast_id, revision, value)
        _write_result_ack(con, ack)
        return dict(revision=revision, payload=deepcopy(value), ack=ack, existing=False)


def results(forecast_id, root=None):
    _hash(forecast_id)
    con = _connect(root)
    if con is None:
        return []
    with closing(con):
        values = [dict(revision=row['revision'], payload=_decode(row)) for row in con.execute(
            'SELECT * FROM results WHERE forecast_id=? ORDER BY revision', (forecast_id,))]
        if [row['revision'] for row in values] != list(range(1, len(values)+1)):
            raise StoreError('Les révisions de résultat sont discontinues.')
        if any(row['payload'].get('forecast_id', forecast_id) != forecast_id for row in values):
            raise StoreError('Les révisions ne correspondent pas à leur prévision.')
        for row in values:
            row['ack'] = _result_ack(con, forecast_id, row['revision'], row['payload'])
        return values
