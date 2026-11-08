"""Isolated API-Football reads; no application or Cloud storage imports."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import ssl
import tomllib
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, HTTPSHandler, HTTPRedirectHandler, build_opener

BASE = 'https://v3.football.api-sports.io/'
ALLOWED = frozenset(('status', 'leagues', 'fixtures', 'fixtures/statistics'))
QUOTA_HEADERS = frozenset(('x-ratelimit-requests-limit', 'x-ratelimit-requests-remaining',
                          'x-ratelimit-limit', 'x-ratelimit-remaining', 'retry-after'))


class ClientError(RuntimeError):
    pass


def load_key(root):
    key = os.environ.get('API_FOOTBALL_KEY', '').strip()
    if not key:
        try:
            with (Path(root) / '.streamlit/secrets.toml').open('rb') as handle:
                config = tomllib.load(handle)
            key = config.get('API_FOOTBALL_KEY')
            if not key:
                section = config.get('api_football', {})
                key = section.get('key') if isinstance(section, dict) else None
        except (OSError, ValueError):
            raise ClientError('La clé API-Football locale est indisponible.') from None
    if not isinstance(key, str) or not key.strip():
        raise ClientError('La clé API-Football locale est indisponible.')
    return key.strip()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ApiClient:
    def __init__(self, root=None, *, key=None, timeout=25, opener=None):
        self.root = Path(root) if root else Path(__file__).resolve().parent
        self._key = key  # Credentials loaded only when a request is actually needed.
        self.timeout = timeout
        self._opener = opener

    def get(self, endpoint, **params):
        if endpoint not in ALLOWED:
            raise ValueError('Endpoint interdit au collecteur de statistiques.')
        key = self._key or load_key(self.root)
        if not isinstance(key, str) or not key.strip():
            raise ClientError('La clé API-Football locale est indisponible.')
        key = key.strip()
        result = dict(endpoint=endpoint, parameters=params, headers={},
                      observed_at_utc=datetime.now(timezone.utc).isoformat(), transport='network_error')
        if self._opener is None:
            try:
                import certifi
                context = ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                context = ssl.create_default_context()
            self._opener = build_opener(HTTPSHandler(context=context), _NoRedirect())
        request = Request(BASE + endpoint + ('?' + urlencode(params) if params else ''),
                          headers={'x-apisports-key': key, 'Accept': 'application/json'})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                result['http_status'] = response.status
                result['headers'] = {k.lower(): v for k,v in response.headers.items() if k.lower() in QUOTA_HEADERS}
                raw = response.read(32 * 1024 * 1024 + 1)
                if len(raw) > 32 * 1024 * 1024:
                    result['transport'] = 'oversized_response'
                elif response.status == 204:
                    # HTTP204 is a valid no-content response, not a JSON error.
                    result.update(transport='received', payload=dict(errors=[], response=[]))
                else:
                    def reject_constant(_):
                        raise ValueError('Nonfinite JSON')
                    payload = json.loads(raw, parse_constant=reject_constant)
                    if isinstance(payload, dict):
                        result.update(transport='received', payload=payload)
                    else:
                        result['transport'] = 'invalid_envelope'
        except HTTPError as error:
            result.update(http_status=error.code, transport='http_error')
            result['headers'] = {k.lower():v for k,v in (error.headers.items() if error.headers else [])
                                 if k.lower() in QUOTA_HEADERS}
        except (URLError, TimeoutError, OSError):
            result['transport'] = 'network_error'
        except (ValueError, UnicodeError):
            result['transport'] = 'invalid_json'
        # No exception text, raw headers, or credential may reach the ledger.
        return json.loads(json.dumps(result, ensure_ascii=False, allow_nan=False).replace(key, '[REDACTED]'))
