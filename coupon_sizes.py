"""Coupon size policy and bounded exact ranking for independent outcomes."""
MIN_SIZE = 2
MAX_SIZE = 20
SIZES = tuple(range(MIN_SIZE, MAX_SIZE + 1))


def validate_size(size):
    if isinstance(size, bool) or not isinstance(size, int) or size not in SIZES:
        raise ValueError('Un coupon doit contenir de 2 à 20 matchs.')
    return size


def top_independent(distributions, limit=81):
    """Exact top-k products: discarded prefixes cannot outrank k retained prefixes."""
    validate_size(len(distributions))
    if not 1 <= limit <= 1000:
        raise ValueError('Limite de classement invalide')
    candidates = [((), 1.0)]
    for distribution in distributions:
        candidates = sorted(((prefix + (value,), p * weight)
                             for prefix, p in candidates for value, weight in distribution.items()),
                            key=lambda item: (-item[1], item[0]))[:limit]
    return [{'sequence': seq, 'probability': p} for seq, p in candidates]
