"""Chronologically selected CatBoost scenarios and native, versioned artifacts.

Validation alone chooses tree parameters and the recurrence exponent. The test
uses frozen parameters and train+validation weights. Lagged features and the
clubs baseline may see already played earlier test days, never any member of the
target group's first day or later. Deployment is a separate all-past-data refit.
"""
from datetime import datetime, timezone
import hashlib
import importlib
import json
from math import fsum, log, prod
import os
from pathlib import Path
import re
import shutil
import tempfile

from coupon_sizes import top_independent, validate_size
from league_model import VALUES, outcome, score
from league_patterns import repeated_scenarios
from match_predictor import estimate
from scenario_predictor import PRIOR_WEIGHT, best_sequences, fit_sequences, probability


VERSION = "catboost-scenarios-v1"
DATA_DIR = Path(__file__).resolve().parent / "data" / "models"
CANDIDATES = ({"depth": 3, "iterations": 150, "l2_leaf_reg": 5},
              {"depth": 5, "iterations": 300, "l2_leaf_reg": 8})
ALPHAS = (0.0, .25, .5, 1.0)
GROUP_SIZE = 4
METHODS = ("frequency", "clubs", "combined", "catboost")
_SOURCES = {"API-Football": ("API-Football", "api-football", "api-football"),
            "api-football": ("API-Football", "api-football", "api-football")}


class ModelError(ValueError):
    """A model is unavailable, inconsistent, stale, or unsuitable for the target day."""


def _features():
    try:
        return importlib.import_module("scenario_features")
    except ImportError:
        raise ModelError("Le module des caractéristiques du modèle est indisponible.") from None


def _classifier():
    try:
        from catboost import CatBoostClassifier
    except ImportError:
        raise ModelError("CatBoost n’est pas installé dans cet environnement.") from None
    return CatBoostClassifier


def _today():
    return datetime.now(timezone.utc).date().isoformat()


def _utc_stamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _source(source):
    if source not in _SOURCES:
        raise ModelError("Source du modèle inconnue.")
    return _SOURCES[source]


def _rows(rows, league, source, before=None):
    _, provider, _ = _source(source)
    selected = {}
    for raw in rows:
        if str(raw.get("league")) != str(league):
            continue
        actual_provider = raw.get("provider")
        if actual_provider != provider:
            continue
        row = dict(raw)
        row["league"] = str(league)
        row["provider"] = provider
        try:
            row["timestamp"] = _utc_stamp(row["timestamp"])
            if before is not None and row["timestamp"][:10] >= before:
                continue
            if outcome(score(row["ht"]), score(row["ft"])) != row["value"]:
                raise ValueError("Score et résultat contradictoires")
            mid = str(row["match_id"])
            int(mid)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ModelError("L’historique contient un match ou un score invalide.") from None
        if mid in selected and selected[mid] != row:
            raise ModelError("L’historique contient un identifiant contradictoire.")
        selected[mid] = row
    return sorted(selected.values(), key=lambda row: (row["timestamp"], int(row["match_id"])))


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _fingerprint(rows):
    return hashlib.sha256(_json(rows).encode()).hexdigest()


def _location(source, league, directory):
    _, _, slug = _source(source)
    key = str(league)
    safe = key if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", key) else hashlib.sha256(key.encode()).hexdigest()
    return Path(directory or DATA_DIR) / slug / safe


def _pool(features, columns, categorical, labels=None):
    try:
        from catboost import Pool
        values = [[item[column] for column in columns] for item in features]
        return Pool(values, label=labels, feature_names=list(columns),
                    cat_features=[columns.index(name) for name in categorical])
    except ImportError:
        raise ModelError("CatBoost n’est pas installé dans cet environnement.") from None


def _fit(examples, params, columns, categorical):
    labels = [item["label"] for item in examples]
    if set(labels) != set(VALUES):
        raise ModelError("L’apprentissage doit contenir les trois résultats 1st, Égal et 2nd.")
    model = _classifier()(loss_function="MultiClass", class_names=list(VALUES), random_seed=20261018,
                          thread_count=2, has_time=True, allow_writing_files=False, verbose=False, **params)
    try:
        model.fit(_pool([item["features"] for item in examples], columns, categorical, labels))
    except ModelError:
        raise
    except Exception:
        raise ModelError("CatBoost n’a pas pu apprendre sur ces caractéristiques.") from None
    return model


def _predict(model, features, columns, categorical):
    values = model.predict_proba(_pool(features, columns, categorical))
    result = []
    for probabilities in values:
        mapped = {str(label): max(float(p), 1e-15) for label, p in zip(model.classes_, probabilities)}
        if set(mapped) != set(VALUES):
            raise ModelError("Les classes du modèle ne correspondent pas aux trois résultats attendus.")
        total = fsum(mapped.values())
        result.append({label: mapped[label] / total for label in VALUES})
    return result


def _distribution(fit, distributions, alpha):
    n = fit[1]
    unseen = (PRIOR_WEIGHT / (n + PRIOR_WEIGHT)) ** alpha
    correction = {seq: (probability(fit, seq) / probability(fit, seq, baseline=True)) ** alpha for seq in fit[0]}
    z = unseen + fsum(prod(p[v] for p, v in zip(distributions, seq)) * (factor - unseen)
                      for seq, factor in correction.items())
    return dict(fit=fit, distributions=distributions, alpha=alpha, unseen=unseen, correction=correction, z=z)


def _details(model, sequence):
    sequence = tuple(sequence)
    q = prod(p[v] for p, v in zip(model["distributions"], sequence))
    b = probability(model["fit"], sequence, baseline=True)
    observed, expected = model["fit"][0][sequence], model["fit"][1] * b
    return dict(sequence=sequence, probability=q * model["correction"].get(sequence, model["unseen"]) / model["z"],
                club_probability=q, frequency_probability=probability(model["fit"], sequence),
                chance_probability=b, observed=observed, expected=expected,
                lift=observed / expected if expected else None)


def _rank(model, limit=3):
    candidates = set(model["fit"][0])
    candidates.update(item["sequence"] for item in top_independent(model["distributions"], limit))
    return sorted((_details(model, seq) for seq in candidates),
                  key=lambda item: (-item["probability"], item["sequence"]))[:limit]


def _blocks(rows, size=GROUP_SIZE):
    if not rows:
        return []
    return repeated_scenarios(rows, size)[rows[0]["league"]]["blocks"]


def _contained(blocks, examples):
    ids = {str(item["match"]["match_id"]) for item in examples}
    return [block for block in blocks if all(str(row["match_id"]) in ids for row in block["matches"])]


def _feature(module, rows, fixture, before):
    value = module.feature_for_fixture(rows, fixture, before=before)
    return value["features"] if "features" in value and isinstance(value["features"], dict) else value


def _group_inputs(module, rows, blocks):
    prepared = []
    for block in blocks:
        before = min(row["timestamp"][:10] for row in block["matches"])
        features = [_feature(module, rows, row, before) for row in block["matches"]]
        clubs = [estimate(rows, row["league"], row["home"], row["away"], before)["probabilities"]
                 for row in block["matches"]]
        prepared.append(dict(block=block, before=before, features=features, clubs=clubs))
    return prepared


def _metric(actual, ranked, p):
    return dict(top1=actual == ranked[0], top3=actual in ranked[:3], loss=-log(max(p, 1e-300)))


def _evaluate(model, groups, fit, alpha, columns, categorical):
    scores = {name: [] for name in METHODS}
    targets = []
    for group in groups:
        actual = tuple(group["block"]["sequence"])
        cb = _predict(model, group["features"], columns, categorical)
        contexts = {"clubs": _distribution(fit, group["clubs"], 0),
                    "combined": _distribution(fit, group["clubs"], .5),
                    "catboost": _distribution(fit, cb, alpha)}
        frequency_rank = best_sequences(fit, 3)
        scores["frequency"].append(_metric(actual, frequency_rank, probability(fit, actual)))
        for name, context in contexts.items():
            ranked = _rank(context)
            scores[name].append(_metric(actual, [row["sequence"] for row in ranked], _details(context, actual)["probability"]))
        targets.append(dict(day=group["before"], match_ids=[str(row["match_id"]) for row in group["block"]["matches"]]))
    output = {}
    for name, rows in scores.items():
        n = len(rows)
        output[name] = dict(tested=n, top1=sum(row["top1"] for row in rows), top3=sum(row["top3"] for row in rows),
                            loss=fsum(row["loss"] for row in rows) / n if n else None,
                            top1_rate=sum(row["top1"] for row in rows) / n if n else None,
                            top3_rate=sum(row["top3"] for row in rows) / n if n else None)
    return output, targets


def _split_details(examples, groups):
    days = [row["match"]["timestamp"][:10] for row in examples]
    return dict(matches=len(examples), start=min(days), end=max(days), groups=len(groups))


def _publish(model, report, source, league, directory):
    if Path(directory or DATA_DIR).resolve() == Path(DATA_DIR).resolve():
        import model_campaign as campaign
        try:
            with campaign.manual_publication(league):
                return _publish_unlocked(model, report, source, league, directory)
        except campaign.CampaignError as error:
            raise ModelError(str(error)) from None
    return _publish_unlocked(model, report, source, league, directory)


def _publish_unlocked(model, report, source, league, directory):
    base = _location(source, league, directory)
    base.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".training-", dir=base))
    pointer_temp = None
    try:
        model.save_model(str(temporary / "model.cbm"), format="cbm")
        report["deployment"]["model_sha256"] = hashlib.sha256((temporary / "model.cbm").read_bytes()).hexdigest()
        generation = hashlib.sha256((_json(report) + report["deployment"]["model_sha256"]).encode()).hexdigest()
        report["deployment"]["generation"] = generation
        raw_report = _json(report).encode()
        (temporary / "report.json").write_bytes(raw_report)
        target = base / generation
        if target.exists():
            shutil.rmtree(temporary)
        else:
            os.replace(temporary, target)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=base, prefix=".pointer-", delete=False) as handle:
            pointer_temp = Path(handle.name)
            json.dump(dict(generation=generation, report_sha256=hashlib.sha256(raw_report).hexdigest()), handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pointer_temp, base / "CURRENT.json")
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
        if pointer_temp is not None and pointer_temp.exists():
            pointer_temp.unlink()


def train(rows, league, source, directory=None, *, before=None):
    """Select on validation, report untouched test, then refit deployment on all past days."""
    module = _features()
    canonical, provider, _ = _source(source)
    cutoff = _today() if before is None else before
    try:
        if datetime.strptime(cutoff, '%Y-%m-%d').date().isoformat() != cutoff or cutoff > _today():
            raise ValueError()
    except (ValueError, TypeError):
        raise ModelError('La date limite d’apprentissage doit être un jour UTC actuel ou antérieur.') from None
    history = _rows(rows, league, source, cutoff)
    try:
        examples = module.build_examples(history, str(league), provider=provider)
        split = module.chronological_split(examples, .6, .2)
    except ValueError as error:
        raise ModelError(str(error)) from None
    columns, categorical = list(module.FEATURE_COLUMNS), list(module.CATEGORICAL_COLUMNS)
    blocks = _blocks(history)
    groups = {name: _contained(blocks, split[name]) for name in ("train", "validation", "test")}
    if not groups["validation"] or not groups["test"] or not groups["train"]:
        raise ModelError("Pas assez de groupes complets entièrement contenus dans chaque période.")
    train_fit = fit_sequences([block["sequence"] for block in groups["train"]], GROUP_SIZE)
    validation_inputs = _group_inputs(module, history, groups["validation"])
    test_inputs = _group_inputs(module, history, groups["test"])
    selection = None
    trials = []
    for params in CANDIDATES:
        model = _fit(split["train"], params, columns, categorical)
        for alpha in ALPHAS:
            metrics, targets = _evaluate(model, validation_inputs, train_fit, alpha, columns, categorical)
            loss = metrics["catboost"]["loss"]
            trials.append(dict(params=dict(params), alpha=alpha, loss=loss))
            if selection is None or loss < selection["validation_loss"]:
                selection = dict(params=dict(params), alpha=alpha, validation_loss=loss)
                selected_validation, validation_targets = metrics, targets
    train_val = split["train"] + split["validation"]
    frozen = _fit(train_val, selection["params"], columns, categorical)
    train_val_groups = _contained(blocks, train_val)
    test_fit = fit_sequences([block["sequence"] for block in train_val_groups], GROUP_SIZE)
    test_metrics, test_targets = _evaluate(frozen, test_inputs, test_fit, selection["alpha"], columns, categorical)
    deployment = _fit(examples, selection["params"], columns, categorical)
    report = dict(version=VERSION, feature_version=module.FEATURE_VERSION, source=canonical, provider=provider,
                  league=str(league), created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  feature_columns=columns, categorical_columns=categorical, labels=list(VALUES),
                  split={name: _split_details(split[name], groups[name]) for name in groups},
                  selection=dict(selection, candidates=trials, criterion="validation whole-group logloss", validated_size=GROUP_SIZE),
                  validation=selected_validation, test=test_metrics,
                  targets=dict(validation=validation_targets, test=test_targets),
                  policy=dict(grouping="Fixed chronological non-overlapping groups of four; boundary-crossing groups excluded.",
                              features="All features and clubs baselines use days strictly before each whole group's first day. Earlier test days may update lagged state; model parameters and alpha stay frozen.",
                              repeats="Frozen training groups for validation; train+validation groups for test.",
                              deployment="Separate refit on all local usable rows before the current UTC day, after reporting the test.",
                              test_selection=False, probability_floor=1e-15),
                  deployment=dict(rows=len(history), trained_through=history[-1]["timestamp"][:10], cutoff_exclusive=cutoff,
                                  groups=len(blocks), fingerprint=_fingerprint(history),
                                  excluded_current_or_future=sum(
                                      str(row.get("league")) == str(league)
                                      and row.get("provider") == provider
                                      and _utc_stamp(row["timestamp"])[:10] >= cutoff for row in rows)))
    try:
        _publish(deployment, report, source, league, directory)
    except OSError:
        raise ModelError("Impossible d’enregistrer le modèle entraîné localement.") from None
    return report


def load_report(source, league, directory=None):
    """Read small JSON artifacts only; importing CatBoost is deferred until inference."""
    base = _location(source, league, directory)
    pointer = base / "CURRENT.json"
    if not pointer.exists():
        return None
    try:
        current = json.loads(pointer.read_text())
        generation = current["generation"]
        if not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{64}", generation):
            raise ValueError("Invalid generation")
        raw = (base / generation / "report.json").read_bytes()
        if hashlib.sha256(raw).hexdigest() != current["report_sha256"]:
            raise ValueError("Invalid report digest")
        report = json.loads(raw)
        if (report["version"] != VERSION or report["source"] != _source(source)[0] or report["league"] != str(league)
                or report["deployment"]["generation"] != generation):
            raise ValueError("Model identity mismatch")
        if (not isinstance(report["feature_version"], str) or not isinstance(report["feature_columns"], list)
                or not report["feature_columns"] or not isinstance(report["categorical_columns"], list)
                or set(report["labels"]) != set(VALUES) or report["selection"]["alpha"] not in ALPHAS
                or not re.fullmatch(r"[0-9a-f]{64}", report["deployment"]["model_sha256"])):
            raise ValueError("Invalid model schema")
        datetime.strptime(report["deployment"]["trained_through"], "%Y-%m-%d")
        datetime.strptime(report["deployment"]["cutoff_exclusive"], "%Y-%m-%d")
        return report
    except (KeyError, TypeError, ValueError, OSError):
        raise ModelError("Le rapport du modèle local est invalide ou incomplet.") from None


def forecast(rows, fixtures, source, directory=None, limit=3):
    """Load a frozen trained model, enforce provenance/cutoff, and rank exactly."""
    fixtures = list(fixtures)
    validate_size(len(fixtures))
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ModelError("Limite de classement invalide.")
    leagues = {str(row.get("league")) for row in fixtures}
    canonical, provider, _ = _source(source)
    if len(leagues) != 1 or "None" in leagues or any(row.get("provider") not in (None, provider) for row in fixtures):
        raise ModelError("Le groupe doit utiliser une seule compétition et une seule source.")
    league = leagues.pop()
    report = load_report(source, league, directory)
    if report is None:
        raise ModelError("Aucun modèle CatBoost entraîné pour cette source et cette compétition.")
    module = _features()
    if (report["feature_version"] != module.FEATURE_VERSION or report["feature_columns"] != list(module.FEATURE_COLUMNS)
            or report["categorical_columns"] != list(module.CATEGORICAL_COLUMNS)):
        raise ModelError("Les caractéristiques ont changé ; ce modèle exige un contrôle de compatibilité.")
    try:
        before = min(_utc_stamp(row["timestamp"])[:10] for row in fixtures)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ModelError("La date des affiches est invalide.") from None
    deployment = report["deployment"]
    if before <= deployment["trained_through"]:
        raise ModelError("Ce modèle a déjà vu des jours de cette période : choisis une date après son apprentissage.")
    fit_rows = _rows(rows, league, source, deployment["cutoff_exclusive"])
    if _fingerprint(fit_rows) != deployment["fingerprint"]:
        raise ModelError("L’historique appris a changé ; ce modèle est indisponible jusqu’au contrôle des données et de sa campagne.")
    history = _rows(rows, league, source, before)
    seen, ids = set(), set()
    for row in fixtures:
        identity = row.get("home"), row.get("away"), _utc_stamp(row["timestamp"])
        mid = row.get("match_id")
        if not row.get("home") or not row.get("away") or row["home"] == row["away"] or identity in seen or mid is not None and str(mid) in ids:
            raise ModelError("Choisis des affiches distinctes avec deux clubs différents.")
        seen.add(identity)
        if mid is not None:
            ids.add(str(mid))
    path = _location(source, league, directory) / deployment["generation"] / "model.cbm"
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != deployment["model_sha256"]:
        raise ModelError("Le fichier du modèle est absent ou invalide.")
    model = _classifier()()
    try:
        model.load_model(str(path), format="cbm")
    except Exception:
        raise ModelError("Le modèle CatBoost natif ne peut pas être chargé.") from None
    features = [_feature(module, history, row, before) for row in fixtures]
    probabilities = _predict(model, features, report["feature_columns"], report["categorical_columns"])
    blocks = _blocks(fit_rows, len(fixtures))
    fit = fit_sequences([block["sequence"] for block in blocks], len(fixtures))
    distribution = _distribution(fit, probabilities, report["selection"]["alpha"])
    estimates = []
    for fixture, p in zip(fixtures, probabilities):
        metadata = estimate(history, league, fixture["home"], fixture["away"], before)
        estimates.append(dict(metadata, probabilities=p, model_family="CatBoost"))
    return dict(eligible=True, version=VERSION, source=canonical, league=league, size=len(fixtures), before=before,
                method="CatBoost", model_label="CatBoost entraîné", validated_size=GROUP_SIZE,
                training_groups=len(blocks), training_matches=deployment["rows"],
                club_estimates=estimates, personalized_count=sum(row["personalized"] for row in estimates),
                personalized_matches=sum(row["personalized"] for row in estimates),
                ranking=_rank(distribution, limit), normalization=distribution["z"], latest=deployment["trained_through"],
                alpha=report["selection"]["alpha"], generation=deployment["generation"],
                diagnostic="Paramètres sélectionnés et testés sur des groupes de quatre matchs.")
