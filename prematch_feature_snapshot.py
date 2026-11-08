"""Pure, source-bound features shared by research and individual inference.

Only the existing scenario feature schema and fixed Clubs formula are used.
This module performs no I/O, training, model promotion or source reconstruction.
Callers must supply rows and the receipts selected by the explicit as-of reader.
Local import receipts are not provider-publication or commit-time proof.
"""
from copy import deepcopy
from hashlib import sha256
import json

from club_identity import fixture_identity
from fixture_temporal import utc_instant
from halftime_domain import CLASS_ORDER, validate_probabilities
import match_predictor as predictor
import scenario_features as features


VERSION = 'prematch-feature-snapshot-v1'
TIME_KIND = 'local_import_recorded'
_FIXTURE_REQUIRED = ('match_id', 'league', 'home_id', 'away_id', 'home', 'away', 'timestamp', 'provider')
_FIXTURE_OPTIONAL = ('season', 'competition', 'competition_code', 'country', 'status', 'raw_status', 'is_upcoming')
_HISTORY_REF = ('match_id', 'observation_id', 'observed_at_utc', 'time_kind', 'normalized_sha256')
_TARGET_REF = ('match_id', 'observation_id', 'observed_at_utc', 'time_kind', 'raw_sha256')


class SnapshotError(ValueError):
    """A feature snapshot cannot attest the supplied target/history contract."""


def encoded(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        raise SnapshotError('Le contenu doit être JSON fini et reproductible.') from None


def digest(value):
    return sha256(encoded(value).encode('utf-8')).hexdigest()


def _identifier(value):
    if (isinstance(value, bool) or not isinstance(value, (str, int))
            or not str(value).isascii() or not str(value).isdecimal() or int(value) <= 0):
        raise SnapshotError('Identifiant API-Football absent ou invalide.')
    return str(int(value))


def _hash(value):
    if (not isinstance(value, str) or len(value) != 64
            or any(character not in '0123456789abcdef' for character in value)):
        raise SnapshotError('Empreinte SHA256 absente ou invalide.')
    return value


def _instant(value):
    try:
        return utc_instant(value)
    except ValueError as error:
        raise SnapshotError(str(error)) from None


def _fixture(value, decision):
    if (not isinstance(value, dict) or not set(_FIXTURE_REQUIRED) <= set(value)
            or set(value) - set(_FIXTURE_REQUIRED + _FIXTURE_OPTIONAL)):
        raise SnapshotError('Une affiche normalisée, confirmée et sans score est nécessaire.')
    if value.get('provider') != 'api-football':
        raise SnapshotError('La source de l’affiche doit être API-Football.')
    result = {key: _identifier(value[key]) for key in ('match_id', 'league', 'home_id', 'away_id')}
    if result['home_id'] == result['away_id']:
        raise SnapshotError('Les deux clubs doivent être distincts.')
    for key in ('home', 'away'):
        if not isinstance(value[key], str) or not value[key].strip():
            raise SnapshotError('Les noms des deux clubs sont nécessaires.')
        result[key] = value[key].strip()
    try:
        kickoff = features._stamp(value['timestamp'])
    except ValueError as error:
        raise SnapshotError(str(error)) from None
    if kickoff <= decision:
        raise SnapshotError('La décision doit précéder le coup d’envoi du match.')
    if (value.get('raw_status', 'NS') != 'NS' or value.get('status', 'TIMED') not in ('NS', 'TIMED')
            or value.get('is_upcoming', True) is not True):
        raise SnapshotError('L’affiche doit être confirmée non commencée.')
    result.update(provider='api-football', timestamp=kickoff.replace(tzinfo=None).isoformat())
    for key in ('competition', 'country'):
        if key in value and value[key] is not None:
            if not isinstance(value[key], str) or not value[key].strip():
                raise SnapshotError('Les libellés de l’affiche doivent être du texte non vide.')
            result[key] = value[key].strip()
    if value.get('competition_code') is not None:
        if _identifier(value['competition_code']) != result['league']:
            raise SnapshotError('Les identifiants de compétition diffèrent.')
        result['competition_code'] = result['league']
    if value.get('season') is not None:
        season = value['season']
        if type(season) is not int or not 1900 <= season <= 2100:
            raise SnapshotError('Saison de l’affiche invalide.')
        result['season'] = season
    return result, kickoff


def _reference(value, fields, decision):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise SnapshotError('La référence source est incomplète ou comporte des champs inconnus.')
    observed = _instant(value['observed_at_utc'])
    if observed >= decision:
        raise SnapshotError('La référence source doit être enregistrée strictement avant la décision.')
    if value['time_kind'] != TIME_KIND:
        raise SnapshotError('La nature de l’heure source n’est pas attestée.')
    return dict(match_id=_identifier(value['match_id']), observation_id=_hash(value['observation_id']),
                observed_at_utc=observed.isoformat(), time_kind=TIME_KIND,
                **{fields[-1]: _hash(value[fields[-1]])})


def _history(rows, target, cutoff):
    if isinstance(rows, (str, bytes, dict)):
        raise SnapshotError('Une liste de matchs historiques est nécessaire.')
    try:
        rows = list(rows)
    except TypeError:
        raise SnapshotError('Une liste de matchs historiques est nécessaire.') from None
    selected = {}
    for row in rows:
        if not isinstance(row, dict):
            raise SnapshotError('Chaque historique doit être un match normalisé.')
        try:
            stamp = features._stamp(row.get('timestamp'))
        except ValueError as error:
            raise SnapshotError(str(error)) from None
        # Exclude every current-day/future score before reading its provider,
        # labels or malformed result fields. Such rows never enter the seal.
        if stamp.date().isoformat() >= cutoff:
            continue
        if row.get('provider') != 'api-football' or _identifier(row.get('league')) != target['league']:
            raise SnapshotError('L’historique doit conserver la source et la compétition de l’affiche.')
        mid = _identifier(row.get('match_id'))
        if mid == target['match_id']:
            raise SnapshotError('Le match cible ne peut pas appartenir à son historique.')
        home, away = _identifier(row.get('home_id')), _identifier(row.get('away_id'))
        if home == away:
            raise SnapshotError('Les clubs historiques doivent être distincts.')
        # Deep-copy only after the causal cutoff. This also excludes a future
        # non-JSON score from validation of a genuinely earlier snapshot.
        copied = deepcopy(row)
        encoded(copied)
        if mid in selected and selected[mid] != copied:
            raise SnapshotError('Identifiant historique contradictoire.')
        selected[mid] = copied
    return sorted(selected.values(), key=lambda row: (features._stamp(row['timestamp']), int(row['match_id'])))


def _history_references(values, rows, decision):
    if isinstance(values, (str, bytes, dict)):
        raise SnapshotError('Une liste de références historiques est nécessaire.')
    try:
        values = list(values)
    except TypeError:
        raise SnapshotError('Une liste de références historiques est nécessaire.') from None
    expected = {_identifier(row['match_id']): row for row in rows}
    references = {}
    for value in values:
        if not isinstance(value, dict):
            raise SnapshotError('Référence historique invalide.')
        mid = _identifier(value.get('match_id'))
        # Only references belonging to the history actually used can influence
        # an earlier snapshot. A future row/reference is not inspected or sealed.
        if mid not in expected:
            continue
        reference = _reference(value, _HISTORY_REF, decision)
        if reference['normalized_sha256'] != digest(expected[mid]):
            raise SnapshotError('La référence historique ne correspond pas aux valeurs utilisées.')
        if _instant(reference['observed_at_utc'])<features._stamp(expected[mid]['timestamp']):
            raise SnapshotError('Un résultat historique ne peut pas être observé avant son coup d’envoi.')
        if mid in references and references[mid] != reference:
            raise SnapshotError('Références historiques contradictoires.')
        references[mid] = reference
    if set(references) != set(expected):
        raise SnapshotError('Chaque match historique utilisé doit posséder sa référence source.')
    return [references[mid] for mid in sorted(references, key=int)]


def _target_observation(raw,target,reference):
    """Bind score-free target metadata to its saved provider observation."""
    try:
        if (not isinstance(raw,dict) or raw.get('provider') not in (None,'api-football','API-Football')
                or '_patternlab_import' in raw or digest(raw)!=reference['raw_sha256']
                or raw['fixture']['status']['short'].strip().upper()!='NS'):
            raise ValueError()
        expected=dict(match_id=raw['fixture']['id'],league=raw['league']['id'],
                      home_id=raw['teams']['home']['id'],away_id=raw['teams']['away']['id'])
        if any(_identifier(value)!=target[key] for key,value in expected.items()):
            raise ValueError()
        if _instant(raw['fixture']['date'])!=features._stamp(target['timestamp']):
            raise ValueError()
        for side in ('home','away'):
            if raw['teams'][side]['name'].strip()!=target[side]:
                raise ValueError()
        if 'season' in target and raw['league'].get('season')!=target['season']:
            raise ValueError()
        if 'competition' in target and raw['league'].get('name','').strip()!=target['competition']:
            raise ValueError()
        if 'country' in target and (raw['league'].get('country') or 'International').strip()!=target['country']:
            raise ValueError()
    except (ValueError,TypeError,KeyError,AttributeError):
        raise SnapshotError('L’observation cible ne correspond pas aux métadonnées figées.') from None
    return deepcopy(raw)


def build(history, fixture, *, decision_at, source_refs, target_ref, target_raw=None):
    """Build an immutable-by-content, score-free feature snapshot at decision T.

    Aware decision/observation times are mandatory. Fixture and normalized
    history naive timestamps follow the application's explicit UTC convention.
    Imported row bytes/fields are preserved for the receipt hash; model inputs
    receive semantically identical canonical UTC timestamps. Source references
    bind every retained row, without claiming provider-publication availability.
    """
    decision = _instant(decision_at)
    target, kickoff = _fixture(fixture, decision)
    cutoff = min(decision.date(), kickoff.date()).isoformat()
    target_source = _reference(target_ref, _TARGET_REF, decision)
    if target_source['match_id'] != target['match_id']:
        raise SnapshotError('La référence de l’affiche ne correspond pas au match cible.')
    target_observation=_target_observation(target_raw,target,target_source) if target_raw is not None else None
    rows = _history(history, target, cutoff)
    references = _history_references(source_refs, rows, decision)
    model_rows = [dict(row, timestamp=features._stamp(row['timestamp']).replace(tzinfo=None).isoformat())
                  for row in rows]
    try:
        vector = features.feature_for_fixture(model_rows, target, before=cutoff)
        estimate = predictor.estimate(model_rows, target['league'], target['home'], target['away'], cutoff,
                                      **fixture_identity(target))
    except (ValueError, TypeError, KeyError) as error:
        raise SnapshotError(str(error)) from None
    reference = dict(clubs=validate_probabilities(estimate['probabilities']),
                     frequency=validate_probabilities(estimate['baseline']), clubs_version=predictor.VERSION,
                     eligible=estimate['eligible'], personalized=estimate['personalized'],
                     training=estimate['training'], home_profile=estimate['home_profile'],
                     away_profile=estimate['away_profile'], expected_goals=estimate['expected_goals'])
    value = dict(version=VERSION, decision_at=decision.isoformat(), cutoff_exclusive=cutoff, fixture=target,
                 feature_version=features.FEATURE_VERSION,
                 feature_schema=dict(columns=list(features.FEATURE_COLUMNS),
                                     categorical_columns=list(features.CATEGORICAL_COLUMNS), class_order=list(CLASS_ORDER)),
                 features={column: vector[column] for column in features.FEATURE_COLUMNS}, references=reference,
                 history_rows=rows, history_fingerprint=digest(rows), source_refs=references, target_ref=target_source,
                 temporal_quality='retrospective_conditional', time_basis=TIME_KIND,
                 provider_publication_time_proven=False, database_visibility_at_cutoff_proven=False,
                 prospective_confirmation=False)
    if target_observation is not None:
        # The source envelope is evidence only; no target score is passed to
        # either feature generator or Clubs estimate.
        value['target_observation']=target_observation
    value['id'] = digest(value)
    return value


def validate(snapshot):
    """Verify the seal, then independently rebuild features and references.

    Rebuilding uses only the saved rows and receipts. No live source or model
    artifact is consulted. A resealed but changed vector is still invalid.
    """
    if not isinstance(snapshot, dict) or snapshot.get('version') != VERSION:
        raise SnapshotError('Version du snapshot de variables invalide.')
    value = {key: item for key, item in snapshot.items() if key != 'id'}
    if _hash(snapshot.get('id')) != digest(value):
        raise SnapshotError('Le snapshot de variables a été modifié.')
    try:
        rebuilt = build(snapshot['history_rows'], snapshot['fixture'], decision_at=snapshot['decision_at'],
                        source_refs=snapshot['source_refs'], target_ref=snapshot['target_ref'],
                        target_raw=snapshot.get('target_observation'))
    except (KeyError, TypeError, ValueError) as error:
        raise SnapshotError('Le snapshot ne peut pas être reproduit : ' + str(error)) from None
    if rebuilt != snapshot:
        raise SnapshotError('Les variables ou références ne reproduisent pas le snapshot conservé.')
    return snapshot
