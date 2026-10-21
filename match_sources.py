"""API-Football match loading and calendar access; no legacy-source fallback."""
from datetime import datetime, timezone

import api_football

SOURCES = ('API-Football',)
SOURCE_ERRORS = (api_football.ApiFootballError,)


def _validate(source):
    if source != 'API-Football':
        raise ValueError('Seule la source API-Football est autorisée.')


def data_signature(source):
    _validate(source)
    return api_football.data_signature(api_football.STORE_PATH)


def load_matches(coupons=None, source='API-Football'):
    _validate(source)
    return api_football.load_matches(api_football.STORE_PATH)


def scheduled_fixtures(source, league):
    """Read locally scheduled fixtures only; callers filter elapsed kickoff times."""
    _validate(source)
    rows = []
    for raw in api_football.load_fixtures(api_football.STORE_PATH):
        try:
            event = raw['fixture']
            if event['status'].get('short') != 'NS':
                continue
            competition = raw['league']
            home, away = raw['teams']['home'], raw['teams']['away']
            match_id, stamp = event['id'], event['date']
            if str(competition['id']) != str(league):
                continue
            if not home.get('id') or not away.get('id') or home['id'] == away['id']:
                continue
            if not home.get('name') or not away.get('name') or home['name'] == away['name']:
                continue
            stamp = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                continue
            rows.append(dict(match_id=str(int(match_id)), league=str(league),
                             home=home['name'], away=away['name'],
                             home_id=str(home['id']), away_id=str(away['id']),
                             timestamp=stamp.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds'),
                             provider='api-football'))
        except (ValueError, KeyError, TypeError, AttributeError):
            # Unconfirmed knockout participants and unknown dates cannot define a group.
            continue
    return sorted(rows, key=lambda row: (row['timestamp'], int(row['match_id'])))
