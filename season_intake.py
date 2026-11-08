"""Prevalidate full-season replies without relaxing the frozen import policy.

The service holds the original worker lock while this boundary runs. A rejected
reply raises the existing provider-error type: the unchanged history scheduler
records its attempted request, defers that job for six hours, and continues.
No season manifest, fixture, prediction or training data is written here.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import api_football as api
import api_match_workspace as workspace
import fixture_import_policy as policy

VERSION = 'season-intake-v1'
REASONS = {'identity_changed', 'incomplete_final', 'final_after_administrative',
           'state_after_administrative', 'season_changed', 'invalid_batch'}


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _record(path, code, season, raw, conflicts):
    """Append private review evidence, never substitute it for a season import."""
    stamp = datetime.now(timezone.utc).isoformat(timespec='microseconds')
    evidence = dict(version=VERSION, observed_at=stamp, league=code, season=season,
        response_sha256=sha256(_encoded(raw).encode()).hexdigest(),
        received_count=len(raw), conflicts=conflicts)
    encoded = _encoded(evidence)
    identity = sha256(encoded.encode()).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=15) as connection:
        connection.execute('''CREATE TABLE IF NOT EXISTS rejected_seasons (
            id TEXT PRIMARY KEY, observed_at TEXT NOT NULL, league TEXT NOT NULL,
            season INTEGER NOT NULL, evidence TEXT NOT NULL)''')
        connection.execute('INSERT INTO rejected_seasons VALUES(?,?,?,?,?)',
                           (identity, stamp, code, season, encoded))


def validate_reply(raw, code, season, path):
    """Use the same decisions and manifests as the importer, without a write."""
    incoming = {}
    try:
        for item in raw:
            mid = api._numeric_id(item['fixture']['id'], 'du match')
            if mid in incoming or api._season_identity(item) != (code, season):
                raise ValueError()
            incoming[mid] = item
        marker = api._validate_season_import(dict(competition_code=code, season=season,
            match_ids=list(incoming)), incoming,
            imported_at=datetime.now(timezone.utc).isoformat())
    except (ValueError, KeyError, TypeError, AttributeError):
        return [dict(reason='invalid_batch')]
    if not incoming:
        return []
    previous = {api._numeric_id(item['fixture']['id'], 'du match'): item
                for item in api.load_fixtures(path, ids=list(incoming))}
    latest = {record['match_id']: record
              for record in api.load_import_observations(path, ids=list(incoming))}
    merged, conflicts = {}, []
    for mid, item in incoming.items():
        selected, decision, reason = policy.decide(previous.get(mid), item,
                                                  api.normalize_fixture, latest=latest.get(mid))
        merged[mid] = selected
        quality = policy.quarantine(selected)
        if quality:
            reason = quality.get('reason')
        elif api._season_identity(selected) != (code, season):
            reason = 'season_changed'
        else:
            continue
        conflicts.append(dict(match_id=mid, reason=reason if reason in REASONS else 'invalid_batch',
            decision=decision, prior=previous.get(mid), received=item))
    # Keep this final check even if the policy later adds another rejection.
    try:
        api._validate_season_import(marker, merged)
    except (ValueError, KeyError, TypeError, AttributeError):
        if not conflicts:
            conflicts.append(dict(reason='invalid_batch'))
    return conflicts


class SeasonIntakeClient:
    """Delegate every call; inspect only unfiltered league/season fixtures."""
    def __init__(self, client, path):
        self.client, self.path = client, Path(path)
        self.deferred = []

    @property
    def last_quota(self):
        return self.client.last_quota

    def fixtures(self, **params):
        response = self.client.fixtures(**params)
        if (not {'league', 'season'} <= params.keys()
                or params.keys() - {'league', 'season', 'timezone'}):
            return response
        raw = workspace._response_fixtures(response)
        try:
            code = api._numeric_id(params['league'], 'de compétition')
            season = params['season']
            if type(season) is not int or not 1900 <= season <= 2100:
                raise ValueError()
            conflicts = validate_reply(raw, code, season, self.path)
            if not conflicts:
                return response
            _record(self.path.parent/'.season_intake.sqlite3', code, season, raw, conflicts)
        except (ValueError, KeyError, TypeError, AttributeError, OSError, sqlite3.Error):
            raise api.ApiFootballError('Vérification du lot historique indisponible ; lot non importé.') from None
        self.deferred.append(dict(league=code, season=season, received=len(raw),
                                  conflicts=len(conflicts), state='deferred_for_review'))
        raise api.ApiFootballError('Lot historique incohérent conservé pour examen ; saison non confirmée.')
