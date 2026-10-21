"""Causal pre-match features, independent of any machine-learning library.

Normalized naive timestamps are UTC. A whole UTC day is emitted before any
result from that day updates the history. Empty-history means and rates use 0
with explicit support counts; unknown rest uses -1. No target score, outcome,
or match identifier is a feature.
"""
from collections import Counter, defaultdict, deque
from datetime import date, datetime, timezone
from itertools import groupby
from math import isfinite
from numbers import Real

from league_model import VALUES, outcome, score


LABELS = VALUES
FEATURE_VERSION = 'scenario-features-v1'
CATEGORICAL_COLUMNS = ('provider', 'league', 'home_id', 'away_id', 'home_name', 'away_name')
MIN_TRAIN_EXAMPLES = 100
MIN_VALIDATION_EXAMPLES = 30
MIN_TEST_EXAMPLES = 30
UNKNOWN_ID = '__unknown__'
_LABEL_TAGS = dict(zip(LABELS, ('1st', 'equal', '2nd')))
_GOAL_FIELDS = ('ht_for', 'ht_against', 'sh_for', 'sh_against')
_PROFILES = ('home_overall', 'home_role', 'away_overall', 'away_role')
FEATURE_COLUMNS = (
    CATEGORICAL_COLUMNS
    + ('season', 'season_known', 'month', 'weekday', 'hour_utc')
    + ('league_count', 'league_last10_count')
    + tuple(f'league_rate_{tag}' for tag in _LABEL_TAGS.values())
    + tuple(f'league_last10_rate_{tag}' for tag in _LABEL_TAGS.values())
    + ('league_last10_ht_total', 'league_last10_sh_total')
    + tuple(column for prefix in _PROFILES for column in (
        f'{prefix}_count', f'{prefix}_days_rest',
        *(name for window in (5, 10) for name in (
            f'{prefix}_last{window}_count',
            *(f'{prefix}_last{window}_{field}' for field in _GOAL_FIELDS),
            *(f'{prefix}_last{window}_rate_{tag}' for tag in _LABEL_TAGS.values()),
        )),
    ))
)


class FeatureError(ValueError):
    """A source, timestamp, result, or chronological partition is invalid."""


def _league(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise FeatureError('Compétition absente ou invalide.')
    return str(value).strip()


def _provider(value):
    if not isinstance(value, str) or not value.strip():
        raise FeatureError('La source des matchs doit être précisée.')
    return value.strip().casefold()


def _identifier(value, *, optional=False):
    if optional and value in (None, ''):
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).isdigit() or int(value) <= 0:
        raise FeatureError('Identifiant de match ou de club absent ou invalide.')
    return str(int(value))


def _stamp(value):
    if not isinstance(value, str):
        raise FeatureError('Date du match absente ou invalide.')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise FeatureError('Date du match absente ou invalide.') from None


def _metadata(row, league, provider, *, require_id=False):
    try:
        if _league(row['league']) != league or _provider(row['provider']) != provider:
            raise FeatureError('La compétition et la source doivent rester identiques.')
        home, away = row['home'], row['away']
        if not all(isinstance(name, str) and name.strip() for name in (home, away)):
            raise FeatureError('Les noms des deux clubs sont nécessaires.')
        home, away = home.strip(), away.strip()
        home_id = _identifier(row.get('home_id'), optional=True)
        away_id = _identifier(row.get('away_id'), optional=True)
        if home == away or home_id is not None and home_id == away_id:
            raise FeatureError('Les deux clubs doivent être différents.')
        season = row.get('season')
        if season is not None and (type(season) is not int or not 1900 <= season <= 2100):
            raise FeatureError('Saison absente ou invalide.')
        return dict(original=row, match_id=_identifier(row.get('match_id'), optional=not require_id),
                    league=league, provider=provider, home=home, away=away,
                    home_id=home_id, away_id=away_id, stamp=_stamp(row['timestamp']), season=season)
    except (KeyError, TypeError, AttributeError):
        raise FeatureError('Métadonnées du match absentes ou invalides.') from None


def _scoped_rows(rows, league, provider=None, cutoff=None):
    """Select the requested source before computing any historical statistic."""
    league = _league(league)
    provider = _provider(provider) if provider is not None else None
    selected = []
    for row in rows:
        if not isinstance(row, dict):
            raise FeatureError('Chaque match doit être un objet normalisé.')
        if str(row.get('league', '')).strip() != league:
            continue
        if provider is not None and (not isinstance(row.get('provider'), str)
                                     or _provider(row['provider']) != provider):
            continue
        # Future result fields are never inspected during an earlier forecast.
        if cutoff is not None and _stamp(row.get('timestamp')).date() >= cutoff:
            continue
        selected.append(row)
    if provider is None:
        providers = {_provider(row.get('provider')) for row in selected}
        if len(providers) > 1:
            raise FeatureError('Choisissez une seule source pour cette compétition.')
        provider = next(iter(providers), None)
    prepared, seen = [], {}
    for row in selected:
        item = _metadata(row, league, provider, require_id=True)
        try:
            ht, ft = score(row.get('ht')), score(row.get('ft'))
            actual = outcome(ht, ft)
        except ValueError:
            raise FeatureError('Scores historiques HT/FT absents ou invalides.') from None
        if row.get('value') not in LABELS or row['value'] != actual:
            raise FeatureError('Le résultat historique contredit les scores HT/FT.')
        item.update(ht=ht, ft=ft, label=actual)
        signature = (item['stamp'], item['home'], item['away'], item['home_id'], item['away_id'],
                     item['season'], ht, ft, actual)
        mid = item['match_id']
        if mid in seen:
            if seen[mid] != signature:
                raise FeatureError(f'Identifiant de match contradictoire : {mid}.')
            continue
        seen[mid] = signature
        prepared.append(item)
    prepared.sort(key=lambda item: (item['stamp'], int(item['match_id'])))
    return provider, prepared


def _team_key(team_id, name):
    return ('id', team_id) if team_id is not None else ('name', name)


class _TeamHistory:
    def __init__(self):
        self.count = 0
        self.recent = deque(maxlen=10)

    def update(self, record):
        self.count += 1
        self.recent.append(record)


class _History:
    def __init__(self):
        self.overall = defaultdict(_TeamHistory)
        self.roles = defaultdict(_TeamHistory)
        self.name_ids = defaultdict(set)
        self.count = 0
        self.labels = Counter()
        self.recent = deque(maxlen=10)
        self.latest_season = None

    def update(self, item):
        ht, ft = item['ht'], item['ft']
        self.count += 1
        self.labels[item['label']] += 1
        self.recent.append(dict(label=item['label'], ht_total=sum(ht), sh_total=sum(ft) - sum(ht)))
        if item['season'] is not None:
            self.latest_season = item['season']
        for index, venue in enumerate(('home', 'away')):
            name, team_id = item[venue], item[f'{venue}_id']
            if team_id is not None:
                self.name_ids[name].add(team_id)
            key = _team_key(team_id, name)
            record = dict(day=item['stamp'].date(), label=item['label'],
                          ht_for=ht[index], ht_against=ht[1-index],
                          sh_for=ft[index]-ht[index], sh_against=ft[1-index]-ht[1-index])
            self.overall[key].update(record)
            self.roles[(key, venue)].update(record)

    def resolve(self, name, team_id):
        if team_id is None and len(self.name_ids.get(name, ())) == 1:
            team_id = next(iter(self.name_ids[name]))
        key = _team_key(team_id, name)
        # A source may have omitted IDs on all older rows. Use an unambiguous
        # name-only history, without joining a different known team identifier.
        if key not in self.overall and not self.name_ids.get(name) and ('name', name) in self.overall:
            key = ('name', name)
        return team_id or UNKNOWN_ID, key


def _mean(records, key):
    return sum(row[key] for row in records) / len(records) if records else 0.0


def _profile_features(features, prefix, history, target_day):
    recent = list(history.recent)
    features[f'{prefix}_count'] = history.count
    features[f'{prefix}_days_rest'] = (target_day - recent[-1]['day']).days if recent else -1
    for window in (5, 10):
        records = recent[-window:]
        features[f'{prefix}_last{window}_count'] = len(records)
        for field in _GOAL_FIELDS:
            features[f'{prefix}_last{window}_{field}'] = _mean(records, field)
        for label, tag in _LABEL_TAGS.items():
            features[f'{prefix}_last{window}_rate_{tag}'] = (
                sum(row['label'] == label for row in records) / len(records) if records else 0.0)


def _features(history, item):
    home_id, home_key = history.resolve(item['home'], item['home_id'])
    away_id, away_key = history.resolve(item['away'], item['away_id'])
    stamp = item['stamp']
    features = dict(provider=item['provider'], league=item['league'], home_id=home_id, away_id=away_id,
                    home_name=item['home'], away_name=item['away'],
                    season=item['season'] if item['season'] is not None else history.latest_season or stamp.year,
                    season_known=int(item['season'] is not None), month=stamp.month, weekday=stamp.weekday(),
                    hour_utc=stamp.hour, league_count=history.count, league_last10_count=len(history.recent))
    recent = list(history.recent)
    for label, tag in _LABEL_TAGS.items():
        features[f'league_rate_{tag}'] = history.labels[label] / history.count if history.count else 0.0
    for label, tag in _LABEL_TAGS.items():
        features[f'league_last10_rate_{tag}'] = (
            sum(row['label'] == label for row in recent) / len(recent) if recent else 0.0)
    features['league_last10_ht_total'] = _mean(recent, 'ht_total')
    features['league_last10_sh_total'] = _mean(recent, 'sh_total')
    for venue, key in (('home', home_key), ('away', away_key)):
        _profile_features(features, f'{venue}_overall', history.overall.get(key, _TeamHistory()), stamp.date())
        _profile_features(features, f'{venue}_role', history.roles.get((key, venue), _TeamHistory()), stamp.date())
    return {column: features[column] for column in FEATURE_COLUMNS}


def build_examples(rows, league, provider=None):
    """Return ordered labelled matches with features frozen before each UTC day.

    Other leagues are ignored. An explicit provider also filters other sources;
    without it, mixed providers within the selected league are rejected. Equal
    duplicate records count once; contradictory duplicate identifiers fail.
    ``match`` retains the original normalized dictionary without mutating it.
    """
    _, prepared = _scoped_rows(rows, league, provider)
    history, examples = _History(), []
    for _, day_matches in groupby(prepared, key=lambda item: item['stamp'].date()):
        day_matches = list(day_matches)
        for item in day_matches:
            examples.append(dict(match=item['original'], features=_features(history, item), label=item['label']))
        for item in day_matches:
            history.update(item)
    return examples


def feature_for_fixture(rows, fixture, before=None):
    """Build the same schema without reading any target outcome or target score.

    The history cutoff is the earlier of ``before`` and the fixture's UTC day.
    Team IDs may be resolved from a unique name in that past history. A missing
    season uses the most recent past season, or the fixture year if none exists.
    """
    if not isinstance(fixture, dict):
        raise FeatureError('Affiche absente ou invalide.')
    try:
        target_day = _stamp(fixture['timestamp']).date()
        cutoff = min(date.fromisoformat(before), target_day) if before is not None else target_day
        league = _league(fixture['league'])
    except (KeyError, TypeError, ValueError) as error:
        raise FeatureError('Date limite ou affiche absente ou invalide.') from error
    provider, prepared = _scoped_rows(rows, league, fixture.get('provider'), cutoff)
    if provider is None:
        raise FeatureError('La source de l’affiche doit être précisée.')
    item = _metadata(dict(fixture, provider=provider), league, provider)
    history = _History()
    for previous in prepared:
        history.update(previous)
    return _features(history, item)


def chronological_split(examples, train_fraction=.6, validation_fraction=.2):
    """Split on whole UTC days, using only dates and counts to choose boundaries.

    At least 100/30/30 examples are required in train/validation/test. The pair
    of valid day boundaries closest to the requested fractions is selected;
    labels and feature values do not influence this deterministic partition.
    """
    fractions = (train_fraction, validation_fraction)
    if any(isinstance(value, bool) or not isinstance(value, Real) or not isfinite(value) or value <= 0
           for value in fractions) or train_fraction + validation_fraction >= 1:
        raise FeatureError('Les proportions apprentissage/validation doivent être positives et laisser un test.')
    examples = list(examples)
    if len(examples) < MIN_TRAIN_EXAMPLES + MIN_VALIDATION_EXAMPLES + MIN_TEST_EXAMPLES:
        raise FeatureError('Il faut au moins 100 matchs d’apprentissage, 30 de validation et 30 de test.')
    ordered, identities, leagues, providers = [], set(), set(), set()
    for example in examples:
        try:
            row = example['match']
            mid = _identifier(row['match_id'])
            league, provider = _league(row['league']), _provider(row['provider'])
            stamp = _stamp(row['timestamp'])
        except (KeyError, TypeError):
            raise FeatureError('Exemple sans métadonnées chronologiques valides.') from None
        identity = (provider, league, mid)
        if identity in identities:
            raise FeatureError('Un match ne peut apparaître deux fois dans le découpage.')
        identities.add(identity)
        leagues.add(league)
        providers.add(provider)
        ordered.append((stamp, int(mid), example))
    if len(leagues) != 1 or len(providers) != 1:
        raise FeatureError('Le découpage doit conserver une seule compétition et une seule source.')
    ordered.sort(key=lambda item: (item[0], item[1]))
    boundaries = [i for i in range(1, len(ordered)) if ordered[i-1][0].date() != ordered[i][0].date()]
    n, best = len(ordered), None
    for first in boundaries:
        if first < MIN_TRAIN_EXAMPLES:
            continue
        for second in boundaries:
            if second - first < MIN_VALIDATION_EXAMPLES or n - second < MIN_TEST_EXAMPLES:
                continue
            sizes = (first, second-first, n-second)
            target = (train_fraction, validation_fraction, 1-train_fraction-validation_fraction)
            distance = sum(abs(count / n - fraction) for count, fraction in zip(sizes, target))
            candidate = (distance, abs(first / n-train_fraction), first, second)
            if best is None or candidate < best:
                best = candidate
    if best is None:
        raise FeatureError('Les jours entiers ne permettent pas les minimums 100/30/30 sans chevauchement.')
    _, _, first, second = best
    ordered = [example for _, _, example in ordered]
    return dict(train=ordered[:first], validation=ordered[first:second], test=ordered[second:])
