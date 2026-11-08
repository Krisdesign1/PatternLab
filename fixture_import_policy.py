"""Pure import decisions and append-only observation envelopes.

Provider values remain separate from local quality annotations. Receipt time
is never presented as historical provider publication time.
"""
from copy import deepcopy
from hashlib import sha256
import json

VERSION = 'fixture-import-quality-v1'
QUALITY_KEY = '_patternlab_import'
FINISHED = {'FT', 'AET', 'PEN'}
ADMINISTRATIVE = {'CANC', 'ABD', 'AWD', 'WO'}
EXPLICIT_NONFINAL = {'PST', 'SUSP', 'INT'}
KNOWN = FINISHED | ADMINISTRATIVE | EXPLICIT_NONFINAL | {
    'TBD', 'NS', '1H', 'HT', '2H', 'ET', 'BT', 'P', 'LIVE'}
REASONS = {
    'incomplete_final': 'Relevé final incomplet ou incohérent reçu : les scores antérieurs sont conservés, mais la rencontre est en quarantaine.',
    'identity_changed': 'Les identifiants reçus diffèrent de la rencontre conservée : correction en quarantaine.',
    'final_after_administrative': 'Un résultat final contredit une annulation, un abandon ou un forfait conservé : correction en quarantaine.',
    'state_after_administrative': 'Un nouvel état contredit une annulation, un abandon ou un forfait conservé : correction en quarantaine.',
}


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return sha256(dumps(value).encode()).hexdigest()


def provider_raw(raw):
    return {key: deepcopy(value) for key, value in raw.items() if key != QUALITY_KEY}


def status(raw):
    state = raw.get('fixture', {}).get('status') if isinstance(raw.get('fixture'), dict) else None
    short = state.get('short') if isinstance(state, dict) else None
    if not isinstance(short, str):
        return None
    canonical = short.strip().upper()
    return canonical if canonical in KNOWN else short


def quarantine(raw):
    quality = raw.get(QUALITY_KEY)
    return quality if isinstance(quality, dict) and quality.get('quarantined') is True else None


def quality_error(raw):
    value = quarantine(raw)
    return ('Quarantaine import : ' + REASONS.get(value.get('reason'), 'Correction de données à examiner.')) if value else None


def identity(raw):
    try:
        if raw.get('provider') not in (None, 'api-football', 'API-Football'):
            return None
        values = (raw['fixture']['id'], raw['league']['id'], raw['teams']['home']['id'], raw['teams']['away']['id'])
        if any(isinstance(value, bool) or not str(value).isascii() or not str(value).isdecimal()
               or int(value) <= 0 for value in values):
            return None
        result = tuple(str(int(value)) for value in values)
        return result if result[2] != result[3] else None
    except (KeyError, TypeError, AttributeError):
        return None


def decide(previous, incoming, normalize, *, latest=None):
    """Return selected raw and decision; never fabricate or fill a score.

    A quarantine preserves the prior provider payload while blocking every
    normalization path. A later complete consistent final can clear a quality
    regression. A final contradicting an administrative decision needs review.
    """
    incoming = deepcopy(incoming)
    if QUALITY_KEY in incoming:
        # Loading/exporting an annotated fixture is not fresh confirmation.
        raise ValueError('Un relevé interne annoté ne constitue pas une nouvelle réponse fournisseur.')
    if previous is None:
        return incoming, 'received', None
    prior = provider_raw(previous)
    old, new = status(prior), status(incoming)
    review = (latest or {}).get('review') or {}
    if (not quarantine(previous) and isinstance(review, dict) and review.get('choice') == 'prior'
            and latest.get('decision') == 'reviewed'
            and digest(incoming) == latest.get('received_raw_sha256')
            and digest(prior) == latest.get('selected_raw_sha256')):
        # An identical response supplies no new evidence after explicit review.
        # The decision expires when a different payload is observed.
        return deepcopy(previous), 'review_retained', None

    def hold(reason):
        selected = deepcopy(prior)
        selected[QUALITY_KEY] = dict(version=VERSION, quarantined=True, reason=reason,
            previous_raw_sha256=digest(prior), received_raw_sha256=digest(incoming),
            received_status=new)
        return selected, 'quarantined', reason

    if identity(prior) is not None and identity(prior) != identity(incoming):
        return hold('identity_changed')
    if old in ADMINISTRATIVE and new in FINISHED:
        return hold('final_after_administrative')
    if old in ADMINISTRATIVE and new not in ADMINISTRATIVE:
        return hold('state_after_administrative')
    if old in FINISHED and new in FINISHED:
        try:
            normalize(prior)
            prior_complete = True
        except (ValueError, TypeError, KeyError):
            prior_complete = False
        try:
            normalize(incoming)
        except (ValueError, TypeError, KeyError):
            if prior_complete:
                return hold('incomplete_final')
    if old in FINISHED and new not in FINISHED | ADMINISTRATIVE | EXPLICIT_NONFINAL:
        return deepcopy(previous), 'retained_final', 'nonfinal_regression'
    if new in ADMINISTRATIVE | EXPLICIT_NONFINAL and old in FINISHED:
        return incoming, 'status_correction', None
    return incoming, 'received', None


def observation(previous, incoming, selected, decision, reason, observed_at, last=None, *, review=None):
    """Deduplicate consecutive observations; keep A→B→A→B transitions."""
    received = provider_raw(incoming)
    quality = deepcopy(selected.get(QUALITY_KEY))
    signature = dict(received_raw_sha256=digest(received),
                     selected_raw_sha256=digest(provider_raw(selected)), selected_quality=quality)
    if last and all(last.get(key) == value for key, value in signature.items()) and review is None:
        return None
    record = dict(version=VERSION, match_id=str(int(incoming['fixture']['id'])),
        observed_at_utc=observed_at, time_kind='local_import_recorded',
        decision=decision, reason=reason, received_raw=received,
        previous_observation=last['id'] if last else None, **signature)
    if last is None and previous is not None:
        baseline = provider_raw(previous)
        record.update(baseline_raw_sha256=digest(baseline),
                      baseline_time_kind='recovered_at_first_tracked_import')
        if baseline != received:
            record['baseline_raw'] = baseline
        else:
            record['baseline_equals_received'] = True
    if review is not None:
        record['review'] = review
    record['id'] = digest(record)
    return record


def validate_record(record):
    if (not isinstance(record, dict) or record.get('version') != VERSION
            or record.get('id') != digest({k: v for k, v in record.items() if k != 'id'})
            or digest(record.get('received_raw')) != record.get('received_raw_sha256')):
        raise ValueError('Historique des imports incohérent.')
    return record


def review_selection(current, latest, *, choice, expected_observation, reason, normalize, observed_at):
    """Explicit local adjudication, guarded against concurrent/new observations."""
    if (not quarantine(current) or not latest or latest['id'] != expected_observation
            or latest.get('selected_raw_sha256') != digest(provider_raw(current))
            or latest.get('selected_quality') != current.get(QUALITY_KEY)):
        raise ValueError('La correction a changé ou n’est plus en quarantaine ; relisez ses observations.')
    if choice not in ('received', 'prior') or not isinstance(reason, str) or not 3 <= len(reason.strip()) <= 500:
        raise ValueError('Choisissez received ou prior et indiquez un motif de revue de 3 à 500 caractères.')
    selected = provider_raw(latest['received_raw'] if choice == 'received' else current)
    if identity(selected) is None:
        raise ValueError('La rencontre choisie doit conserver des identifiants valides.')
    if status(selected) in FINISHED:
        normalize(selected)  # A review can never manufacture missing scores.
    elif status(selected) not in ADMINISTRATIVE | EXPLICIT_NONFINAL:
        raise ValueError('La revue attend un résultat complet ou un état administratif explicite.')
    event = observation(current, latest['received_raw'], selected, 'reviewed', None, observed_at,
                        latest, review=dict(choice=choice, reason=reason.strip()))
    return selected, event
