"""Register once the criteria for future, descriptive paired comparisons.

Opening the journal only reads this file. Registration never touches forecasts,
results, models, the API or their automation settings.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile


VERSION = 'prospective-evidence-v1'
PATH = Path(__file__).resolve().parent/'data'/'api_football'/'prospective_protocol.json'
CRITERIA = dict(
    version=VERSION, baseline='clubs', primary='logloss', secondary='top3',
    challengers=['frequency', 'combined', 'catboost', 'context_neighbors'],
    sizes=list(range(2, 21)), confidence=.95, bootstrap_samples=4000,
    seed=20261018, min_blocks=20, block='utc_week_and_shared_fixtures',
    scope='one_competition_one_size_exact_method_versions',
    historical_policy='exclude_forecasts_registered_before_this_protocol',
    block_policy='connected_components_of_all_spanned_utc_weeks_and_shared_fixture_ids',
    maturity_policy='exclude_open_weeks_and_components_with_pending_or_unresolved_results',
    weighting='paired_cluster_bootstrap_ratio_of_summed_group_gains_to_summed_group_counts',
    favorable_rule='lower_loss_gain_above_zero_and_lower_top3_gain_at_least_zero',
    interpretation='descriptive_only_no_confirmation_or_automatic_promotion',
    limitations=[
        '20 blocks is a predefined operational minimum, not a guarantee of power or coverage.',
        'Percentile bootstrap intervals assume independent, comparable blocks; dependence may persist across weeks.',
        'Intervals are not adjusted for repeated consultation or multiple comparisons.',
        'Constant gains do not establish absence of uncertainty or equivalence.',
        'Filters and model availability can change the population being described.',
    ],
)


class ProtocolError(ValueError):
    """The registered protocol is invalid, changed or currently being created."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _fingerprint(value):
    return hashlib.sha256(_json({k:v for k,v in value.items() if k != 'fingerprint'}).encode()).hexdigest()


def validate_protocol(value):
    try:
        if not isinstance(value, dict) or set(value) != set(CRITERIA)|{'registered_at', 'fingerprint'}:
            raise ValueError()
        for key, expected in CRITERIA.items():
            # bools must not impersonate numeric criteria.
            if type(value[key]) is not type(expected) or value[key] != expected:
                raise ValueError()
        if any(type(size) is not int for size in value['sizes']):
            raise ValueError()
        stamp = datetime.fromisoformat(value['registered_at'])
        if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
            raise ValueError()
        if stamp.isoformat() != value['registered_at'] or value['fingerprint'] != _fingerprint(value):
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise ProtocolError('Le protocole prospectif est invalide ou a changé ; le suivi ne peut pas être recalculé sous de nouveaux critères.') from None
    return copy.deepcopy(value)


def load_protocol(path=None):
    path = Path(path if path is not None else PATH)
    if not path.exists():
        return None
    try:
        return validate_protocol(json.loads(path.read_text(encoding='utf-8')))
    except (ValueError, OSError) as error:
        if isinstance(error, ProtocolError):
            raise
        raise ProtocolError('Le protocole prospectif local est illisible.') from None


@contextmanager
def _registration_lock(path):
    with path.with_suffix('.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ProtocolError('Un enregistrement du protocole est déjà en cours.') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def register_protocol(path=None, *, now=None):
    """Explicit one-time registration. Existing criteria are never overwritten.

    ``now`` is a deterministic test clock and requires an isolated custom path.
    The real protocol always uses the current UTC clock at registration.
    """
    target = Path(path if path is not None else PATH)
    if now is not None and target.resolve() == Path(PATH).resolve():
        raise ProtocolError('La date du protocole réel provient uniquement de l’horloge UTC.')
    if now is not None and (not isinstance(now, datetime) or now.tzinfo is None):
        raise ProtocolError('La date d’enregistrement doit préciser son fuseau.')
    target.parent.mkdir(parents=True, exist_ok=True)
    with _registration_lock(target):
        existing = load_protocol(target)
        if existing is not None:
            return existing
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        value = dict(copy.deepcopy(CRITERIA), registered_at=current.isoformat())
        value['fingerprint'] = _fingerprint(value)
        validate_protocol(value)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=target.parent,
                                             prefix='.'+target.name+'.', suffix='.tmp', delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(_json(value)+'\n')
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        return value
