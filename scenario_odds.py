"""Indicative decimal odds supplied by the user, not model probabilities."""
from decimal import Decimal, InvalidOperation, localcontext
from math import isfinite

DEFAULT_ODDS = {'1st': 3.10, 'Égal': 3.00, '2nd': 2.10}


def _decimal_product(sequence, odds):
    try:
        values = [Decimal(str(odds[value])) for value in sequence]
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError('Les cotes doivent être finies et supérieures à 1.') from error
    if not values or any(not value.is_finite() or value <= 1 for value in values):
        raise ValueError('Les cotes doivent être finies et supérieures à 1.')
    with localcontext() as context:
        context.prec = 64
        result = Decimal(1)
        for value in values:
            result *= value
    if not result.is_finite() or not isfinite(float(result)):
        raise ValueError('La cote cumulée doit rester finie.')
    return result


def combined_odds(sequence, odds=None):
    """Product over the complete scenario; preserve the numeric legacy API."""
    return float(_decimal_product(sequence, DEFAULT_ODDS if odds is None else odds))


def format_odds(value):
    """French decimal display, rounded only after the full calculation."""
    return f'{value:,.2f}'.replace(',', '\u202f').replace('.', ',')


def scenario_odds_text(sequence):
    """Display today's manually supplied reference, without altering forecasts."""
    return format_odds(_decimal_product(sequence, DEFAULT_ODDS))
