"""API-Football access and local fixtures for the application's single source."""

import cloud_storage
from datetime import datetime, timezone
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import ssl
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from league_model import outcome


BASE_URL = "https://v3.football.api-sports.io"
DATA_DIR = Path(__file__).resolve().parent / "data" / "api_football"
STORE_PATH = DATA_DIR / "fixtures.json"
SECRETS_PATH = Path(__file__).resolve().parent / ".streamlit" / "secrets.toml"
FINISHED_STATUSES = frozenset(("FT", "AET", "PEN"))


class ApiFootballError(Exception):
    """A safe-to-display API or local-storage error."""


def load_key():
    """Read the environment first, then Streamlit's local TOML secrets."""
    key = os.environ.get("API_FOOTBALL_KEY", "").strip()
    if key:
        return key
    try:
        import tomllib
    except ImportError:
        return ""
    if not SECRETS_PATH.exists():
        return ""
    try:
        with SECRETS_PATH.open("rb") as handle:
            secrets = tomllib.load(handle)
    except (OSError, ValueError):
        raise ApiFootballError("Le fichier local des secrets ne peut pas être lu.") from None
    key = secrets.get("API_FOOTBALL_KEY", "")
    if not key:
        section = secrets.get("api_football", {})
        key = section.get("key", "") if isinstance(section, dict) else ""
    return key.strip() if isinstance(key, str) else ""


def _ssl_context():
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


class ApiFootballClient:
    """One verified HTTPS request per call; no automatic retry or pagination."""

    def __init__(self, key):
        if not isinstance(key, str) or not key.strip():
            raise ApiFootballError("La clé API-Football n’est pas configurée.")
        self._key = key.strip()
        self.last_quota = {}

    def _capture_quota(self, headers):
        """Keep only documented nonnegative counters, never arbitrary headers."""
        self.last_quota = {}
        if headers is None:
            return
        try:
            values = {str(key).casefold(): value for key, value in headers.items()}
        except (AttributeError, TypeError, ValueError):
            return
        fields = (("x-ratelimit-requests-limit", "requests_limit_day"),
                  ("x-ratelimit-requests-remaining", "requests_remaining_day"),
                  ("x-ratelimit-remaining", "requests_available_minute"),
                  ("retry-after", "reset_seconds"))
        for header, field in fields:
            value = values.get(header)
            if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip().isdigit():
                self.last_quota[field] = int(str(value).strip())

    def _safe(self, message):
        return str(message).replace(self._key, "[clé masquée]")

    @cloud_storage.persisted
    def get(self, endpoint, **params):
        self.last_quota = {}
        if not isinstance(endpoint, str) or not endpoint.strip("/"):
            raise ApiFootballError("Endpoint API-Football invalide.")
        endpoint = endpoint.strip("/")
        if any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-/" for char in endpoint):
            raise ApiFootballError("Endpoint API-Football invalide.")
        query = urlencode({key: value for key, value in params.items() if value is not None})
        url = f"{BASE_URL}/{endpoint}" + (f"?{query}" if query else "")
        request = Request(url, headers={"x-apisports-key": self._key, "Accept": "application/json"})
        # Never spend a provider request after losing the shared workspace lease.
        cloud_storage.assert_lease()
        try:
            with urlopen(request, timeout=25, context=_ssl_context()) as response:
                self._capture_quota(getattr(response, 'headers', None))
                payload = json.load(response)
        except HTTPError as error:
            self._capture_quota(getattr(error, 'headers', None))
            messages = {
                401: "Clé API-Football refusée.",
                403: "Accès API-Football refusé : vérifie les droits de l’abonnement.",
                429: "Quota API-Football ou limite de requêtes atteint. Réessaie plus tard.",
            }
            raise ApiFootballError(messages.get(error.code, f"API-Football a répondu HTTP {error.code}.")) from None
        except (URLError, TimeoutError, OSError):
            raise ApiFootballError("Connexion à API-Football impossible. Vérifie le réseau et réessaie.") from None
        except (ValueError, UnicodeError):
            raise ApiFootballError("Réponse API-Football invalide : JSON attendu.") from None
        if not isinstance(payload, dict):
            raise ApiFootballError("Réponse API-Football invalide : objet attendu.")
        errors = payload.get("errors")
        if errors:
            if isinstance(errors, dict):
                detail = "; ".join(f"{key}: {value}" for key, value in errors.items())
            elif isinstance(errors, list):
                detail = "; ".join(str(value) for value in errors)
            else:
                detail = str(errors)
            raise ApiFootballError("API-Football : " + self._safe(detail)) from None
        if "response" not in payload:
            raise ApiFootballError("Réponse API-Football invalide : données absentes.")
        return payload

    def status(self):
        return self.get("status")

    def leagues(self, **params):
        return self.get("leagues", **params)

    def fixtures(self, **params):
        return self.get("fixtures", **params)


def _numeric_id(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isdigit() or int(value) <= 0:
        raise ValueError(f"Identifiant {label} absent ou invalide")
    return str(int(value))


def _score_pair(scores, period):
    pair = scores.get(period)
    if not isinstance(pair, dict):
        raise ValueError("Score HT/FT absent ou invalide")
    home, away = pair.get("home"), pair.get("away")
    if any(type(value) is not int or value < 0 for value in (home, away)):
        raise ValueError("Score HT/FT absent ou invalide")
    return home, away


def normalize_fixture(fixture):
    """Convert one completed fixture using regulation fulltime, even after AET/PEN."""
    try:
        event = fixture["fixture"]
        if event["status"]["short"] not in FINISHED_STATUSES:
            raise ValueError("Match non terminé")
        match_id = _numeric_id(event["id"], "du match")
        league = fixture["league"]
        league_id = _numeric_id(league["id"], "de compétition")
        teams = fixture["teams"]
        home_id = _numeric_id(teams["home"]["id"], "de l’équipe domicile")
        away_id = _numeric_id(teams["away"]["id"], "de l’équipe extérieure")
        home, away, competition = teams["home"]["name"], teams["away"]["name"], league["name"]
        if not all(isinstance(value, str) and value.strip() for value in (home, away, competition)):
            raise ValueError("Noms des équipes ou de compétition absents")
        if home_id == away_id:
            raise ValueError("Les deux équipes doivent être différentes")
        stamp = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("Fuseau horaire du match absent")
        stamp = stamp.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
        ht = _score_pair(fixture["score"], "halftime")
        ft = _score_pair(fixture["score"], "fulltime")
        value = outcome(ht, ft)
        season = league["season"]
        if type(season) is not int or season < 1900:
            raise ValueError("Saison absente ou invalide")
        return dict(match_id=match_id, league=league_id, competition_code=league_id, timestamp=stamp, day=stamp[:10],
                    country=league.get("country") or "International", competition=competition,
                    home=home, away=away, home_id=home_id, away_id=away_id, season=season,
                    ht="-".join(map(str, ht)), ft="-".join(map(str, ft)), value=value,
                    provider="api-football", source="https://www.api-football.com/")
    except (KeyError, TypeError, AttributeError):
        raise ValueError("Données du match absentes ou invalides") from None


def _load_store(path):
    path = Path(path)
    if not path.exists():
        return dict(version=1, fixtures=[], season_imports=[])
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("fixtures"), list):
            raise ValueError("Invalid store")
        fixtures = payload["fixtures"]
        seen = set()
        for fixture in fixtures:
            match_id = _numeric_id(fixture["fixture"]["id"], "du match")
            if match_id in seen:
                raise ValueError("Duplicate fixture")
            seen.add(match_id)
        return payload
    except (OSError, ValueError, KeyError, TypeError):
        raise ApiFootballError("Le fichier local des matchs API-Football est illisible ou invalide.") from None


def load_fixtures(path=STORE_PATH):
    """Return saved raw fixtures; absent storage is an empty history."""
    return _load_store(path)["fixtures"]


def _season_identity(raw):
    league = _numeric_id(raw["league"]["id"], "de compétition")
    season = raw["league"]["season"]
    if type(season) is not int or not 1900 <= season <= 2100:
        raise ValueError("Invalid season")
    return league, season


def _validate_season_import(entry, fixtures, *, imported_at=None):
    if not isinstance(entry, dict) or imported_at is not None and entry.get("complete", True) is not True:
        raise ValueError("Invalid season import")
    code = _numeric_id(entry.get("competition_code"), "de compétition")
    season, ids = entry.get("season"), entry.get("match_ids")
    if type(season) is not int or not 1900 <= season <= 2100 or not isinstance(ids, list):
        raise ValueError("Invalid season")
    ids = [_numeric_id(value, "du match") for value in ids]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate season fixture")
    if entry.get("league") is not None and _numeric_id(entry["league"], "de compétition") != code:
        raise ValueError("Mismatched competition")
    for match_id in ids:
        if _season_identity(fixtures[match_id]) != (code, season):
            raise ValueError("Mismatched season fixture")
    if imported_at is None:
        if entry.get("complete") is not True or type(entry.get("received_count")) is not int or entry["received_count"] != len(ids):
            raise ValueError("Incomplete season import")
        stamp = datetime.fromisoformat(entry["imported_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("Season import timezone missing")
        imported_at = stamp.astimezone(timezone.utc).isoformat(timespec="seconds")
    return dict(competition_code=code, season=season, league=code, complete=True,
                match_ids=sorted(ids, key=int), received_count=len(ids), imported_at=imported_at)


def _valid_season_imports(payload, fixtures):
    entries = payload.get("season_imports", [])
    if not isinstance(entries, list):
        return []
    valid = {}
    for entry in entries:
        try:
            marker = _validate_season_import(entry, fixtures)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        identity = marker["competition_code"], marker["season"]
        if identity not in valid or marker["imported_at"] > valid[identity]["imported_at"]:
            valid[identity] = marker
    return list(valid.values())


def load_season_imports(path=STORE_PATH):
    """Return verified complete-season manifests, without inferring from result presence."""
    payload = _load_store(path)
    fixtures = {_numeric_id(raw["fixture"]["id"], "du match"): raw for raw in payload["fixtures"]}
    return _valid_season_imports(payload, fixtures)


def _status_short(raw):
    event = raw.get("fixture")
    state = event.get("status") if isinstance(event, dict) else None
    short = state.get("short") if isinstance(state, dict) else None
    return short if isinstance(short, str) else None


@contextmanager
def _store_write_lock(path):
    """Serialize read/merge/replace across UI threads and background processes.

    The lock has a stable inode separate from the atomically replaced JSON.
    Readers see either complete version and need no lock.
    """
    path = Path(path).resolve()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.with_name('.'+path.name+'.lock').open('a')
    except OSError:
        raise ApiFootballError("Impossible de verrouiller les matchs API-Football localement.") from None
    acquired = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            acquired = True
        except OSError:
            raise ApiFootballError("Impossible de verrouiller les matchs API-Football localement.") from None
        yield path
    finally:
        if acquired:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@cloud_storage.persisted
def save_fixtures(fixtures, path=STORE_PATH, *, season_imports=None):
    """Serialize the whole merge before atomically replacing the provider store."""
    with _store_write_lock(path) as locked_path:
        return _save_fixtures_locked(fixtures, locked_path, season_imports=season_imports)


def _save_fixtures_locked(fixtures, path, *, season_imports=None):
    """Atomically upsert fixtures and optional full-season manifests.

    Calendar/live states never overwrite FT/AET/PEN results. API-Football has no
    update timestamp used here: final-to-final corrections follow import order.
    ``season_imports`` is reserved for complete league/season responses.
    """
    path = Path(path)
    stored = _load_store(path)
    merged = {_numeric_id(row["fixture"]["id"], "du match"): row for row in stored["fixtures"]}
    incoming = {}
    try:
        for row in fixtures:
            match_id = _numeric_id(row["fixture"]["id"], "du match")
            incoming[match_id] = row
            previous = merged.get(match_id)
            if previous is not None and _status_short(previous) in FINISHED_STATUSES and _status_short(row) not in FINISHED_STATUSES:
                continue
            merged[match_id] = row
    except (KeyError, TypeError, ValueError):
        raise ApiFootballError("Un match API-Football à enregistrer a un identifiant invalide.") from None
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    markers = {(row["competition_code"], row["season"]): row for row in _valid_season_imports(stored, merged)}
    if season_imports is not None:
        try:
            if not isinstance(season_imports, list):
                raise ValueError("Season imports must be a list")
            for entry in season_imports:
                marker = _validate_season_import(entry, incoming, imported_at=now)
                expected = {match_id for match_id, raw in incoming.items()
                            if _season_identity(raw) == (marker["competition_code"], marker["season"])}
                if set(marker["match_ids"]) != expected:
                    raise ValueError("Incomplete supplied season batch")
                marker = _validate_season_import(marker, merged)
                markers[marker["competition_code"], marker["season"]] = marker
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ApiFootballError("Le manifeste de saison API-Football est invalide ou ne correspond pas au lot reçu.") from None
    payload = dict(version=1, fixtures=list(merged.values()), season_imports=list(markers.values()), updated_at=now)
    temporary = None
    try:
        serialized = json.dumps(payload, ensure_ascii=False, indent=2)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except (OSError, TypeError, ValueError):
        raise ApiFootballError("Impossible d’enregistrer les matchs API-Football localement.") from None
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
    return len(merged)


def load_matches(path=STORE_PATH):
    """Only completed fixtures enter the model; audit invalid completed scores."""
    fixtures = load_fixtures(path)
    rows, issues, expected = [], [], 0
    for fixture in fixtures:
        event = fixture.get("fixture", {})
        status = event.get("status")
        if not isinstance(status, dict) or not isinstance(status.get("short"), str):
            expected += 1
            issues.append(dict(match_id=str(event.get("id", "?")), reason="Statut du match absent ou invalide"))
            continue
        if status.get("short") not in FINISHED_STATUSES:
            continue
        expected += 1
        try:
            rows.append(normalize_fixture(fixture))
        except ValueError as error:
            issues.append(dict(match_id=str(event.get("id", "?")), reason=str(error)))
    rows.sort(key=lambda row: (row["timestamp"], int(row["match_id"])))
    return rows, dict(expected=expected, matched=len(rows), issues=issues)


def data_signature(path=STORE_PATH):
    """A cheap signature suitable for Streamlit cache arguments."""
    path = Path(path)
    try:
        stat = path.stat()
    except FileNotFoundError:
        return str(path), None, None
    return str(path), stat.st_mtime_ns, stat.st_size
