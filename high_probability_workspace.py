"""Local probability search and explicit, immutable pre-match registration.

The list always uses the same Clubs model. Group snapshots reuse exactly the
displayed probabilities; no model selection, training or history import occurs.
"""
from copy import deepcopy
from datetime import datetime, timezone
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import api_football as api
import api_match_workspace as calendar
import high_probability_engine as engine
import prediction_journal as journal
import prospective_models as models
from match_predictor import VERSION as CLUB_VERSION


VERSION = 'high-probability-v3'
ZONE = ZoneInfo('Africa/Kinshasa')


def _now(value=None):
    value = datetime.now(timezone.utc) if value is None else value
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('Une horloge avec fuseau horaire est nécessaire.')
    return value.astimezone(timezone.utc)


def _root(root):
    return Path(api.DATA_DIR if root is None else root)


@lru_cache(maxsize=4)
def _cached_scan(path, modified, size, minute):
    return engine.scan(api.load_fixtures(Path(path)), now=datetime.fromisoformat(minute))


def load_scan(root=None, now=None):
    """Read local data only, caching by file revision and UTC minute."""
    current = _now(now)
    path = _root(root) / 'fixtures.json'
    stat = path.stat() if path.exists() else None
    minute = current.replace(second=0, microsecond=0).isoformat()
    report = deepcopy(_cached_scan(str(path), stat.st_mtime_ns if stat else 0,
                                  stat.st_size if stat else 0, minute))
    # A cached minute must never keep a match whose kickoff has just passed.
    kept, expired = [], []
    for row in report['rows']:
        (kept if journal._stamp(row['fixture']['timestamp']) > current else expired).append(row)
    report['rows'] = kept
    report['excluded'].extend(dict(match_id=row['fixture']['match_id'], reason='kickoff_passed',
                                  detail='Le coup d’envoi est passé.', fixture=row['fixture']) for row in expired)
    report['diagnostics']['eligible_count'] = len(kept)
    report['checked_at'] = current.isoformat()
    report['calendar_updated_at'] = calendar._calendar_updates(path).get((report['day'], ZONE.key))
    return report


def refresh_scan(root=None, now=None):
    """One explicit calendar request, no training or multi-season imports."""
    current = _now(now)
    day = current.astimezone(ZONE).date()
    calendar.refresh_day(day, timezone_name=ZONE.key, path=_root(root)/'fixtures.json')
    after = _now(now)
    if after.astimezone(ZONE).date() != day:
        raise ValueError('La journée a changé pendant l’actualisation ; relancez la recherche.')
    _cached_scan.cache_clear()
    return load_scan(root=root, now=after)


def _digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def prepare_group(report, match_ids, now=None, *, outcome=None, minimum=0, choices=None):
    """Freeze the selected, displayed Clubs distributions without recalculating."""
    current = _now(now)
    day = current.astimezone(ZONE).date().isoformat()
    if report.get('source') != 'API-Football' or report.get('day') != day:
        raise ValueError('Relancez la recherche sur les matchs du jour à Kinshasa.')
    ids = [str(value) for value in match_ids]
    if len(ids) != len(set(ids)) or not 2 <= len(ids) <= 20:
        raise ValueError('Choisissez entre 2 et 20 matchs distincts.')
    if choices is not None:
        if (outcome is not None or not isinstance(choices, dict)
                or any(not isinstance(key, str) for key in choices)
                or set(choices) != set(ids) or any(value not in ('1st', 'Égal', '2nd') for value in choices.values())):
            raise ValueError('Choisissez exactement un résultat 1st, Égal ou 2nd pour chaque match du groupe.')
    available = {row['fixture']['match_id']: row for row in engine.filter_rows(
        report, minimum=0 if choices is not None else minimum, outcome=outcome)}
    # Validate a custom threshold even when each selected fixture has its own outcome.
    if choices is not None:
        engine.filter_rows(report, minimum=minimum)
    if any(identity not in available for identity in ids):
        raise ValueError('Une affiche ne figure plus dans les résultats de la recherche.')
    chosen = sorted((deepcopy(available[identity]) for identity in ids),
                    key=lambda row: (row['fixture']['timestamp'], int(row['fixture']['match_id'])))
    if choices is not None:
        for row in chosen:
            row['search_outcome'] = choices[row['fixture']['match_id']]
            row['search_probability'] = row['probabilities'][row['search_outcome']]
            if row['search_probability'] < minimum:
                raise ValueError('Un résultat choisi est inférieur au seuil demandé.')
    fixtures = journal._fixtures([row['fixture'] for row in chosen], allow_mixed=True)
    journal._check_time(fixtures, current, True)
    before = fixtures[0]['timestamp'][:10]
    history_before = report['before']
    datetime.strptime(history_before, '%Y-%m-%d')
    if history_before > before or history_before > current.date().isoformat():
        raise ValueError('La coupure historique doit précéder les rencontres à prévoir.')
    grouped = {}
    for index, row in enumerate(chosen):
        if row.get('personalized') is not True:
            raise ValueError('Les profils des deux clubs doivent être exploitables.')
        grouped.setdefault(row['fixture']['league'], []).append((index, row))
    leagues = sorted(grouped, key=int)
    components, competitions = [], {}
    for league in leagues:
        members = grouped[league]
        details = [{key: row[key] for key in ('probabilities', 'baseline', 'home_profile', 'away_profile',
                   'personalized', 'training', 'before') if key in row} for _, row in members]
        probabilities = [row['probabilities'] for _, row in members]
        fingerprints = {row['history_fingerprint'] for _, row in members if row.get('history_fingerprint')}
        if len(fingerprints) > 1:
            raise ValueError('Les profils de cette compétition utilisent des historiques différents.')
        provenance = dict(source='API-Football', league=league, training_before=before,
                          history_cutoff_exclusive=history_before,
                          history_fingerprint=next(iter(fingerprints), _digest(details)),
                          training_matches=members[0][1]['training'], training_groups=0)
        distribution = models._distribution('independent', len(members), distributions=probabilities)
        method = models._method(CLUB_VERSION, distribution, provenance,
                                _component=len(members) == 1, club_estimates=details)
        components.append(dict(league=league, indices=[index for index, _ in members], method=method))
        fixture = members[0][1]['fixture']
        competitions[league] = dict(competition=fixture.get('competition') or league,
                                    country=fixture.get('country') or 'International')
    provenance = dict(source='API-Football', provider='api-football', league='mixed', leagues=leagues,
                      group_kind='mixed', training_before=before, history_cutoff_exclusive=history_before,
                      fixtures=fixtures, assumes_block_independence=True,
                      correction_scope='within_each_league_only', selection_policy=VERSION)
    distribution = dict(schema=models.DISTRIBUTION_VERSION, type='block_product', size=len(fixtures),
                        components=components)
    method = models._method(models.MIXED_VERSION, distribution, provenance,
                            generation=_digest([component['method'] for component in components]))
    chances = [row['search_probability'] for row in chosen]
    sequence = [row['search_outcome'] for row in chosen]
    requested = (dict(sequence=sequence, probability=models.probability_for_sequence(method, sequence))
                 if choices is not None or outcome is not None else None)
    search_kind = 'mixed' if choices is not None else outcome
    selection = dict(policy=VERSION,
                     ranking_metric=('custom_outcome_clubs_probability' if choices is not None else
                                     'outcome_clubs_probability' if outcome is not None else 'maximum_clubs_probability'),
                     searched_outcome=outcome, requested_scenario=requested,
                     minimum_match_probability=min(chances), maximum_match_probability=max(chances),
                     search=dict(outcome=search_kind, minimum=minimum),
                     before=history_before, match_probabilities=[dict(match_id=row['fixture']['match_id'],
                         probabilities=row['probabilities'], choice=row['choice'],
                         max_probability=row['max_probability'], search_outcome=row['search_outcome'],
                         search_probability=row['search_probability'], baseline=row.get('baseline'),
                         home_profile=row.get('home_profile'), away_profile=row.get('away_profile'),
                         delta_points=(100*(row['search_probability']-row['baseline'][row['search_outcome']])
                                       if row['search_outcome'] and row.get('baseline') else None)) for row in chosen],
                     reason=('Choix personnalisés pour chaque rencontre, à partir des probabilités Clubs, '
                             if choices is not None else
                             'Matchs choisis selon la probabilité Clubs de « ' + outcome + ' », '
                             if outcome is not None else
                             'Matchs choisis dans le classement des probabilités Clubs décroissantes, ') +
                            'toutes compétitions confondues. Les probabilités du groupe sont calculées '
                            'sous une hypothèse d’indépendance des rencontres.')
    return dict(source='API-Football', day=day, fixtures=fixtures, methods={'clubs': method},
                metadata=dict(selected_engine='clubs', daily_policy=VERSION, selection=selection,
                              competitions=competitions), requested_scenario=requested,
                created_at=current.isoformat())


def record_group(prepared, root=None, clock=None, fetch=None):
    """Fresh provider preflight followed by the existing write-once journal."""
    root = _root(root)
    clock = (lambda: datetime.now(timezone.utc)) if clock is None else clock
    fixtures = journal._fixtures(prepared['fixtures'], allow_mixed=True)
    existing = journal.find_mixed(fixtures, path=root/'predictions.sqlite3')
    requested = prepared.get('metadata', {}).get('selection', {}).get('requested_scenario')
    if existing and requested is None:
        return dict(existing, created=False)
    if requested is not None:
        models._sequence(requested.get('sequence'), len(fixtures))
    if existing:
        old = {row['match_id']: row for row in existing['fixtures']}
        if any(any(str(row[field]) != str(old[row['match_id']][field])
                   for field in ('league', 'home_id', 'away_id', 'home', 'away', 'timestamp')) for row in fixtures):
            raise ValueError('Les affiches ont changé depuis la première prévision figée de ce groupe.')
    current = _now(clock())
    if prepared.get('source') != 'API-Football' or prepared.get('day') != current.astimezone(ZONE).date().isoformat():
        raise ValueError('Cette analyse ne concerne plus la journée actuelle.')
    journal._check_time(fixtures, current, True)
    for payload in prepared['methods'].values():
        models.validate_method(payload, len(fixtures))
    key = ''
    try:
        if fetch is None:
            client, key = calendar._client()
            fresh = calendar._response_fixtures(client.fixtures(
                ids='-'.join(row['match_id'] for row in fixtures), timezone='UTC'))
        else:
            fresh = fetch(ids='-'.join(row['match_id'] for row in fixtures), timezone='UTC')
        if (not isinstance(fresh, list) or len(fresh) != len(fixtures)
                or any(not isinstance(row, dict) or not isinstance(row.get('fixture'), dict) for row in fresh)
                or {str(row['fixture'].get('id')) for row in fresh} != {row['match_id'] for row in fixtures}):
            raise ValueError('API-Football ne confirme pas exactement les matchs de cette analyse.')
        # Validate identities/status before persisting the provider response.
        by_id = {str(row['fixture']['id']): row for row in fresh}
        for fixture in fixtures:
            raw = by_id[fixture['match_id']]
            confirmed = calendar.fixture_from_raw(raw)
            if api._status_short(raw) != 'NS' or any(str(confirmed[field]) != str(fixture[field])
                    for field in ('league', 'home_id', 'away_id', 'home', 'away', 'timestamp')):
                raise ValueError('Une affiche, son statut ou son horaire a changé : actualisez la recherche.')
        journal._check_time(fixtures, _now(clock()), True)
        api.save_fixtures(fresh, root/'fixtures.json')
        metadata = deepcopy(prepared['metadata'])
        metadata['calendar_checked_at'] = _now(clock()).isoformat()
        entry = (dict(existing, created=False) if existing else
                 journal.record_mixed(fixtures, prepared['methods'], path=root/'predictions.sqlite3',
                                      metadata=metadata, raw_fixtures=fresh, clock=clock))
        if requested is not None:
            import probability_scenario_store as intents
            intent = intents.record(entry, prepared['metadata']['selection'],
                                    path=root/'probability_scenarios.sqlite3', clock=clock)
            return dict(entry, forecast_created=entry['created'], created=intent['created'], scenario_intent=intent)
        return entry
    except api.ApiFootballError as error:
        raise api.ApiFootballError(calendar._safe(error, key)) from None
