"""Pure contracts for the regulation-time target and its probabilities.

No provider, database, model, Streamlit or clock is imported here. Missing
scores are not zeros. Extra time, penalties and administrative awards must
never be substituted for the two regulation-time score pairs by an adapter.
"""
from dataclasses import dataclass
from math import fsum, isclose, isfinite
from numbers import Real
import re


CLASS_ORDER = ('1st', 'Égal', '2nd')
TARGET_VERSION = 'halftime-regulation-v1'
FINISHED_STATUSES = frozenset(('FT', 'AET', 'PEN'))


class ContractError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def parse_score(value):
    """Read exactly two nonnegative integers, without numerical coercion.

    Strings are the legacy local HT/FT representation; mappings are API score
    pairs. A missing component, bool, fractional number or extra component is
    invalid. The returned tuple is independent of the caller's mutable value.
    """
    if isinstance(value, str):
        text = value.strip()
        if text.startswith('(') and text.endswith(')'):
            text = text[1:-1].strip()
        found = re.fullmatch(r'(\d+)\s*-\s*(\d+)', text)
        if found:
            return tuple(map(int, found.groups()))
    else:
        if isinstance(value, dict):
            value = (value.get('home'), value.get('away'))
        if (isinstance(value, (tuple, list)) and len(value) == 2
                and all(type(number) is int and number >= 0 for number in value)):
            return tuple(value)
    raise ContractError('INVALID_SCORE', 'Score HT/FT absent ou invalide')


@dataclass(frozen=True)
class RegulationResult:
    halftime: tuple[int, int]
    fulltime: tuple[int, int]
    first_goals: int
    second_goals: int
    label: str


def regulation_result(halftime, fulltime, *, status=None):
    """Compare total goals in each half, using regulation fulltime only.

    A provider adapter must supply a final status. Status can be omitted by
    readers of already-normalized legacy rows; that does not certify when the
    data was received or that a prediction was made before kickoff.
    """
    if status is not None and (not isinstance(status, str) or status not in FINISHED_STATUSES):
        raise ContractError('MATCH_NOT_FINISHED', 'Match non terminé ou résultat administratif non évaluable')
    ht, ft = parse_score(halftime), parse_score(fulltime)
    if any(first > final for first, final in zip(ht, ft)):
        raise ContractError('INCONSISTENT_SCORE', 'HT supérieur au FT')
    first, second = sum(ht), sum(ft) - sum(ht)
    label = '1st' if first > second else '2nd' if second > first else 'Égal'
    return RegulationResult(ht, ft, first, second, label)


def outcome(halftime, fulltime):
    return regulation_result(halftime, fulltime).label


def validate_probabilities(value, *, rel_tol=1e-9, abs_tol=1e-12):
    """Validate the three absolute probabilities; never normalize or clip.

    Canonical order is explicit, independent of mapping insertion order. A
    sparse top-scenario list is not a three-class probability distribution.
    """
    if (not isinstance(value, dict) or set(value) != set(CLASS_ORDER)
            or any(isinstance(number, bool) or not isinstance(number, Real)
                   or not isfinite(number) or not 0 <= number <= 1 for number in value.values())
            or not isclose(fsum(value.values()), 1, rel_tol=rel_tol, abs_tol=abs_tol)):
        raise ContractError('INVALID_PROBABILITIES', 'Les trois probabilités avant match sont invalides.')
    return {label: value[label] for label in CLASS_ORDER}
