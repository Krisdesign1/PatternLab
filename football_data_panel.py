"""Explicit football-data.org access, season preview, and local import controls."""
from datetime import datetime, timezone
import json

import streamlit as st

from football_data import (
    DATA_DIR, FINISHED_STATUSES, STORE_PATH, FootballDataClient,
    FootballDataError, load_fixtures, load_key, normalize_fixture, save_fixtures,
)


CHECK_PATH = DATA_DIR / "access_check.json"


def _safe(message, key):
    return str(message).replace(key, "[clé masquée]") if key else str(message)


def _check_report():
    try:
        report = json.loads(CHECK_PATH.read_text(encoding="utf-8"))
        return report if isinstance(report, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_check(report):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CHECK_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def _quota(client):
    """Retain numeric quota counters only; never persist response headers or tokens."""
    quota = getattr(client, "last_quota", {})
    if not isinstance(quota, dict):
        return {}
    return {name: int(value) for name, value in quota.items()
            if name in ("requests_available_minute", "reset_seconds")
            and (type(value) is int or isinstance(value, str) and value.isdigit())}


def _show_quota(quota):
    if quota:
        labels = {"requests_available_minute": "requêtes restantes cette minute",
                  "reset_seconds": "réinitialisation dans (secondes)"}
        st.caption("Quota communiqué par football-data.org : " +
                   " · ".join(f"{labels[name]} : {value}" for name, value in quota.items()
                              if name in labels))


def _dict(value):
    return value if isinstance(value, dict) else {}


def fixture_preview(fixtures):
    """Preview calendar rows separately from validated regulation-time results."""
    table, usable, excluded = [], 0, []
    for raw in fixtures:
        item = _dict(raw)
        competition = _dict(item.get("competition"))
        home, away = _dict(item.get("homeTeam")), _dict(item.get("awayTeam"))
        raw_status = item.get("status")
        status = raw_status if isinstance(raw_status, str) else "Statut invalide"
        row = {"Identifiant": item.get("id"), "Date (UTC)": item.get("utcDate"),
               "Compétition": competition.get("name"), "Domicile": home.get("name"),
               "Extérieur": away.get("name"), "Statut": status,
               "Score à la pause": "", "Score à 90 min": "", "Résultat": ""}
        if status in FINISHED_STATUSES:
            try:
                match = normalize_fixture(item)
                row.update({"Score à la pause": match["ht"], "Score à 90 min": match["ft"],
                            "Résultat": match["value"]})
                usable += 1
            except ValueError as error:
                excluded.append({"Identifiant": item.get("id"), "Motif": str(error)})
        table.append(row)
    return table, usable, excluded


def _season_year(competition):
    start = _dict(competition.get("currentSeason")).get("startDate")
    try:
        return datetime.strptime(start, "%Y-%m-%d").year
    except (TypeError, ValueError):
        return None


def _render_import(client, key):
    competitions = st.session_state.get("fd_competitions", [])
    if competitions:
        show_others = st.checkbox("Afficher aussi les compétitions des autres offres",
                                  key="fd_show_other_plans")
        if not show_others:
            competitions = [item for item in competitions if isinstance(item, dict)
                            and item.get("plan") in (None, "TIER_ONE")]
    indexed = {item["code"]: item for item in competitions
               if isinstance(item, dict) and isinstance(item.get("code"), str) and item["code"]}
    if not indexed:
        return
    selected = st.selectbox(
        "Compétition football-data.org", list(indexed),
        format_func=lambda code: f"{_dict(indexed[code].get('area')).get('name', '')} — "
                                 f"{indexed[code].get('name', code)} ({code})",
        key="fd_competition_selected",
    )
    current_year = _season_year(indexed[selected])
    season = None
    if current_year is not None:
        season = st.selectbox("Saison annoncée par l’API (année de début)", [current_year],
                              key="fd_season_selected")
    else:
        st.info("La saison courante n’est pas indiquée. Renseigne son année de début ci-dessous.")
    override = st.text_input("Autre année de début de saison (facultatif)",
                             placeholder="Exemple : 2025", key="fd_season_override").strip()
    valid_season = not override or (len(override) == 4 and override.isdigit()
                                    and 1900 <= int(override) <= 2100)
    if override and valid_season:
        season = int(override)
    elif not valid_season:
        st.warning("Indique une année de début sur quatre chiffres.")
    st.caption("L’année désigne le début de la saison : 2026 pour 2026–2027. "
               "L’accès à la compétition et à l’historique dépend des droits du compte ; "
               "la liste ne garantit pas l’accès à chaque saison.")
    context = (selected, season) if valid_season and season is not None else None
    if st.button("Charger les matchs de cette saison", icon=":material/cloud_download:",
                 key="fd_fetch_season", disabled=context is None):
        st.session_state.pop("fd_fixture_batch", None)
        try:
            with st.spinner("Chargement des résultats et du calendrier…"):
                payload = client.matches(selected, season=season)
            if not isinstance(payload, dict) or not isinstance(payload.get("matches"), list):
                raise FootballDataError("Liste de matchs football-data.org invalide.")
            st.session_state["fd_fixture_batch"] = {"context": context, "fixtures": payload["matches"]}
            st.session_state["fd_latest_quota"] = _quota(client)
        except FootballDataError as error:
            st.error(_safe(error, key))
            st.session_state["fd_latest_quota"] = _quota(client)
    batch = st.session_state.get("fd_fixture_batch")
    if batch and context is not None and batch.get("context") == context:
        table, usable, excluded = fixture_preview(batch["fixtures"])
        st.markdown(f"**{len(table)} matchs reçus**, dont **{usable} résultats HT/FT utilisables**.")
        if table:
            st.dataframe(table[:50], hide_index=True, width="stretch")
            st.caption("Aperçu limité aux 50 premiers matchs. L’enregistrement conserve tout le lot, "
                       "y compris le calendrier. Seuls les matchs terminés avec scores HT/90 min "
                       "valides entrent dans les analyses.")
            if st.button("Enregistrer ce lot dans PatternLab", icon=":material/save:", key="fd_import"):
                try:
                    count = save_fixtures(batch["fixtures"], season_imports=[
                        {"competition_code": selected, "season": season,
                         "match_ids": [row["id"] for row in batch["fixtures"]]}
                    ])
                    st.success(f"{count} matchs football-data.org conservés au total. "
                               "Retrouvez les précédents dans Explorer les scénarios et les matchs dans Analyser des matchs.")
                except FootballDataError as error:
                    st.error(_safe(error, key))
        if excluded:
            st.warning(f"{len(excluded)} match(s) terminé(s) exclus des analyses : "
                       "scores HT/90 min absents ou incohérents.")
            st.dataframe(excluded, hide_index=True, width="stretch")


def _render_stored(key):
    try:
        stored = load_fixtures()
    except FootballDataError as error:
        st.error(_safe(error, key))
        return
    if stored:
        table, usable, excluded = fixture_preview(stored)
        st.caption(f"{len(stored)} matchs enregistrés localement · {usable} résultats utilisables · "
                   f"{len(excluded)} résultats exclus.")
        with st.expander("Calendrier et résultats football-data.org enregistrés"):
            st.dataframe(table, hide_index=True, width="stretch")
            try:
                backup = STORE_PATH.read_bytes()
            except OSError:
                st.warning("La sauvegarde locale n’a pas pu être lue.")
            else:
                st.download_button("Sauvegarder les matchs football-data.org", backup,
                                   file_name="football_data_fixtures.json", mime="application/json",
                                   key="fd_backup")


def render_football_data():
    st.subheader("Connexion football-data.org")
    st.caption("Les appels sont effectués uniquement avec les boutons ci-dessous. "
               "football-data.org reste une source distincte de Forebet et d’API-Football.")
    for name, default in (("fd_competitions", []), ("fd_latest_quota", {})):
        st.session_state.setdefault(name, default)
    key = ""
    try:
        key = load_key()
        client = FootballDataClient(key) if key else None
    except FootballDataError as error:
        st.error(_safe(error, key))
        _render_stored(key)
        return
    if client is None:
        st.info("Configure la clé dans .streamlit/secrets.toml, section [football_data], "
                "champ key, puis relance ce panneau.")
        _render_stored(key)
        return
    st.caption("Clé locale configurée.")
    report = _check_report()
    if st.button("Vérifier l’accès et charger les compétitions", icon=":material/sync:", key="fd_check"):
        report = {"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "ok": False}
        st.session_state["fd_competitions"] = []
        st.session_state.pop("fd_fixture_batch", None)
        try:
            with st.spinner("Vérification de l’accès aux compétitions…"):
                payload = client.competitions()
            if not isinstance(payload, dict) or not isinstance(payload.get("competitions"), list):
                raise FootballDataError("Liste de compétitions football-data.org invalide.")
            st.session_state["fd_competitions"] = payload["competitions"]
            report.update(ok=True, competitions_count=len(payload["competitions"]),
                          free_competitions_count=sum(isinstance(item, dict) and item.get("plan") == "TIER_ONE"
                                                      for item in payload["competitions"]))
        except FootballDataError as error:
            report["error"] = _safe(error, key)
        report["quota"] = _quota(client)
        st.session_state["fd_latest_quota"] = report["quota"]
        try:
            _save_check(report)
        except OSError:
            st.warning("Le contrôle a abouti, mais son rapport local n’a pas pu être enregistré.")
    if report.get("error"):
        st.warning("Dernier contrôle : " + _safe(report["error"], key))
    elif report.get("ok") or report.get("access"):
        count = report.get("competitions_count", len(report.get("competitions", [])))
        st.success(f"Dernier contrôle : catalogue reçu · {count} compétitions répertoriées.")
        st.caption("La présence dans le catalogue ne garantit pas les droits sur les matchs. "
                   "Le chargement de la saison vérifie cet accès.")
    _render_import(client, key)
    _show_quota(st.session_state["fd_latest_quota"] or report.get("quota", {}))
    _render_stored(key)
