"""Local-day match selection and explicit football-data.org synchronisation.

Read helpers never access the network or credentials. All dates used for API
requests are UTC; a strict local-day filter defines what the user sees. Imports
upsert the raw provider records, preserving every previously stored season.
"""
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import football_data


SOURCE_LABEL = "football-data.org"
DEFAULT_TIMEZONE = "Africa/Kinshasa"
FUTURE_STATUSES = frozenset(("TIMED", "SCHEDULED"))
HISTORICAL_STATUSES = frozenset(("FINISHED",))


def _path(path):
    return Path(path) if path is not None else Path(football_data.STORE_PATH)


def _zone(timezone_name):
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise ValueError("Fuseau horaire inconnu") from None


def _bounds(day, timezone_name):
    if type(day) is not date:
        raise ValueError("La journée doit être une date")
    zone = _zone(timezone_name)
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc), zone


def _stamp(raw):
    try:
        stamp = datetime.fromisoformat(raw["utcDate"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError("Date du match absente ou invalide") from None
    if stamp.tzinfo is None:
        raise ValueError("Fuseau horaire du match absent")
    return stamp.astimezone(timezone.utc)


def _now(now):
    if now is None:
        return datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("L’heure actuelle doit préciser son fuseau horaire")
    return now.astimezone(timezone.utc)


def _identifier(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isdigit() or int(value) <= 0:
        raise ValueError(f"Identifiant {label} absent ou invalide")
    return str(int(value))


def _day_rows(matches, day, timezone_name):
    start, end, _ = _bounds(day, timezone_name)
    selected = []
    for raw in matches:
        try:
            stamp = _stamp(raw)
            if start <= stamp < end:
                selected.append(raw)
        except ValueError:
            continue
    return sorted(selected, key=lambda raw: (_stamp(raw), str(raw.get("id", ""))))


def local_day_matches(day, timezone_name=DEFAULT_TIMEZONE, *, path=None):
    """Return every locally stored match on this local date, including nonfinal statuses."""
    return _day_rows(football_data.load_fixtures(_path(path)), day, timezone_name)


def fixture_from_raw(raw):
    """Create a score-free contextual fixture; unconfirmed participants are rejected."""
    try:
        home, away, competition = raw["homeTeam"], raw["awayTeam"], raw["competition"]
        home_id = _identifier(home["id"], "du club à domicile")
        away_id = _identifier(away["id"], "du club à l’extérieur")
        home_name, away_name = home["name"], away["name"]
        placeholders = {"tbd", "tba", "unknown", "to be determined"}
        if not all(isinstance(name, str) and name.strip() and name.strip().casefold() not in placeholders
                   for name in (home_name, away_name)):
            raise ValueError("Les deux clubs doivent être confirmés")
        if home_id == away_id or home_name.strip() == away_name.strip():
            raise ValueError("Les deux clubs doivent être différents")
        stamp = _stamp(raw)
        area = raw.get("area") if isinstance(raw.get("area"), dict) else {}
        try:
            season = date.fromisoformat(raw.get('season', {}).get('startDate', '')).year
        except (ValueError, TypeError, AttributeError):
            season = None
        return dict(match_id=_identifier(raw["id"], "du match"),
                    league=_identifier(competition["id"], "de compétition"),
                    competition_code=competition.get("code"), competition=competition.get("name"),
                    country=area.get("name") or "International",
                    home=home_name, away=away_name, home_id=home_id, away_id=away_id, season=season,
                    timestamp=stamp.replace(tzinfo=None).isoformat(timespec="seconds"),
                    provider=SOURCE_LABEL)
    except (KeyError, TypeError, AttributeError):
        raise ValueError("Affiche absente ou clubs non confirmés") from None


def fixtures_in_day(raw, day, now=None, *, timezone_name=DEFAULT_TIMEZONE):
    """Return confirmed-team fixtures on a date, with explicit selection-status flags.

    Finished matches remain selectable for a retrospective check. Scheduled
    kickoffs already elapsed, live, postponed, and cancelled matches are disabled.
    Unknown participants remain visible in ``local_snapshot()['matches']`` only.
    """
    current = _now(now)
    rows = []
    for item in _day_rows(raw, day, timezone_name):
        try:
            fixture = fixture_from_raw(item)
        except ValueError:
            continue
        status = item.get("status")
        status = status if isinstance(status, str) else "UNKNOWN"
        future = status in FUTURE_STATUSES and _stamp(item) > current
        historical = status in HISTORICAL_STATUSES
        selectable = future or historical
        reason = ""
        if historical:
            reason = "Match terminé : estimation rétrospective uniquement."
        elif status in FUTURE_STATUSES and not future:
            reason = "Horaire dépassé sans confirmation d’un match à venir."
        elif not selectable:
            reason = "Statut non confirmé pour un groupe de matchs à venir."
        rows.append(dict(fixture, status=status, is_upcoming=future, selectable=selectable,
                         is_historical=historical, status_reason=reason))
    return sorted(rows, key=lambda row: (row["timestamp"], int(row["match_id"])))


def upcoming_in_day(raw, day, now=None, *, timezone_name=DEFAULT_TIMEZONE):
    """Select only future TIMED/SCHEDULED fixtures with two confirmed clubs."""
    return [row for row in fixtures_in_day(raw, day, now, timezone_name=timezone_name) if row["is_upcoming"]]


def _metadata(path):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload.get("updated_at") if isinstance(payload, dict) else None
    except (OSError, ValueError):
        return None


def _usable_rows(raw):
    rows = []
    for match in raw:
        if match.get("status") != "FINISHED":
            continue
        try:
            rows.append(football_data.normalize_fixture(match))
        except ValueError:
            continue
    return rows


def _snapshot(raw, day, timezone_name, now, path):
    day_matches = _day_rows(raw, day, timezone_name)
    fixtures = fixtures_in_day(day_matches, day, now, timezone_name=timezone_name)
    upcoming = [row for row in fixtures if row["is_upcoming"]]
    usable = _usable_rows(raw)
    day_usable = _usable_rows(day_matches)
    history_counts = {}
    for row in usable:
        history_counts[row["league"]] = history_counts.get(row["league"], 0) + 1
    competitions = {}
    for item in day_matches:
        try:
            competition = item["competition"]
            league = _identifier(competition["id"], "de compétition")
        except (KeyError, TypeError, ValueError):
            continue
        area = item.get("area") if isinstance(item.get("area"), dict) else {}
        start = item.get("season", {}).get("startDate") if isinstance(item.get("season"), dict) else None
        try:
            current_season = date.fromisoformat(start).year
        except (TypeError, ValueError):
            current_season = None
        info = competitions.setdefault(league, dict(id=league, league=league, code=competition.get("code"),
                    name=competition.get("name") or "Compétition inconnue", country=area.get("name") or "International",
                    matches=0, upcoming=0, usable=0, history_matches=history_counts.get(league, 0),
                    current_season=current_season))
        info["matches"] += 1
        if info["current_season"] is None and current_season is not None:
            info["current_season"] = current_season
    for fixture in upcoming:
        competitions[fixture["league"]]["upcoming"] += 1
    for row in day_usable:
        if row["league"] in competitions:
            competitions[row["league"]]["usable"] += 1
    return dict(day=day.isoformat(), timezone=timezone_name, matches=day_matches, fixtures=fixtures,
                upcoming=upcoming, competitions=competitions, stored_count=len(raw), usable_count=len(usable),
                day_count=len(day_matches), day_usable_count=len(day_usable), updated_at=_metadata(path),
                source_label=SOURCE_LABEL, history_counts=history_counts)


def local_snapshot(day, *, timezone_name=DEFAULT_TIMEZONE, now=None, path=None):
    """Read the calendar, date-specific competitions, and history counts without API calls."""
    path = _path(path)
    return _snapshot(football_data.load_fixtures(path), day, timezone_name, now, path)


def _quota(client):
    raw = getattr(client, "last_quota", {})
    if not isinstance(raw, dict):
        return {}
    return {name: value for name, value in raw.items()
            if name in ("requests_available_minute", "reset_seconds") and type(value) is int and value >= 0}


def _client():
    key = football_data.load_key()
    return football_data.FootballDataClient(key), key


def _safe_error(error, key):
    return str(error).replace(key, "[clé masquée]") if key else str(error)


def _response_matches(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("matches"), list):
        raise football_data.FootballDataError("Liste de matchs football-data.org invalide.")
    matches = payload["matches"]
    try:
        for match in matches:
            _identifier(match["id"], "du match")
    except (KeyError, TypeError, ValueError):
        raise football_data.FootballDataError("Un match reçu de football-data.org a un identifiant invalide.") from None
    return matches


def refresh_day(day, *, timezone_name=DEFAULT_TIMEZONE, path=None):
    """Make one explicit API request, persist all returned rows, and show only the local day."""
    start, end, _ = _bounds(day, timezone_name)
    path = _path(path)
    key = ""
    try:
        client, key = _client()
        payload = client.get("matches", dateFrom=start.date().isoformat(),
                             dateTo=(end.date() + timedelta(days=1)).isoformat())
        matches = _response_matches(payload)
        count = football_data.save_fixtures(matches, path)
    except football_data.FootballDataError as error:
        raise football_data.FootballDataError(_safe_error(error, key)) from None
    snapshot = local_snapshot(day, timezone_name=timezone_name, path=path)
    snapshot.update(received_count=len(matches), updated_count=len({str(row["id"]) for row in matches}),
                    stored_count=count, quota=_quota(client))
    return snapshot


def _history_selection(competition_code, seasons):
    if not isinstance(competition_code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", competition_code):
        raise ValueError("Code de compétition invalide")
    try:
        years = list(seasons)
    except TypeError:
        raise ValueError("Choisis des années de début de saison") from None
    if any(type(year) is not int or not 1900 <= year <= 2100 for year in years):
        raise ValueError("Choisis des années de début de saison valides")
    return competition_code.upper(), list(dict.fromkeys(years))


def missing_history(competition_code, seasons, path=None):
    """Return seasons without a verified complete-import marker, never infer from FT rows."""
    code, years = _history_selection(competition_code, seasons)
    imported = {(entry["competition_code"], entry["season"])
                for entry in football_data.load_season_imports(_path(path))}
    return [year for year in years if (code, year) not in imported]


def ensure_history(competition_code, seasons, *, path=None):
    """Fetch one or two explicitly chosen seasons, then commit one atomic upsert.

    Every response is collected and checked before any write. A quota or network
    failure on the second request therefore leaves the previous store intact.
    """
    code, years = _history_selection(competition_code, seasons)
    if not 1 <= len(years) <= 2:
        raise ValueError("Choisis une ou deux années de début de saison")
    path = _path(path)
    key, collected, reports, markers, league = "", [], [], [], None
    try:
        client, key = _client()
        for year in years:
            matches = _response_matches(client.matches(code, season=year))
            issues, usable = [], 0
            for raw in matches:
                competition = raw.get("competition")
                if not isinstance(competition, dict) or competition.get("code") != code:
                    raise football_data.FootballDataError("La réponse contient une autre compétition que celle demandée.")
                try:
                    actual_league = _identifier(competition.get("id"), "de compétition")
                    start = date.fromisoformat(raw["season"]["startDate"])
                except (KeyError, TypeError, ValueError):
                    raise football_data.FootballDataError("Compétition ou saison invalide dans la réponse football-data.org.") from None
                if start.year != year or league is not None and actual_league != league:
                    raise football_data.FootballDataError("La réponse ne correspond pas à la compétition et à la saison demandées.")
                league = actual_league
                if raw.get("status") == "FINISHED":
                    try:
                        football_data.normalize_fixture(raw)
                        usable += 1
                    except ValueError as error:
                        issues.append(dict(match_id=str(raw["id"]), reason=str(error)))
            collected.extend(matches)
            reports.append(dict(season=year, received=len(matches), usable=usable, issues=issues))
            markers.append(dict(competition_code=code, season=year, league=league,
                                match_ids=[str(raw["id"]) for raw in matches]))
        count = football_data.save_fixtures(collected, path, season_imports=markers)
    except football_data.FootballDataError as error:
        raise football_data.FootballDataError(_safe_error(error, key)) from None
    stored = football_data.load_fixtures(path)
    history_count = sum(row["league"] == league for row in _usable_rows(stored)) if league else 0
    return dict(source_label=SOURCE_LABEL, competition_code=code, league=league, seasons=years,
                received_count=len(collected), stored_count=count, usable_count=sum(row["usable"] for row in reports),
                history_count=history_count, reports=reports, quota=_quota(client), updated_at=_metadata(path))
