"""football-data.org access and local matches, independent of other providers."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import ssl
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from league_model import outcome


BASE_URL = "https://api.football-data.org/v4"
DATA_DIR = Path(__file__).resolve().parent / "data" / "football_data"
STORE_PATH = DATA_DIR / "matches.json"
SECRETS_PATH = Path(__file__).resolve().parent / ".streamlit" / "secrets.toml"
FINISHED_STATUSES = frozenset(("FINISHED",))


class FootballDataError(Exception):
    """A safe-to-display provider or local-storage error."""


def load_key():
    """Read FOOTBALL_DATA_KEY, then the local [football_data] key setting."""
    key = os.environ.get("FOOTBALL_DATA_KEY", "").strip()
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
        raise FootballDataError("Le fichier local des secrets ne peut pas être lu.") from None
    section = secrets.get("football_data", {})
    key = section.get("key", "") if isinstance(section, dict) else ""
    return key.strip() if isinstance(key, str) else ""


def _ssl_context():
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


class FootballDataClient:
    """A single verified HTTPS request per call, without automatic retries."""

    def __init__(self, key):
        if not isinstance(key, str) or not key.strip():
            raise FootballDataError("La clé football-data.org n’est pas configurée.")
        self._key = key.strip()
        self.last_quota = {}

    def _read_quota(self, headers):
        self.last_quota = {}
        if headers is None:
            return
        for header, field in (("X-RequestsAvailable", "requests_available_minute"),
                              ("X-RequestCounter-Reset", "reset_seconds")):
            value = headers.get(header)
            if value is None and header == "X-RequestsAvailable":
                value = headers.get("X-RequestsAvailableMinute")
            if value is not None and re.fullmatch(r"\d+", str(value).strip()):
                self.last_quota[field] = int(str(value).strip())

    def get(self, endpoint, **params):
        if not isinstance(endpoint, str) or not re.fullmatch(r"[A-Za-z0-9_/-]+", endpoint.strip("/")):
            raise FootballDataError("Endpoint football-data.org invalide.")
        endpoint = endpoint.strip("/")
        query = urlencode({key: value for key, value in params.items() if value is not None})
        url = f"{BASE_URL}/{endpoint}" + (f"?{query}" if query else "")
        request = Request(url, headers={"X-Auth-Token": self._key, "Accept": "application/json"})
        self.last_quota = {}
        try:
            with urlopen(request, timeout=25, context=_ssl_context()) as response:
                self._read_quota(response.headers)
                payload = json.load(response)
        except HTTPError as error:
            self._read_quota(error.headers)
            messages = {
                401: "Clé football-data.org refusée.",
                403: "Accès football-data.org refusé. Vérifie la clé et les compétitions incluses dans l’abonnement.",
                429: "Limite de requêtes football-data.org atteinte. Réessaie après le délai indiqué.",
            }
            raise FootballDataError(messages.get(error.code, f"football-data.org a répondu HTTP {error.code}.")) from None
        except (URLError, TimeoutError, OSError):
            raise FootballDataError("Connexion à football-data.org impossible. Vérifie le réseau et réessaie.") from None
        except (ValueError, UnicodeError):
            raise FootballDataError("Réponse football-data.org invalide : JSON attendu.") from None
        if not isinstance(payload, dict):
            raise FootballDataError("Réponse football-data.org invalide : objet attendu.")
        if payload.get("errorCode") or payload.get("message"):
            detail = str(payload.get("message") or "Erreur du fournisseur").replace(self._key, "[clé masquée]")
            raise FootballDataError("football-data.org : " + detail) from None
        return payload

    def competitions(self):
        return self.get("competitions")

    def matches(self, competition, **params):
        code = str(competition)
        if isinstance(competition, bool) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", code):
            raise FootballDataError("Identifiant de compétition football-data.org invalide.")
        return self.get(f"competitions/{code}/matches", **params)


def _numeric_id(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isdigit() or int(value) <= 0:
        raise ValueError(f"Identifiant {label} absent ou invalide")
    return str(int(value))


def _score_pair(score, field):
    pair = score.get(field)
    if not isinstance(pair, dict):
        raise ValueError("Score HT/FT à 90 minutes absent ou invalide")
    home, away = pair.get("home"), pair.get("away")
    if any(type(value) is not int or value < 0 for value in (home, away)):
        raise ValueError("Score HT/FT à 90 minutes absent ou invalide")
    return home, away


def normalize_fixture(fixture):
    """Use regularTime for overtime/shootouts; fullTime is safe only for REGULAR."""
    try:
        if fixture["status"] not in FINISHED_STATUSES:
            raise ValueError("Match non terminé")
        match_id = _numeric_id(fixture["id"], "du match")
        competition = fixture["competition"]
        league_id = _numeric_id(competition["id"], "de compétition")
        home_team, away_team = fixture["homeTeam"], fixture["awayTeam"]
        home_id = _numeric_id(home_team["id"], "de l’équipe domicile")
        away_id = _numeric_id(away_team["id"], "de l’équipe extérieure")
        if home_id == away_id:
            raise ValueError("Les deux équipes doivent être différentes")
        home, away, name = home_team["name"], away_team["name"], competition["name"]
        if not all(isinstance(value, str) and value.strip() for value in (home, away, name)):
            raise ValueError("Noms des équipes ou de compétition absents")
        stamp = datetime.fromisoformat(fixture["utcDate"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("Fuseau horaire du match absent")
        stamp = stamp.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
        start = fixture["season"]["startDate"]
        season = datetime.strptime(start[:10], "%Y-%m-%d").year
        if season < 1900:
            raise ValueError("Saison absente ou invalide")
        score = fixture["score"]
        ht = _score_pair(score, "halfTime")
        duration = score.get("duration")
        if duration == "REGULAR":
            ft = _score_pair(score, "fullTime")
        elif duration in ("EXTRA_TIME", "PENALTY_SHOOTOUT"):
            ft = _score_pair(score, "regularTime")
        else:
            raise ValueError("Durée réglementaire du match non reconnue")
        value = outcome(ht, ft)
        return dict(match_id=match_id, league=league_id, competition_code=competition.get("code"),
                    country=fixture["area"].get("name") or "International", competition=name,
                    timestamp=stamp, day=stamp[:10], home=home, away=away,
                    home_id=home_id, away_id=away_id, season=season,
                    ht="-".join(map(str, ht)), ft="-".join(map(str, ft)), value=value,
                    provider="football-data.org", source="https://www.football-data.org/")
    except (KeyError, TypeError, AttributeError):
        raise ValueError("Données du match absentes ou invalides") from None


def _load_store(path):
    path = Path(path)
    if not path.exists():
        return dict(version=1, matches=[], season_imports=[])
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("matches"), list):
            raise ValueError("Invalid store")
        matches = payload["matches"]
        seen = set()
        for match in matches:
            match_id = _numeric_id(match["id"], "du match")
            if match_id in seen:
                raise ValueError("Duplicate match")
            seen.add(match_id)
        return payload
    except (OSError, ValueError, KeyError, TypeError):
        raise FootballDataError("Le fichier local des matchs football-data.org est illisible ou invalide.") from None


def load_fixtures(path=STORE_PATH):
    """Return saved raw matches; refuse corruption instead of loading a partial file."""
    return _load_store(path)["matches"]


def _season_identity(match):
    competition = match["competition"]
    code = competition["code"]
    if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9_-]{1,40}", code):
        raise ValueError("Invalid competition")
    league = _numeric_id(competition["id"], "de compétition")
    year = datetime.strptime(match["season"]["startDate"], "%Y-%m-%d").year
    return code, year, league


def _validate_season_import(entry, matches, *, imported_at=None):
    """A marker is valid only when its identity and referenced raw records agree."""
    if not isinstance(entry, dict):
        raise ValueError("Invalid season import")
    if imported_at is not None and entry.get("complete", True) is not True:
        raise ValueError("Incomplete season import")
    code, year, ids = entry.get("competition_code"), entry.get("season"), entry.get("match_ids")
    if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9_-]{1,40}", code):
        raise ValueError("Invalid competition")
    if type(year) is not int or not 1900 <= year <= 2100 or not isinstance(ids, list):
        raise ValueError("Invalid season")
    ids = [_numeric_id(value, "du match") for value in ids]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate season match")
    league = entry.get("league")
    if league is not None:
        league = _numeric_id(league, "de compétition")
    for match_id in ids:
        actual_code, actual_year, actual_league = _season_identity(matches[match_id])
        if (actual_code, actual_year) != (code, year) or league is not None and actual_league != league:
            raise ValueError("Mismatched season import")
        league = actual_league
    if imported_at is None:
        if entry.get("complete") is not True:
            raise ValueError("Incomplete season import")
        stamp = datetime.fromisoformat(entry["imported_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("Season import timezone missing")
        imported_at = stamp.astimezone(timezone.utc).isoformat(timespec="seconds")
        if type(entry.get("received_count")) is not int or entry["received_count"] != len(ids):
            raise ValueError("Season import count mismatch")
    return dict(competition_code=code, season=year, league=league, complete=True,
                match_ids=sorted(ids, key=int), received_count=len(ids), imported_at=imported_at)


def _valid_season_imports(payload, matches):
    entries = payload.get("season_imports", [])
    if not isinstance(entries, list):
        return []
    valid = {}
    for entry in entries:
        try:
            marker = _validate_season_import(entry, matches)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        identity = marker["competition_code"], marker["season"]
        if identity not in valid or marker["imported_at"] > valid[identity]["imported_at"]:
            valid[identity] = marker
    return list(valid.values())


def load_season_imports(path=STORE_PATH):
    """Read verified full-season import markers; mere match presence is insufficient.

    Invalid markers never establish coverage. Legacy stores without metadata are
    valid match stores, but have no known complete season until explicitly marked.
    """
    payload = _load_store(path)
    matches = {_numeric_id(row["id"], "du match"): row for row in payload["matches"]}
    return _valid_season_imports(payload, matches)


def _last_updated(match):
    """Only explicit, timezone-aware provider timestamps establish freshness."""
    try:
        stamp = datetime.fromisoformat(match["lastUpdated"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return stamp.astimezone(timezone.utc) if stamp.tzinfo is not None else None


def _may_replace(previous, incoming):
    old_stamp, new_stamp = _last_updated(previous), _last_updated(incoming)
    if old_stamp is not None and new_stamp is not None and new_stamp < old_stamp:
        return False
    # Calendar endpoints can return an older scheduled snapshot without reliable
    # timestamps. It must not erase a result already used by the model. Final
    # score corrections and nonfinal -> final transitions remain supported.
    if previous.get("status") == "FINISHED" and incoming.get("status") != "FINISHED":
        return False
    return True


def save_fixtures(fixtures, path=STORE_PATH, *, season_imports=None):
    """Atomically upsert without stale updates or a finished-status rollback.

    Comparable provider ``lastUpdated`` values take precedence over import order.
    A FINISHED record is retained against any nonfinal state; a newer FINISHED
    version may still correct its scores and metadata.

    ``season_imports`` is reserved for complete competition/season responses,
    never a daily calendar. Each entry supplies competition_code, season,
    match_ids, and optionally league. Markers and matches commit atomically.
    """
    path = Path(path)
    stored = _load_store(path)
    merged = {_numeric_id(row["id"], "du match"): row for row in stored["matches"]}
    incoming = {}
    try:
        for row in fixtures:
            match_id = _numeric_id(row["id"], "du match")
            incoming[match_id] = row
            if match_id not in merged or _may_replace(merged[match_id], row):
                merged[match_id] = row
    except (KeyError, TypeError, ValueError):
        raise FootballDataError("Un match football-data.org à enregistrer a un identifiant invalide.") from None
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    markers = {(row["competition_code"], row["season"]): row
               for row in _valid_season_imports(stored, merged)}
    if season_imports is not None:
        try:
            if not isinstance(season_imports, list):
                raise ValueError("Season imports must be a list")
            for entry in season_imports:
                marker = _validate_season_import(entry, incoming, imported_at=now)
                expected = {match_id for match_id, row in incoming.items()
                            if _season_identity(row)[:2] == (marker["competition_code"], marker["season"])}
                if set(marker["match_ids"]) != expected:
                    raise ValueError("Season import does not cover the supplied season batch")
                marker = _validate_season_import(marker, merged)
                markers[marker["competition_code"], marker["season"]] = marker
        except (KeyError, TypeError, ValueError, AttributeError):
            raise FootballDataError("Le manifeste de saison football-data.org est invalide ou ne correspond pas au lot reçu.") from None
    payload = dict(version=1, matches=list(merged.values()), season_imports=list(markers.values()), updated_at=now)
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
        raise FootballDataError("Impossible d’enregistrer les matchs football-data.org localement.") from None
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
    return len(merged)


def load_matches(path=STORE_PATH):
    """Only finished matches enter the model; completed invalid scores are audited."""
    fixtures = load_fixtures(path)
    rows, issues, expected = [], [], 0
    for fixture in fixtures:
        status = fixture.get("status")
        if not isinstance(status, str):
            expected += 1
            issues.append(dict(match_id=str(fixture.get("id", "?")), reason="Statut du match absent ou invalide"))
            continue
        if status not in FINISHED_STATUSES:
            continue
        expected += 1
        try:
            rows.append(normalize_fixture(fixture))
        except ValueError as error:
            issues.append(dict(match_id=str(fixture.get("id", "?")), reason=str(error)))
    rows.sort(key=lambda row: (row["timestamp"], int(row["match_id"])))
    return rows, dict(expected=expected, matched=len(rows), issues=issues)


def data_signature(path=STORE_PATH):
    """A cheap signature suitable for cache arguments."""
    path = Path(path)
    try:
        stat = path.stat()
    except FileNotFoundError:
        return str(path), None, None
    return str(path), stat.st_mtime_ns, stat.st_size
