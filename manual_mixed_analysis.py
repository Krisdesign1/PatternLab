"""Manual, score-free groups composed from each competition's own history.

The selected Clubs distribution uses the existing per-match model. Other
available methods are frozen by the existing competition-block builder. This
module neither retrieves data nor trains, writes or retrospectively rebuilds a
forecast. The caller owns the before-kickoff archive and provider preflight.
"""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from zoneinfo import ZoneInfo

import catboost_scenarios as ml
from club_identity import fixture_identity
from match_predictor import estimate as estimate_clubs
import prospective_models as models


VERSION = 'manual-mixed-clubs-v1'
ENGINE = 'mixed_clubs'
SELECTED_METHOD = 'clubs'
ZONE = ZoneInfo('Africa/Kinshasa')


def _clock(value):
    value = datetime.now(timezone.utc) if value is None else value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise models.SnapshotError('La décision doit préciser son fuseau horaire.')
    return value.astimezone(timezone.utc)


def _club_product(histories, fixtures, before, decision_at):
    """Freeze exactly the existing independent Clubs probabilities, by league.

    Clubs needs 30 results on three prior UTC days. It does not need the 20
    historical groups used to fit the separate repetition correction.
    """
    grouped, estimates, support = {}, [], []
    for index, fixture in enumerate(fixtures):
        code = fixture['league']
        estimate = estimate_clubs(histories[code], code, fixture['home'], fixture['away'],
                                  before, **fixture_identity(fixture))
        estimates.append(dict(estimate, **{key: fixture[key] for key in (
            'match_id', 'league', 'home', 'away', 'timestamp', 'provider', 'home_id', 'away_id')
            if key in fixture}))
        grouped.setdefault(code, []).append(index)

    components, versions = [], {}
    for code in sorted(grouped, key=int):
        indices = grouped[code]
        selected = [estimates[index] for index in indices]
        train = histories[code]
        support.append(dict(league=code, selected_matches=len(indices), training_matches=len(train),
            training_days=len({row['timestamp'][:10] for row in train}),
            personalized_count=sum(item['personalized'] for item in selected),
            eligible=all(item['eligible'] for item in selected), before=before,
            history_fingerprint=ml._fingerprint(train)))
        if not support[-1]['eligible']:
            continue
        provenance = dict(source=models.SOURCE, league=code, training_before=before,
            history_fingerprint=support[-1]['history_fingerprint'],
            training_matches=len(train), training_groups=0, club_estimates=selected)
        leaf = models._method(models.CLUB_VERSION,
            models._distribution('independent', len(indices),
                                 distributions=[item['probabilities'] for item in selected]),
            provenance, _component=True)
        component_fixtures = [fixtures[index] for index in indices]
        models._bind_methods(dict(methods={'clubs': leaf}), component_fixtures, before, decision_at)
        components.append(dict(indices=indices, league=code, method=leaf))
        versions[code] = dict(version=leaf['version'], generation=leaf.get('generation'))
    if not all(item['eligible'] for item in support):
        return None, estimates, support
    leagues = sorted(grouped, key=int)
    provenance = dict(source=models.SOURCE, provider=models.PROVIDER, league='mixed',
        leagues=leagues, group_kind='mixed', training_before=before,
        fixtures=fixtures, assumes_block_independence=True,
        correction_scope='within_each_league_only', component_versions=versions)
    distribution = dict(schema=models.DISTRIBUTION_VERSION, type='block_product',
                        size=len(fixtures), components=components)
    method = models._method(models.MIXED_VERSION, distribution, provenance,
        generation=sha256(json.dumps(versions, sort_keys=True).encode()).hexdigest())
    models._bind_methods(dict(methods={'clubs': method}), fixtures, before, decision_at)
    return method, estimates, support


def build(rows, fixtures, source='API-Football', *, decision_at=None):
    """Return an immutable-compatible manual payload for 2..20 chosen matches.

    All fixtures must be chronologically ordered, score-free API identities and
    belong to one Kinshasa date. A one-competition selection remains valid.
    Every competition uses the same exclusive UTC cutoff bounded by the actual
    decision. No target-day result can change any estimate or comparison.

    ``comparison_methods`` contains every complete method available for the
    exact group. The selected method is always Clubs, explicitly named by
    ``engine`` and ``selected_method``; there is no hidden engine substitution.
    """
    current = _clock(decision_at)
    fixtures, _, first_day = models._fixtures(fixtures, source, allow_mixed=True)
    # The ordinary single-competition builder tolerates legacy absent club IDs;
    # a mixed portfolio cannot resolve homonymous clubs without those IDs.
    if any(not all(fixture.get(key) for key in ('home_id', 'away_id')) for fixture in fixtures):
        raise models.SnapshotError('Les identifiants des deux clubs sont requis pour chaque match du groupe.')
    local_days = {datetime.fromisoformat(fixture['timestamp']).replace(tzinfo=timezone.utc)
                  .astimezone(ZONE).date().isoformat() for fixture in fixtures}
    if len(local_days) != 1:
        raise models.SnapshotError('Choisissez uniquement des matchs de la même journée à Kinshasa.')
    context = models._context(fixtures, first_day, current)
    before = context['training_before']
    rows = list(rows)
    leagues = sorted({fixture['league'] for fixture in fixtures}, key=int)
    histories = {code: ml._rows(rows, code, models.SOURCE, before) for code in leagues}
    clubs, estimates, support = _club_product(histories, fixtures, before, current)
    frozen = models.build_mixed_methods(rows, fixtures, source, decision_at=current)
    methods = deepcopy(frozen['methods'])
    unavailable = deepcopy(frozen['unavailable'])
    if clubs is not None:
        methods['clubs'] = clubs
        unavailable.pop('clubs', None)
    else:
        methods.pop('clubs', None)
        missing = [item['league'] for item in support if not item['eligible']]
        unavailable['clubs'] = ('Historique Clubs insuffisant pour les compétitions ' + ', '.join(missing)
            + ' : au moins 30 résultats sur trois jours UTC strictement antérieurs sont nécessaires.')
    diagnostic = unavailable.get('clubs', '')
    latest = max((row['timestamp'] for history in histories.values() for row in history), default=None)
    forecast = dict(version=VERSION, source=models.SOURCE, engine=ENGINE, selected_method=SELECTED_METHOD,
        eligible=clubs is not None, size=len(fixtures), league='mixed', leagues=leagues,
        before=before, training_before=before, latest=latest,
        ranking=deepcopy(clubs['ranking']) if clubs else [],
        club_estimates=estimates, personalized_count=sum(item['personalized'] for item in estimates),
        personalized_matches=sum(item['personalized'] for item in estimates),
        training_matches=sum(len(history) for history in histories.values()),
        competition_support=support, assumes_block_independence=True,
        diagnostic=diagnostic, diagnostics=[diagnostic] if diagnostic else [])
    return deepcopy(dict(algorithm_version=VERSION, source=models.SOURCE, engine=ENGINE,
        selected_method=SELECTED_METHOD, group_kind='mixed', day=next(iter(local_days)),
        fixtures=fixtures, forecast=forecast, comparison_methods=methods,
        unavailable=unavailable, history_cutoff_exclusive=before,
        model_generation=clubs.get('generation') if clubs else None,
        competition_support=support, assumes_block_independence=True,
        decision_at=current.isoformat()))
