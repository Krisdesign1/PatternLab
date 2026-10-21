"""API-Football is the only permitted workspace source."""
from datetime import datetime, timezone
import importlib
import json
import os
from pathlib import Path
import tempfile

import api_football


SOURCES = ('API-Football',)
PREFERENCE_PATH = Path(__file__).resolve().parent / 'data' / 'workspace_settings.json'


class WorkspaceSourceError(ValueError):
    """A missing or unreadable workspace preference cannot select another provider silently."""


ERRORS = (WorkspaceSourceError, api_football.ApiFootballError)


def _validate(source):
    if source not in SOURCES:
        raise WorkspaceSourceError('Seule la source API-Football est autorisée.')
    return source


def selected_source():
    if not PREFERENCE_PATH.exists():
        return 'API-Football'
    try:
        payload = json.loads(PREFERENCE_PATH.read_text(encoding='utf-8'))
        if not isinstance(payload, dict) or payload.get('version') != 1:
            raise ValueError('Invalid workspace settings')
        return _validate(payload.get('source'))
    except (OSError, ValueError, TypeError):
        raise WorkspaceSourceError('Le choix de la source de l’accueil est illisible. '
                                   'Enregistrez votre source dans Données et réglages.') from None


def save_source(source):
    """Persist only a provider label; this operation makes no API call."""
    source = _validate(source)
    payload = {'version': 1, 'source': source,
               'updated_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    temporary = None
    try:
        PREFERENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=PREFERENCE_PATH.parent,
                                         prefix='.workspace-', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, PREFERENCE_PATH)
    except OSError:
        raise WorkspaceSourceError('Impossible d’enregistrer la source de l’accueil.') from None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _provider(source):
    _validate(source)
    return api_football


def _service(source):
    _validate(source)
    return importlib.import_module('api_match_workspace')


def default_league(source):
    _validate(source)
    return '39'


def data_signature(source):
    provider = _provider(source)
    return source, provider.data_signature(provider.STORE_PATH)


def load_matches(source):
    provider = _provider(source)
    return provider.load_matches(provider.STORE_PATH)


def local_snapshot(source, day):
    return _service(source).local_snapshot(day)


def refresh_day(source, day):
    return _service(source).refresh_day(day)


def missing_history(source, code, seasons):
    return _service(source).missing_history(code, seasons)


def ensure_history(source, code, seasons):
    return _service(source).ensure_history(code, seasons)
