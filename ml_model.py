"""Modèle séquentiel local, avec évaluation hors entraînement par coupon.

Le modèle de Markov d'ordre 1 apprend P(suivant | dernier élément).
Les paramètres sont estimés directement avec lissage de Laplace (alpha=1) :
aucune époque ni optimisation itérative n'est nécessaire.
"""
from datetime import datetime, timezone
import hashlib
import json
import math

from analytics import VALID, normalize_sequence

MIN_COUPONS = 5
MODEL_VERSION = 1


def dataset_fingerprint(rows):
    """Détecte ajout, suppression, modification et changement d'ordre."""
    payload = [(r['id'], list(r['sequence'])) for r in rows]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def fit_model(sequences):
    """Apprend uniquement les transitions internes aux coupons fournis."""
    counts = {previous: {value: 0 for value in VALID} for previous in VALID}
    targets = {value: 0 for value in VALID}
    n_coupons = 0
    for sequence in sequences:
        sequence = normalize_sequence(sequence)
        if len(sequence) < 2:
            raise ValueError('Chaque coupon doit contenir au moins 2 éléments.')
        n_coupons += 1
        for previous, value in zip(sequence, sequence[1:]):
            counts[previous][value] += 1
            targets[value] += 1
    if not n_coupons:
        raise ValueError('Aucun coupon disponible pour apprendre.')
    return {
        'version': MODEL_VERSION,
        'type': 'markov_order_1',
        'alpha': 1.0,
        'counts': counts,
        'targets': targets,
        'coupons': n_coupons,
        'transitions': sum(targets.values()),
    }


def _probabilities(counts, alpha=1.0):
    denominator = sum(counts.values()) + len(VALID) * alpha
    return {value: (counts[value] + alpha) / denominator for value in VALID}


def predict_next(model, context):
    """Probabilités lissées ; repli global si le dernier état est inédit."""
    context = normalize_sequence(context)
    counts = model['counts'][context[-1]] if context else {}
    support = sum(counts.values())
    fallback = support == 0
    if fallback:
        counts = model['targets']
    return {
        'probabilities': _probabilities(counts, model['alpha']),
        'context_support': support,
        'used_global_fallback': fallback,
        'last_value': context[-1] if context else None,
    }


def _winner(probabilities):
    # Ordre fixe et publié pour les ex aequo dans le score d'exactitude.
    return max(VALID, key=lambda value: probabilities[value])


def evaluate_model(model, test_sequences):
    """Prédiction à un pas avec préfixes réels, sans mise à jour du modèle."""
    baseline = _probabilities(model['targets'], model['alpha'])
    scores = {
        name: {'correct': 0, 'log_loss_sum': 0.0,
               'confusion': {actual: {pred: 0 for pred in VALID} for actual in VALID}}
        for name in ('model', 'baseline')
    }
    n = 0
    for sequence in test_sequences:
        sequence = normalize_sequence(sequence)
        if len(sequence) < 2:
            raise ValueError('Un coupon de test est incomplet.')
        for i in range(1, len(sequence)):
            actual = sequence[i]
            probabilities = predict_next(model, (sequence[i - 1],))['probabilities']
            for name, distribution in [('model', probabilities), ('baseline', baseline)]:
                predicted = _winner(distribution)
                scores[name]['correct'] += int(predicted == actual)
                scores[name]['log_loss_sum'] -= math.log(distribution[actual])
                scores[name]['confusion'][actual][predicted] += 1
            n += 1
    if n == 0:
        raise ValueError('Aucune transition disponible pour le test.')
    for score in scores.values():
        score['accuracy'] = score['correct'] / n
        score['log_loss'] = score.pop('log_loss_sum') / n
    return {'predictions': n, **scores}


def train_experiment(rows):
    """Teste sur les 20 % derniers coupons insérés, puis réajuste sur tous.

    Le modèle d'évaluation est conservé séparément du modèle final. Aucune
    information des coupons de test n'entre dans son estimation de paramètres.
    """
    rows = list(rows)
    if len(rows) < MIN_COUPONS:
        raise ValueError(f'Il faut au moins {MIN_COUPONS} coupons pour séparer apprentissage et test.')
    ids = [r['id'] for r in rows]
    if len(set(ids)) != len(ids):
        raise ValueError('Les identifiants des coupons doivent être distincts.')
    sequences = [normalize_sequence(r['sequence']) for r in rows]
    if any(len(seq) < 2 for seq in sequences):
        raise ValueError('Chaque coupon doit contenir au moins 2 éléments.')
    test_size = math.ceil(len(rows) * 0.2)
    split = len(rows) - test_size
    train, test = sequences[:split], sequences[split:]
    evaluation_model = fit_model(train)
    evaluation = evaluate_model(evaluation_model, test)
    train_patterns = set(train)
    overlaps = sum(seq in train_patterns for seq in test)
    return {
        'version': MODEL_VERSION,
        'trained_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'fingerprint': dataset_fingerprint(rows),
        'split_policy': 'last_20_percent_of_coupons_in_insertion_order_ceil',
        'train_ids': ids[:split],
        'test_ids': ids[split:],
        'test_patterns_already_in_train': overlaps,
        'unique_patterns': len(set(sequences)),
        'evaluation_model': evaluation_model,
        'evaluation': evaluation,
        'model': fit_model(sequences),
    }


def export_experiment(experiment):
    return json.dumps(experiment, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
