"""Local Kinshasa calendar and explicit API-Football history imports."""
from datetime import date, datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import api_football as api


SOURCE_LABEL = "API-Football"
PROVIDER = "api-football"
DEFAULT_TIMEZONE = "Africa/Kinshasa"
STATUS_MAP = {
    "NS": "TIMED", "TBD": "SCHEDULED", "FT": "FINISHED", "AET": "FINISHED", "PEN": "FINISHED",
    "1H": "IN_PLAY", "2H": "IN_PLAY", "ET": "IN_PLAY", "BT": "IN_PLAY", "P": "IN_PLAY",
    "HT": "PAUSED", "SUSP": "SUSPENDED", "INT": "SUSPENDED", "ABD": "SUSPENDED",
    "PST": "POSTPONED", "CANC": "CANCELLED", "AWD": "AWARDED", "WO": "AWARDED",
}


def _path(path):
    return Path(path) if path is not None else Path(api.STORE_PATH)


def _bounds(day, timezone_name):
    if type(day) is not date:
        raise ValueError("La journée doit être une date")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise ValueError("Fuseau horaire inconnu") from None
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _stamp(raw):
    try:
        stamp = datetime.fromisoformat(raw["fixture"]["date"].replace("Z", "+00:00"))
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


def _day_rows(raw, day, timezone_name):
    start, end = _bounds(day, timezone_name)
    selected = []
    for item in raw:
        try:
            if start <= _stamp(item) < end:
                selected.append(item)
        except ValueError:
            continue
    return sorted(selected, key=lambda item: (_stamp(item), str(item.get("fixture", {}).get("id", ""))))


def local_day_matches(day, timezone_name=DEFAULT_TIMEZONE, *, path=None):
    return _day_rows(api.load_fixtures(_path(path)), day, timezone_name)


def fixture_from_raw(raw):
    """Create a score-free contextual fixture only for two confirmed participants."""
    try:
        event, competition = raw["fixture"], raw["league"]
        home, away = raw["teams"]["home"], raw["teams"]["away"]
        home_id = api._numeric_id(home["id"], "du club à domicile")
        away_id = api._numeric_id(away["id"], "du club à l’extérieur")
        home_name, away_name = home["name"], away["name"]
        unknown = {"tbd", "tba", "unknown", "to be determined"}
        if not all(isinstance(name, str) and name.strip() and name.strip().casefold() not in unknown
                   for name in (home_name, away_name)):
            raise ValueError("Les deux clubs doivent être confirmés")
        if home_id == away_id or home_name.strip() == away_name.strip():
            raise ValueError("Les deux clubs doivent être différents")
        league = api._numeric_id(competition["id"], "de compétition")
        season = competition.get("season")
        if type(season) is not int or not 1900 <= season <= 2100:
            season = None
        return dict(match_id=api._numeric_id(event["id"], "du match"), league=league,
                    competition_code=league, competition=competition.get("name"),
                    country=competition.get("country") or "International", home=home_name, away=away_name,
                    home_id=home_id, away_id=away_id, season=season,
                    timestamp=_stamp(raw).replace(tzinfo=None).isoformat(timespec="seconds"), provider=PROVIDER)
    except (KeyError, TypeError, AttributeError):
        raise ValueError("Affiche absente ou clubs non confirmés") from None


def fixtures_in_day(raw, day, now=None, *, timezone_name=DEFAULT_TIMEZONE):
    current = _now(now)
    rows = []
    for item in _day_rows(raw, day, timezone_name):
        try:
            fixture = fixture_from_raw(item)
        except ValueError:
            continue
        short = api._status_short(item)
        status = STATUS_MAP.get(short, "UNKNOWN")
        # TBD announces an unsettled kickoff; a placeholder time is not confirmation.
        future = short == "NS" and _stamp(item) > current
        historical = status == "FINISHED"
        selectable = future or historical
        reason = ""
        if historical:
            reason = "Match terminé : estimation rétrospective uniquement."
        elif short == "TBD":
            reason = "Horaire du match à confirmer."
        elif short == "NS" and not future:
            reason = "Horaire dépassé sans confirmation d’un match à venir."
        elif not selectable:
            reason = "Statut non confirmé pour un groupe de matchs à venir."
        rows.append(dict(fixture, status=status, raw_status=short, is_upcoming=future,
                         is_historical=historical, selectable=selectable, status_reason=reason))
    return sorted(rows, key=lambda row: (row["timestamp"], int(row["match_id"])))


def upcoming_in_day(raw, day, now=None, *, timezone_name=DEFAULT_TIMEZONE):
    return [row for row in fixtures_in_day(raw, day, now, timezone_name=timezone_name) if row["is_upcoming"]]


def _metadata(path):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload.get("updated_at") if isinstance(payload, dict) else None
    except (OSError, ValueError):
        return None


def _calendar_path(path):
    return path.with_name(path.stem + ".calendar.json")


def _calendar_updates(path):
    """Invalid or absent display metadata must never prevent reading match data."""
    try:
        payload = json.loads(_calendar_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if (not isinstance(payload, dict) or payload.get("version") != 1
            or payload.get("source") != SOURCE_LABEL or not isinstance(payload.get("calendar_updates"), list)):
        return {}
    updates = {}
    for entry in payload["calendar_updates"]:
        try:
            day = date.fromisoformat(entry["day"])
            zone = entry["timezone"]
            _bounds(day, zone)
            stamp = datetime.fromisoformat(entry["updated_at"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                continue
            stamp = stamp.astimezone(timezone.utc).isoformat(timespec="seconds")
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        key = day.isoformat(), zone
        if key not in updates or stamp > updates[key]:
            updates[key] = stamp
    return updates


def _mark_calendar_refresh(path, day, timezone_name):
    """Persist only successful per-day refresh times in an atomic metadata sidecar."""
    updates = _calendar_updates(path)
    updates[day.isoformat(), timezone_name] = _now(None).isoformat(timespec="seconds")
    payload = dict(version=1, source=SOURCE_LABEL, calendar_updates=[
        dict(day=key[0], timezone=key[1], updated_at=stamp)
        for key, stamp in sorted(updates.items())])
    sidecar = _calendar_path(path)
    temporary = None
    try:
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=sidecar.parent,
                                         prefix=f".{sidecar.name}.", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, sidecar)
    except OSError:
        raise api.ApiFootballError("Les matchs ont été enregistrés, mais la date de leur actualisation n’a pas pu être sauvegardée.") from None
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _usable_rows(raw):
    usable = []
    for item in raw:
        if api._status_short(item) not in api.FINISHED_STATUSES:
            continue
        try:
            usable.append(api.normalize_fixture(item))
        except ValueError:
            continue
    return usable


def local_snapshot(day, *, timezone_name=DEFAULT_TIMEZONE, now=None, path=None):
    """Read only this provider's local calendar and historical result counts."""
    path = _path(path)
    raw = api.load_fixtures(path)
    day_matches = _day_rows(raw, day, timezone_name)
    fixtures = fixtures_in_day(day_matches, day, now, timezone_name=timezone_name)
    upcoming = [row for row in fixtures if row["is_upcoming"]]
    usable, day_usable = _usable_rows(raw), _usable_rows(day_matches)
    history_counts = {}
    for row in usable:
        history_counts[row["league"]] = history_counts.get(row["league"], 0) + 1
    competitions = {}
    for item in day_matches:
        try:
            competition = item["league"]
            league = api._numeric_id(competition["id"], "de compétition")
        except (KeyError, TypeError, ValueError):
            continue
        season = competition.get("season")
        season = season if type(season) is int else None
        info = competitions.setdefault(league, dict(id=league, league=league, code=league,
            name=competition.get("name") or "Compétition inconnue", country=competition.get("country") or "International",
            matches=0, upcoming=0, usable=0, history_matches=history_counts.get(league, 0), current_season=season))
        info["matches"] += 1
        if info["current_season"] is None:
            info["current_season"] = season
    for row in upcoming:
        competitions[row["league"]]["upcoming"] += 1
    for row in day_usable:
        competitions[row["league"]]["usable"] += 1
    return dict(day=day.isoformat(), timezone=timezone_name, matches=day_matches, fixtures=fixtures,
                upcoming=upcoming, competitions=competitions, stored_count=len(raw), usable_count=len(usable),
                day_count=len(day_matches), day_usable_count=len(day_usable), updated_at=_metadata(path),
                calendar_updated_at=_calendar_updates(path).get((day.isoformat(), timezone_name)),
                source_label=SOURCE_LABEL, history_counts=history_counts)


def _quota(client):
    value = getattr(client, "last_quota", {})
    if not isinstance(value, dict):
        return {}
    return {key: number for key, number in value.items()
            if key in ("requests_remaining_day", "requests_limit_day", "requests_available_minute", "reset_seconds")
            and type(number) is int and number >= 0}


def _response_fixtures(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("response"), list):
        raise api.ApiFootballError("Liste de matchs API-Football invalide.")
    if payload.get("errors"):
        raise api.ApiFootballError("API-Football a refusé la demande : aucun match enregistré.")
    paging = payload.get("paging", {"current": 1, "total": 1})
    if (not isinstance(paging, dict) or type(paging.get("total", 1)) is not int
            or type(paging.get("current", 1)) is not int or paging.get("total", 1) != 1 or paging.get("current", 1) != 1):
        raise api.ApiFootballError("Réponse API-Football paginée ou incomplète : aucun match enregistré.")
    fixtures, ids = payload["response"], set()
    if "results" in payload and (type(payload["results"]) is not int or payload["results"] != len(fixtures)):
        raise api.ApiFootballError("Le nombre de matchs API-Football reçus ne correspond pas à la réponse annoncée.")
    try:
        for raw in fixtures:
            match_id = api._numeric_id(raw["fixture"]["id"], "du match")
            if match_id in ids:
                raise ValueError("Duplicate fixture")
            ids.add(match_id)
    except (KeyError, TypeError, ValueError):
        raise api.ApiFootballError("Un match reçu d’API-Football a un identifiant invalide ou répété.") from None
    return fixtures


def _client():
    key = api.load_key()
    return api.ApiFootballClient(key), key


def _safe(error, key):
    return str(error).replace(key, "[clé masquée]") if key else str(error)


def refresh_day(day, *, timezone_name=DEFAULT_TIMEZONE, path=None):
    """One explicit date/timezone request; save all returned rows and filter locally."""
    _bounds(day, timezone_name)
    path, key = _path(path), ""
    try:
        client, key = _client()
        fixtures = _response_fixtures(client.fixtures(date=day.isoformat(), timezone=timezone_name))
        count = api.save_fixtures(fixtures, path)
        _mark_calendar_refresh(path, day, timezone_name)
    except api.ApiFootballError as error:
        raise api.ApiFootballError(_safe(error, key)) from None
    snapshot = local_snapshot(day, timezone_name=timezone_name, path=path)
    snapshot.update(received_count=len(fixtures), updated_count=len(fixtures), stored_count=count, quota=_quota(client))
    return snapshot


def _selection(competition_code, seasons):
    code = api._numeric_id(competition_code, "de compétition")
    try:
        years = list(seasons)
    except TypeError:
        raise ValueError("Choisis des années de début de saison") from None
    if any(type(year) is not int or not 1900 <= year <= 2100 for year in years):
        raise ValueError("Choisis des années de début de saison valides")
    return code, list(dict.fromkeys(years))


def missing_history(competition_code, seasons, path=None):
    code, years = _selection(competition_code, seasons)
    imported = {(marker["competition_code"], marker["season"]) for marker in api.load_season_imports(_path(path))}
    return [year for year in years if (code, year) not in imported]


def ensure_history(competition_code, seasons, *, path=None):
    """Fetch at most two complete season responses and atomically commit their markers."""
    code, years = _selection(competition_code, seasons)
    if not 1 <= len(years) <= 2:
        raise ValueError("Choisis une ou deux années de début de saison")
    path, key = _path(path), ""
    collected, reports, markers = [], [], []
    try:
        client, key = _client()
        for year in years:
            fixtures = _response_fixtures(client.fixtures(league=code, season=year, timezone="UTC"))
            issues, usable = [], 0
            for raw in fixtures:
                try:
                    actual_code, actual_year = api._season_identity(raw)
                except (KeyError, TypeError, ValueError):
                    raise api.ApiFootballError("Compétition ou saison invalide dans la réponse API-Football.") from None
                if (actual_code, actual_year) != (code, year):
                    raise api.ApiFootballError("La réponse ne correspond pas à la compétition et à la saison demandées.")
                if api._status_short(raw) in api.FINISHED_STATUSES:
                    try:
                        api.normalize_fixture(raw)
                        usable += 1
                    except ValueError as error:
                        issues.append(dict(match_id=str(raw["fixture"]["id"]), reason=str(error)))
            collected.extend(fixtures)
            reports.append(dict(season=year, received=len(fixtures), usable=usable, issues=issues))
            markers.append(dict(competition_code=code, season=year, league=code,
                                match_ids=[str(raw["fixture"]["id"]) for raw in fixtures]))
        count = api.save_fixtures(collected, path, season_imports=markers)
    except api.ApiFootballError as error:
        raise api.ApiFootballError(_safe(error, key)) from None
    history_count = sum(row["league"] == code for row in _usable_rows(api.load_fixtures(path)))
    return dict(source_label=SOURCE_LABEL, competition_code=code, league=code, seasons=years,
                received_count=len(collected), stored_count=count, usable_count=sum(row["usable"] for row in reports),
                history_count=history_count, reports=reports, quota=_quota(client), updated_at=_metadata(path))
