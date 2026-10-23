"""Explicit network checks around immutable forecasts; local result updates."""

import cloud_storage
from datetime import datetime, timezone

import api_football as api
import api_match_workspace as workspace
import prediction_journal as journal
import prospective_models as models


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
    frozen = models.build_methods(rows, fixtures, estimate=estimate, engine=engine)
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
def reconcile_local():
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
