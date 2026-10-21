"""Exact contextual scenario ranking, with earlier-day-only comparisons.

Club probabilities are corrected at half strength by the ratio between observed
league scenarios and independent position frequencies. The partition function
is exact: only observed scenarios differ from the common unseen correction.

Position frequencies use Laplace smoothing. The descriptive ``expected`` and
``lift`` values therefore reflect that smoothing, including in constant data;
they are not a significance test or proof of dependence between match outcomes.
The club product itself assumes independence of the supplied fixtures.
"""
from datetime import date, datetime
from math import fsum, log, prod, sqrt

from coupon_sizes import top_independent, validate_size
from league_model import VALUES
from match_predictor import MIN_DAYS, MIN_LEAGUE, estimate
from scenario_predictor import MIN_TRAIN, PRIOR_WEIGHT, best_sequences, fit_sequences, probability


VERSION = "contextual-scenarios-v1"
MODEL_NAMES = ("frequency", "clubs", "combined")


def _day(timestamp):
    if not isinstance(timestamp, str):
        raise ValueError("Date du match absente ou invalide")
    try:
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return date.fromisoformat(timestamp[:10]).isoformat()
    except ValueError:
        raise ValueError("Date du match absente ou invalide") from None


def _validate_limit(limit):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Limite de classement invalide")


def _validate_inputs(result, fixtures, before):
    fixtures = list(fixtures)
    size = len(fixtures)
    validate_size(size)
    if result.get("size", 4) != size:
        raise ValueError("Le nombre d’affiches doit correspondre à la taille des groupes historiques")
    rows = result.get("matches", [])
    leagues = {row.get("league") for row in rows}
    if len(leagues) > 1 or None in leagues:
        raise ValueError("Une seule compétition est autorisée")
    league = next(iter(leagues), None)
    providers = {row["provider"] for row in rows if row.get("provider")}
    if len(providers) > 1:
        raise ValueError("Une seule source de matchs est autorisée")
    provider = next(iter(providers), None)
    dates, seen, ids = [], set(), set()
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            raise ValueError("Affiche absente ou invalide")
        home, away = fixture.get("home"), fixture.get("away")
        if not all(isinstance(team, str) and team.strip() for team in (home, away)) or home == away:
            raise ValueError("Choisis deux équipes différentes pour chaque affiche")
        stamp = fixture.get("timestamp")
        dates.append(_day(stamp))
        if fixture.get("league") is not None:
            if league is None:
                league = fixture["league"]
            elif fixture["league"] != league:
                raise ValueError("Une seule compétition est autorisée")
        if fixture.get("provider"):
            if provider is None:
                provider = fixture["provider"]
            elif fixture["provider"] != provider:
                raise ValueError("Une seule source de matchs est autorisée")
        identity = (home, away, stamp)
        mid = fixture.get("match_id")
        if identity in seen or mid is not None and str(mid) in ids:
            raise ValueError("Chaque affiche doit apparaître une seule fois")
        seen.add(identity)
        if mid is not None:
            ids.add(str(mid))
    first_day = min(dates)
    if before is None:
        before = first_day
    else:
        try:
            before = date.fromisoformat(before).isoformat()
        except (TypeError, ValueError):
            raise ValueError("Date limite invalide") from None
        if before > first_day:
            raise ValueError("L’apprentissage doit précéder le jour du premier match")
    return fixtures, size, league, before


def _club_probability(distributions, sequence):
    return prod(distribution[value] for distribution, value in zip(distributions, sequence))


def _combined_model(fit, distributions):
    """A compact exact distribution over up to 3**20 possible sequences."""
    counts, n, _ = fit
    unseen = sqrt(PRIOR_WEIGHT / (n + PRIOR_WEIGHT))
    corrections = {}
    bonuses = []
    for sequence in counts:
        chance = probability(fit, sequence, baseline=True)
        correction = sqrt(probability(fit, sequence) / chance)
        corrections[sequence] = correction
        bonuses.append(_club_probability(distributions, sequence) * (correction - unseen))
    normalization = unseen + fsum(bonuses)
    return dict(fit=fit, distributions=distributions, unseen=unseen,
                corrections=corrections, normalization=normalization)


def _sequence_details(model, sequence):
    sequence = tuple(sequence)
    fit = model["fit"]
    if len(sequence) != len(fit[2]) or any(value not in VALUES for value in sequence):
        raise ValueError("Scénario invalide ou de taille différente")
    q = _club_probability(model["distributions"], sequence)
    b = probability(fit, sequence, baseline=True)
    observed = fit[0][sequence]
    expected = fit[1] * b
    correction = model["corrections"].get(sequence, model["unseen"])
    return dict(sequence=sequence, probability=q * correction / model["normalization"],
                club_probability=q, frequency_probability=probability(fit, sequence),
                chance_probability=b, observed=observed, expected=expected,
                lift=observed / expected if expected else None, correction=correction)


def _ranking(model, limit):
    # Unobserved sequences all have the same correction. An unobserved sequence
    # outside the club top k therefore cannot enter the combined top k.
    candidates = set(model["fit"][0])
    candidates.update(row["sequence"] for row in top_independent(model["distributions"], limit))
    ranking = [_sequence_details(model, sequence) for sequence in candidates]
    return sorted(ranking, key=lambda row: (-row["probability"], row["sequence"]))[:limit]


def _prepare(result, fixtures, before=None, limit=3, estimate_cache=None, fit_cache=None):
    _validate_limit(limit)
    fixtures, size, league, before = _validate_inputs(result, fixtures, before)
    rows = result.get("matches", [])
    if fit_cache is not None and before in fit_cache:
        fit = fit_cache[before]
    else:
        blocks = [block for block in result.get("blocks", [])
                  if block.get("matches") and max(_day(row["timestamp"]) for row in block["matches"]) < before]
        fit = fit_sequences([block["sequence"] for block in blocks], size)
        if fit_cache is not None:
            fit_cache[before] = fit
    club_estimates = []
    for fixture in fixtures:
        cache_key = (fixture["home"], fixture["away"], before)
        if estimate_cache is not None and cache_key in estimate_cache:
            item = estimate_cache[cache_key]
        else:
            item = estimate(rows, league, fixture["home"], fixture["away"], before)
            if estimate_cache is not None:
                estimate_cache[cache_key] = item
        club_estimates.append(dict(item, home=fixture["home"], away=fixture["away"],
                                   timestamp=fixture["timestamp"], match_id=fixture.get("match_id")))
    n = fit[1]
    training_matches = club_estimates[0]["training"]
    eligible = n >= MIN_TRAIN and all(item["eligible"] for item in club_estimates)
    diagnostics = []
    if n < MIN_TRAIN:
        diagnostics.append(f"Il faut au moins {MIN_TRAIN} groupes historiques complets avant cette date ({n} disponibles).")
    if not all(item["eligible"] for item in club_estimates):
        diagnostics.append(f"Il faut au moins {MIN_LEAGUE} matchs de la compétition sur {MIN_DAYS} jours antérieurs.")
    personalized = sum(item["personalized"] for item in club_estimates)
    if personalized < size:
        diagnostics.append(f"{size - personalized} affiche(s) utilisent la tendance de la compétition, faute d’historique des clubs suffisant.")
    prior_rows = [row for row in rows if row["league"] == league and _day(row["timestamp"]) < before]
    output = dict(version=VERSION, eligible=eligible, size=size, league=league,
                  training_groups=n, training_matches=training_matches, before=before,
                  club_estimates=club_estimates, personalized_matches=personalized, personalized_count=personalized,
                  ranking=[], normalization=None,
                  latest=max((row["timestamp"] for row in prior_rows), default=None),
                  diagnostic=" ".join(diagnostics), diagnostics=diagnostics)
    if not eligible:
        return output, None
    model = _combined_model(fit, [item["probabilities"] for item in club_estimates])
    output.update(ranking=_ranking(model, limit), normalization=model["normalization"])
    return output, model


def forecast(result, fixtures, before=None, limit=3):
    """Rank a supplied ordered group using strictly earlier calendar days only.

    Fixtures supply team names and kickoff dates, never their result labels.
    ``before`` may move the cutoff earlier, but cannot allow the first target day.
    Probabilities retain their full-distribution values; the top k is not rescaled.
    Expected counts and lift are smoothed descriptive references, not p-values.
    """
    return _prepare(result, fixtures, before, limit)[0]


def _metric(probability_value, actual, ranking, limit):
    return dict(top1=actual == ranking[0], top3=actual in ranking[:limit],
                probability=probability_value, loss=-log(probability_value))


def evaluate(result, limit=3):
    """Compare league frequencies, clubs, and their combination chronologically."""
    _validate_limit(limit)
    size = result.get("size", 4)
    validate_size(size)
    trials, estimate_cache, fit_cache = [], {}, {}
    for block in result.get("blocks", []):
        fixtures = [{key: row[key] for key in ("home", "away", "timestamp", "league", "match_id")}
                    for row in block["matches"]]
        prepared, model = _prepare(result, fixtures, limit=limit,
                                   estimate_cache=estimate_cache, fit_cache=fit_cache)
        if not prepared["eligible"]:
            continue
        actual = tuple(block["sequence"])
        details = _sequence_details(model, actual)
        frequency_ranking = best_sequences(model["fit"], limit)
        clubs_ranking = [row["sequence"] for row in top_independent(model["distributions"], limit)]
        combined_ranking = [row["sequence"] for row in prepared["ranking"]]
        models = {"frequency": _metric(details["frequency_probability"], actual, frequency_ranking, limit),
                  "clubs": _metric(details["club_probability"], actual, clubs_ranking, limit),
                  "combined": _metric(details["probability"], actual, combined_ranking, limit)}
        trials.append(dict(group=block["number"], day=prepared["before"],
                           training_groups=prepared["training_groups"], training_matches=prepared["training_matches"],
                           actual=actual, personalized_matches=prepared["personalized_matches"], models=models))
    n = len(trials)
    summary = {"tested": n, "models": {}}
    for name in MODEL_NAMES:
        scores = [trial["models"][name] for trial in trials]
        top1, top3 = sum(row["top1"] for row in scores), sum(row["top3"] for row in scores)
        summary["models"][name] = dict(tested=n, top1=top1, top3=top3,
                                      top1_rate=top1 / n if n else None, top3_rate=top3 / n if n else None,
                                      loss=fsum(row["loss"] for row in scores) / n if n else None)
    return dict(size=size, limit=limit, trials=trials, summary=summary)
