"""API-Football calendar display, independent of model and selection fixtures."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_STATUSES = {
    "NS": ("TIMED", "À venir", "upcoming"),
    "TBD": ("SCHEDULED", "Horaire à confirmer", "upcoming"),
    "1H": ("IN_PLAY", "Première mi-temps", "live"),
    "HT": ("PAUSED", "Mi-temps", "live"),
    "2H": ("IN_PLAY", "Seconde mi-temps", "live"),
    "ET": ("IN_PLAY", "Prolongations", "live"),
    "BT": ("PAUSED", "Pause des prolongations", "live"),
    "P": ("IN_PLAY", "Tirs au but", "live"),
    "LIVE": ("IN_PLAY", "En cours", "live"),
    "FT": ("FINISHED", "Terminé", "finished"),
    "AET": ("FINISHED", "Terminé après prolongations", "finished"),
    "PEN": ("FINISHED", "Terminé après tirs au but", "finished"),
    "PST": ("POSTPONED", "Reporté", "other"),
    "CANC": ("CANCELLED", "Annulé", "other"),
    "SUSP": ("SUSPENDED", "Suspendu", "other"),
    "INT": ("SUSPENDED", "Interrompu", "other"),
    "ABD": ("SUSPENDED", "Abandonné", "other"),
    "AWD": ("AWARDED", "Résultat attribué", "other"),
    "WO": ("AWARDED", "Forfait", "other"),
}
_FILTERS = {"tous": None, "all": None, "à venir": "upcoming", "a venir": "upcoming", "upcoming": "upcoming",
            "en cours": "live", "live": "live", "terminés": "finished", "terminé": "finished",
            "termines": "finished", "finished": "finished", "autres": "other", "other": "other"}


def _dict(value):
    return value if isinstance(value, dict) else {}


def _number(value):
    return value if type(value) is int and value >= 0 else None


def _id(value, *, required=False):
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isdigit() or int(value) <= 0:
        if required:
            raise ValueError("Identifiant du match ou de compétition absent ou invalide")
        return None
    return str(int(value))


def _pair(value):
    pair = _dict(value)
    home, away = _number(pair.get("home")), _number(pair.get("away"))
    return home, away


def _score(home, away):
    return f"{home}–{away}" if home is not None and away is not None else "—"


def _team(value):
    item = _dict(value)
    name = item.get("name")
    known = (isinstance(name, str) and bool(name.strip())
             and name.strip().casefold() not in ("tbd", "tba", "unknown", "to be determined"))
    team_id = _id(item.get("id"))
    return team_id, name.strip() if known else "Club à confirmer", bool(known and team_id)


def from_raw(raw):
    """Return a display row, including live scores without treating them as results.

    ``score`` follows the provider's current goals (possibly including overtime).
    ``regulation_score`` is separately taken from score.fulltime after FT/AET/PEN.
    Missing and negative sentinel values remain unknown, never a manufactured 0.
    """
    if not isinstance(raw, dict):
        raise ValueError("Match API-Football absent ou invalide")
    event, league, teams = _dict(raw.get("fixture")), _dict(raw.get("league")), _dict(raw.get("teams"))
    try:
        stamp = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError("Date du match absente ou invalide") from None
    if stamp.tzinfo is None:
        raise ValueError("Fuseau horaire du match absent")
    stamp = stamp.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    state = _dict(event.get("status"))
    raw_status = state.get("short") if isinstance(state.get("short"), str) else "UNKNOWN"
    status, label, group = _STATUSES.get(raw_status, ("UNKNOWN", "Statut non confirmé", "other"))
    elapsed, extra = _number(state.get("elapsed")), _number(state.get("extra"))
    minute = f"{elapsed}" + (f"+{extra}" if extra else "") + "′" if elapsed is not None else ""
    home_id, home, home_known = _team(teams.get("home"))
    away_id, away, away_known = _team(teams.get("away"))
    confirmed = home_known and away_known and home_id != away_id and home != away
    home_goals, away_goals = _pair(raw.get("goals"))
    scores = _dict(raw.get("score"))
    ht_home, ht_away = _pair(scores.get("halftime"))
    full_home, full_away = _pair(scores.get("fulltime")) if group == "finished" else (None, None)
    phase = "regulation" if raw_status in ("NS", "TBD", "1H", "HT", "2H", "FT") else (
            "extra_time" if raw_status in ("ET", "BT", "AET") else
            "penalties" if raw_status in ("P", "PEN") else "unknown")
    return dict(match_id=_id(event.get("id"), required=True), league=_id(league.get("id"), required=True),
                competition=league.get("name") or "Compétition inconnue", country=league.get("country") or "International",
                home=home, away=away, home_id=home_id, away_id=away_id, timestamp=stamp,
                provider="api-football", raw_status=raw_status, status=status, status_label=label, status_group=group,
                phase=phase, elapsed=elapsed, extra=extra, minute=minute,
                home_goals=home_goals, away_goals=away_goals, score=_score(home_goals, away_goals),
                ht=_score(ht_home, ht_away), halftime_score=_score(ht_home, ht_away),
                halftime_home=ht_home, halftime_away=ht_away,
                regulation_score=_score(full_home, full_away), regulation_home=full_home, regulation_away=full_away,
                teams_confirmed=bool(confirmed), selectable=bool(confirmed and raw_status == "NS"))


def day_rows(raw, day, zone="Africa/Kinshasa"):
    """Display all valid matches in a strict local day, including unconfirmed teams."""
    if type(day) is not date:
        raise ValueError("La journée doit être une date")
    try:
        local = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise ValueError("Fuseau horaire inconnu") from None
    start = datetime.combine(day, time.min, tzinfo=local).astimezone(timezone.utc).replace(tzinfo=None)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=local).astimezone(timezone.utc).replace(tzinfo=None)
    rows = []
    for item in raw:
        try:
            row = from_raw(item)
        except ValueError:
            continue
        if start <= datetime.fromisoformat(row["timestamp"]) < end:
            rows.append(row)
    return sorted(rows, key=lambda row: (row["timestamp"], int(row["match_id"])))


def filter_rows(rows, league=None, status="Tous", query=""):
    """Filter display rows by exact competition ID, status family, and club text."""
    if not isinstance(status, str) or not isinstance(query, str):
        raise ValueError("Filtre de calendrier invalide")
    selection = status.strip().casefold()
    if selection in _FILTERS:
        group, canonical = _FILTERS[selection], None
    else:
        group, canonical = None, status.upper()
        if canonical not in {item[0] for item in _STATUSES.values()}:
            raise ValueError("Statut de calendrier inconnu")
    search = query.strip().casefold()
    return [row for row in rows
            if (league is None or str(row["league"]) == str(league))
            and (group is None or row["status_group"] == group)
            and (canonical is None or row["status"] == canonical)
            and (not search or search in f"{row['home']} {row['away']} {row['competition']} {row['country']}".casefold())]
