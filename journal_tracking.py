"""Explicit network checks around immutable forecasts; local result updates."""

import cloud_storage
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

import api_football as api
import api_match_workspace as workspace
import prediction_journal as journal
import prospective_models as models


_RECONCILIATIONS = OrderedDict()
_RECONCILIATION_LOCK = RLock()


def _file_revision(path):
    path = Path(path)
    try:
        stat = path.stat()
        return str(path.resolve()), stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
    except FileNotFoundError:
        return str(path.resolve()), None


def _reconciliation_signature():
    return (api.data_signature(api.STORE_PATH),
            _file_revision(journal.DB_PATH), _file_revision(str(journal.DB_PATH) + '-wal'))


def _cached_reconciliation():
    """Reconcile once per committed local revision, shared by journal pages."""
    key = str(Path(api.STORE_PATH).resolve()), str(Path(journal.DB_PATH).resolve())
    with _RECONCILIATION_LOCK:
        before = _reconciliation_signature()
        current = datetime.now(timezone.utc)
        previous = _RECONCILIATIONS.get(key)
        if (previous and previous['signature'] == before
                and previous['checked_at'] <= current
                and (previous['deadline'] is None or current < previous['deadline'])):
            _RECONCILIATIONS.move_to_end(key)
            return dict(previous['result'], changed=0)
        _RECONCILIATIONS.pop(key, None)
        entries_before = journal.list_entries()
        result = journal.reconcile()
        after = _reconciliation_signature()
        entries_after = journal.list_entries()
        old = {entry['id']: entry for entry in entries_before}
        new = {entry['id']: entry for entry in entries_after}
        own_revisions_only = (old.keys() == new.keys()
            and sum(len(entry['result_history']) for entry in entries_after)
                - sum(len(entry['result_history']) for entry in entries_before) == result.get('changed', 0)
            and all(new[key]['result_history'][:len(entry['result_history'])] == entry['result_history']
                    for key, entry in old.items()))
        # Do not cache a concurrent import or a prediction added during the
        # reconciliation. Append-only result revisions must account for exactly
        # our own writes, too: any other writer's result forces a new pass.
        if (before[0][1] is not None and before[0] == after[0] and after == _reconciliation_signature()
                and own_revisions_only):
            future = [journal._stamp(fixture['timestamp']) for entry in entries_after
                      for fixture in entry['fixtures'] if journal._stamp(fixture['timestamp']) > current]
            _RECONCILIATIONS[key] = dict(signature=after, checked_at=current,
                                        deadline=min(future, default=None), result=dict(result))
            while len(_RECONCILIATIONS) > 8:
                _RECONCILIATIONS.popitem(last=False)
        return result


@cloud_storage.persisted
def register(rows, fixtures, estimate, engine, *, competition=None):
    """Freeze all available methods, then recheck provider status before commit."""
    now = datetime.now(timezone.utc)
    if any(journal._stamp(row['timestamp']) <= now for row in fixtures):
        return dict(state='retrospective', message='Simulation rétrospective : exclue du journal des prévisions avant match.')
    existing = journal.find(fixtures)
    if existing:
        return dict(state='recorded', id=existing['id'], created=False,
                    message='La première prévision de ce groupe est déjà verrouillée dans le journal.')
    frozen = models.build_methods(rows, fixtures, estimate=estimate, engine=engine, decision_at=now)
    selected = {'statistical': 'combined', 'combined': 'combined', 'catboost': 'catboost',
                'context_neighbors': 'context_neighbors'}.get(engine)
    if selected not in frozen['methods']:
        raise journal.JournalError('Le moteur choisi ne peut pas être figé pour ce groupe.')
    client = api.ApiFootballClient(api.load_key())
    ids = [str(row['match_id']) for row in fixtures]
    raw = workspace._response_fixtures(client.fixtures(ids='-'.join(ids), timezone='UTC'))
    if {str(row['fixture']['id']) for row in raw} != set(ids):
        raise journal.JournalError('API-Football n’a pas confirmé toutes les affiches : journal non enregistré.')
    # Keep the normal API-only local store current; this ID-scoped check is not
    # a refresh of every fixture in a calendar day, so no freshness marker moves.
    api.save_fixtures(raw, api.STORE_PATH)
    metadata = dict(selected_engine=engine, unavailable=frozen['unavailable'],
                    calendar_checked_at=datetime.now(timezone.utc).isoformat(),
                    competition=(competition or {}).get('name', fixtures[0]['league']),
                    country=(competition or {}).get('country', ''))
    entry = journal.record(fixtures, frozen['methods'], metadata=metadata)
    return dict(state='recorded', id=entry['id'], created=entry['created'],
                methods=list(entry['methods']), unavailable=entry['metadata'].get('unavailable', {}),
                message='Prévisions verrouillées avant le coup d’envoi ; leur comparaison aux résultats est activée.')


@cloud_storage.persisted
def reconcile_local(*, if_changed=False):
    if if_changed:
        return _cached_reconciliation()
    # Explicit refreshes and workers always perform their requested check.
    key = str(Path(api.STORE_PATH).resolve()), str(Path(journal.DB_PATH).resolve())
    with _RECONCILIATION_LOCK:
        _RECONCILIATIONS.pop(key, None)
        return journal.reconcile()


@cloud_storage.persisted
def refresh_entry(entry):
    """One explicit request for up to 20 tracked fixtures, including corrections."""
    ids = [row['match_id'] for row in entry['fixtures']]
    client = api.ApiFootballClient(api.load_key())
    raw = workspace._response_fixtures(client.fixtures(ids='-'.join(ids), timezone='UTC'))
    if {str(row['fixture']['id']) for row in raw} != set(ids):
        raise journal.JournalError('Réponse incomplète : les résultats du journal ne sont pas modifiés.')
    api.save_fixtures(raw, api.STORE_PATH)
    return reconcile_local()
