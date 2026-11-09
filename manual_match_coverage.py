"""Pure prematch coverage checks for manual, multi-competition selections.

This reads only the supplied history. It neither estimates probabilities nor
loads data, trains a model or saves an analysis. Its thresholds and historical
row validators are the existing Clubs model's own ones.
"""
from collections.abc import Mapping
from datetime import datetime, timezone

import catboost_scenarios as ml
from club_identity import ClubProfiles, different_clubs, identifier
import match_predictor as predictor
from match_predictor import MIN_DAYS, MIN_LEAGUE, MIN_TEAM


PROVIDER = 'api-football'
SOURCE = 'API-Football'


def _clock(value):
    if (not isinstance(value, datetime) or value.tzinfo is None
            or value.utcoffset() is None):
        raise ValueError('La vérification de couverture nécessite une heure avec fuseau horaire.')
    return value.astimezone(timezone.utc)


def _fixture(value):
    if not isinstance(value, Mapping):
        raise ValueError('Affiche invalide.')
    result = {name: identifier(value.get(name)) for name in
              ('match_id', 'league', 'home_id', 'away_id')}
    if any(item is None for item in result.values()):
        raise ValueError('Identifiants API absents.')
    if value.get('provider', PROVIDER) != PROVIDER:
        raise ValueError('Source de l’affiche invalide.')
    home, away = value.get('home'), value.get('away')
    if (not all(isinstance(name, str) and name.strip() for name in (home, away))
            or not different_clubs(home.strip(), away.strip(), result['home_id'], result['away_id'])):
        raise ValueError('Identités des clubs invalides.')
    season = value.get('season')
    if season is not None and (type(season) is not int or not 1900 <= season <= 2100):
        raise ValueError('Saison de l’affiche invalide.')
    stamp = ml._utc_stamp(value.get('timestamp'))
    result.update(home=home.strip(), away=away.strip(), provider=PROVIDER,
                  timestamp=stamp, kickoff=datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc))
    return result


def _failure(before, reason):
    return dict(eligible=False, training_matches=None, training_days=None,
                before=before, reason=reason, invalid=True,
                personalized=False, home_matches=None, away_matches=None)


def _history(rows, league, before):
    # Use both existing stages, in the same order as manual_mixed_analysis:
    # source/UTC/score/dedup first, then the Clubs identity/source validator.
    selected = ml._rows(rows, league, SOURCE, before)
    train = predictor.training_rows(selected, league, before, PROVIDER)
    profiles = ClubProfiles(provider=PROVIDER)
    for row in train:
        profiles.add(row, predictor.goals(row))
    return train, profiles


def _unassigned_history(rows, before):
    """Fail closed for unassignable past API rows, never inspect future scores."""
    for row in rows:
        if not isinstance(row, Mapping):
            return True
        if row.get('provider') != PROVIDER or _identifier_safe(row.get('league')) is not None:
            continue
        try:
            if ml._utc_stamp(row.get('timestamp'))[:10] >= before:
                continue
        except (ValueError, TypeError, AttributeError, OverflowError):
            pass
        return True
    return False


def coverage(rows, fixtures, *, decision_at, group_fixtures=()):
    """Return each candidate's exact local Clubs coverage at the group cutoff.

    ``before`` is the earliest UTC date of the real decision, candidate and
    already selected fixtures, exclusive. All candidates sharing (competition,
    before) share one validated in-memory history/profile pass. Invalid history
    produces an explicit noneligible diagnostic, never a usable partial count.
    Candidate IDs must be present and unique so no result can be overwritten.
    """
    current = _clock(decision_at)
    fixtures, group_fixtures, rows = list(fixtures), list(group_fixtures), list(rows)
    identities, seen = [], set()
    for fixture in fixtures:
        mid = identifier(fixture.get('match_id')) if isinstance(fixture, Mapping) else None
        if mid is None or mid in seen:
            raise ValueError('Chaque candidat doit conserver un identifiant API présent et distinct.')
        identities.append(mid)
        seen.add(mid)
    try:
        group = [_fixture(fixture) for fixture in group_fixtures]
        group_invalid = False
        group_before = min([current.date().isoformat()] + [item['timestamp'][:10] for item in group])
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        group, group_invalid, group_before = [], True, current.date().isoformat()
    # Preserve _rows' exact string-based competition matching while scanning
    # the supplied history only once, rather than once per displayed league.
    by_league, unscoped = {}, []
    for row in rows:
        if not isinstance(row, Mapping):
            unscoped.append(row)
            continue
        code = row.get('league')
        by_league.setdefault(str(code), []).append(row)
        if row.get('provider') == PROVIDER and _identifier_safe(code) is None:
            unscoped.append(row)
    cache, unassigned, result = {}, {}, {}
    for mid, raw in zip(identities, fixtures):
        try:
            fixture = _fixture(raw)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            result[mid] = _failure(group_before, 'Identité, horaire ou source du match invalide ; vérification nécessaire.')
            continue
        before = min(group_before, fixture['timestamp'][:10])
        if group_invalid:
            result[mid] = _failure(before, 'Le groupe contient une affiche invalide ; vérification nécessaire.')
            continue
        if fixture['kickoff'] <= current or any(item['kickoff'] <= current for item in group):
            result[mid] = _failure(before, 'Le coup d’envoi d’un match du groupe est déjà atteint ; nouvelle analyse indisponible.')
            continue
        league = fixture['league']
        key = league, before
        if key not in cache:
            try:
                # A source row without an assignable competition cannot be
                # attributed safely. Future/target-day rows remain excluded.
                if before not in unassigned:
                    unassigned[before] = _unassigned_history(unscoped, before)
                if unassigned[before]:
                    raise ValueError('Historique non attribuable.')
                train, profiles = _history(by_league.get(league, []), league, before)
                cache[key] = (train, profiles)
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                cache[key] = None
        checked = cache[key]
        if checked is None:
            result[mid] = _failure(before,
                f'Historique local invalide pour la compétition {league} ; contrôle nécessaire avant analyse.')
            continue
        train, profiles = checked
        n, days = len(train), len({row['timestamp'][:10] for row in train})
        eligible = n >= MIN_LEAGUE and days >= MIN_DAYS
        home = profiles.profile(fixture['home'], 'home', team_id=fixture['home_id'], league=league, provider=PROVIDER)
        away = profiles.profile(fixture['away'], 'away', team_id=fixture['away_id'], league=league, provider=PROVIDER)
        personalized = eligible and min(home['matches'], away['matches']) >= MIN_TEAM
        reason = (f'Historique Clubs exploitable : {n} résultats sur {days} jours UTC avant le {before}.' if eligible else
                  f'Historique Clubs insuffisant : {n}/{MIN_LEAGUE} résultats et {days}/{MIN_DAYS} jours UTC avant le {before}.')
        result[mid] = dict(eligible=eligible, training_matches=n, training_days=days,
            before=before, reason=reason, invalid=False, personalized=personalized,
            home_matches=home['matches'], away_matches=away['matches'])
    return result


def _identifier_safe(value):
    """Classify unassignable history without leaking an exception's contents."""
    try:
        return identifier(value)
    except ValueError:
        return None
