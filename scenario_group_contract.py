"""Pure checks of ordered group forecasts, including legacy provenance gaps.

This contract checks recorded claims and absolute model mass. It does not
attest historical provider visibility or the physical time of a database
COMMIT. An old record with missing bindings stays unverified, not repaired.
"""
from datetime import date, datetime, timezone
from hashlib import sha256
import json

import group_probability_diagnostics as group_diagnostics
import prospective_models as models


VERSION = 'scenario-group-contract-v1'
FORBIDDEN = {'value', 'ht', 'ft', 'score', 'goals', 'elapsed', 'minute'}


def _stamp(value, *, naive=False):
    if not isinstance(value, str) or 'T' not in value:
        raise ValueError('Une date ISO explicite est nécessaire.')
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        if not naive:
            raise ValueError('Le fuseau de la date doit être conservé.')
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError('La coupure doit être une journée ISO complète.')
    return value


def _id(value):
    if (isinstance(value, bool) or not isinstance(value, (str, int))
            or not str(value).isascii() or not str(value).isdecimal() or int(value) <= 0):
        raise ValueError('Un identifiant numérique API-Football est nécessaire.')
    return str(int(value))


def _digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False)
    return sha256(encoded.encode('utf-8')).hexdigest()


def inspect(entry):
    """Return checks and bounded mass summaries from ONE saved group only.

    ``invalid`` means a contradictory or malformed recorded claim.
    ``unverified`` means required evidence was never stored, notably in legacy
    records. Neither state changes forecasts, sporting verdicts or results.
    """
    issues, summaries, cutoffs, decisions = [], [], set(), set()

    def issue(code, message, *, method=None, severity='invalid'):
        row = dict(code=code, severity=severity, message=message)
        if method is not None:
            row['method'] = method
        issues.append(row)

    if not isinstance(entry, dict):
        return dict(version=VERSION, entry_id=None, size=None, status='invalid',
                    issues=[dict(code='entry_invalid', severity='invalid',
                                 message='Une prévision de groupe enregistrée est nécessaire.')],
                    methods=[], common_cutoff=None, decision_at=None, input_sha256=None)
    projection = {key: entry.get(key) for key in ('id', 'source', 'size', 'created_at', 'fixtures', 'methods')}
    try:
        input_hash = _digest(projection)
    except (ValueError, TypeError, OverflowError, RecursionError):
        input_hash = None
        issue('input_not_finite_json', 'Le contenu de la prévision n’est pas un JSON fini reproductible.')
    if entry.get('source', models.SOURCE) != models.SOURCE:
        issue('source_invalid', 'La source du groupe doit être API-Football.')
    size = entry.get('size')
    try:
        models.validate_size(size)
    except ValueError:
        issue('size_invalid', 'Un groupe doit contenir de 2 à 20 matchs.')
    created = None
    try:
        created = _stamp(entry.get('created_at'))
    except (ValueError, TypeError, OverflowError):
        issue('created_at_unverified', 'L’heure d’enregistrement avec son fuseau est absente ou invalide.',
              severity='unverified')
    fixtures, stamps, normalized = entry.get('fixtures'), [], None
    if not isinstance(fixtures, list) or not fixtures or type(size) is not int or len(fixtures) != size:
        issue('fixtures_size_invalid', 'Les affiches conservées ne correspondent pas à la taille du groupe.')
    else:
        try:
            identities = []
            for row in fixtures:
                if not isinstance(row, dict) or FORBIDDEN.intersection(row):
                    raise ValueError('Les affiches avant match doivent être dépourvues de scores.')
                if row.get('provider') != models.PROVIDER:
                    raise ValueError('Chaque affiche doit conserver explicitement la source API-Football.')
                for key in ('match_id', 'league', 'home_id', 'away_id'):
                    _id(row.get(key))
                identities.append(_id(row['match_id']))
                stamps.append(_stamp(row.get('timestamp'), naive=True))
            if len(set(identities)) != len(identities):
                raise ValueError('Le même identifiant de match apparaît plusieurs fois.')
            normalized, _, _ = models._fixtures(fixtures, models.SOURCE, allow_mixed=True)
            if fixtures != sorted(fixtures, key=lambda row: (_stamp(row['timestamp'], naive=True), int(row['match_id']))):
                raise ValueError('Les matchs doivent conserver l’ordre chronologique et les identifiants numériques.')
            if created is not None and any(stamp <= created for stamp in stamps):
                issue('created_after_kickoff', 'L’heure enregistrée ne précède pas tous les coups d’envoi.')
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as error:
            normalized = None
            issue('fixtures_invalid', str(error))

    def check_method(name, payload, targets, *, component=False, common=None, parent_decision=None):
        if not isinstance(payload, dict):
            issue('method_invalid', 'La méthode figée est absente ou invalide.', method=name)
            return
        try:
            models._validate_method(payload, len(targets), _component=component)
        except (ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError):
            issue('distribution_invalid', 'La distribution complète ou son classement est incohérent.', method=name)
            return
        if payload.get('source') != models.SOURCE:
            issue('method_source_invalid', 'La source de la méthode ne correspond pas au groupe.', method=name)
        cutoff = None
        try:
            cutoff = _day(payload.get('training_before'))
            if not component:
                cutoffs.add(cutoff)
            bound = min([row['timestamp'][:10] for row in targets]
                        + ([created.date().isoformat()] if created is not None else []))
            if cutoff > bound:
                issue('cutoff_after_decision_or_match', 'La coupure historique dépasse la journée d’enregistrement ou un coup d’envoi.', method=name)
            if common is not None and cutoff != common:
                issue('component_cutoff_mismatch', 'Le composant n’utilise pas la coupure commune du groupe.', method=name)
        except (ValueError, TypeError, KeyError):
            issue('cutoff_invalid', 'La coupure historique de la méthode est absente ou invalide.', method=name)
        decision = None
        if payload.get('decision_at') is None:
            issue('decision_unverified', 'L’heure de décision n’a pas été conservée pour cette méthode.',
                  method=name, severity='unverified')
        else:
            try:
                decision = _stamp(payload['decision_at'])
                if not component:
                    decisions.add(decision.isoformat())
                elif parent_decision is not None and decision != parent_decision:
                    issue('component_decision_mismatch', 'Le composant n’utilise pas l’heure de décision du groupe.', method=name)
                if ((created is not None and decision > created)
                        or any(decision >= _stamp(row['timestamp'], naive=True) for row in targets)):
                    raise ValueError()
                if cutoff is not None and cutoff > decision.date().isoformat():
                    issue('cutoff_after_declared_decision', 'La coupure dépasse la journée de décision déclarée.', method=name)
            except (ValueError, TypeError, OverflowError):
                issue('decision_invalid', 'La décision doit être horodatée, antérieure à l’enregistrement et à tous les matchs.', method=name)
        expected_ids = [row['match_id'] for row in targets]
        binding = payload.get('ordered_match_ids')
        if binding is None:
            issue('binding_unverified', 'Les identifiants ordonnés des probabilités n’ont pas été conservés.',
                  method=name, severity='unverified')
        elif binding != expected_ids:
            issue('ordered_match_ids_mismatch', 'Les probabilités ne sont pas liées aux mêmes matchs dans le même ordre.', method=name)
        fingerprint = payload.get('fixture_fingerprint')
        if fingerprint is None:
            issue('fingerprint_unverified', 'L’empreinte des affiches n’a pas été conservée.',
                  method=name, severity='unverified')
        elif fingerprint != models._context(targets, cutoff or targets[0]['timestamp'][:10])['fixture_fingerprint']:
            if binding is None:
                # Older context engines used this same key for a different
                # internal fingerprint convention. Without ordered IDs its
                # scope is unknown, not proof of a contradictory group.
                issue('fingerprint_scope_unverified', 'La portée de cette ancienne empreinte n’est pas attestée par les identifiants ordonnés.',
                      method=name, severity='unverified')
            else:
                issue('fixture_fingerprint_mismatch', 'L’empreinte des affiches diffère de celle du groupe conservé.', method=name)
        trained = payload.get('trained_through')
        if trained is not None:
            try:
                if cutoff is None or _day(trained) >= cutoff:
                    raise ValueError()
            except (ValueError, TypeError):
                issue('training_period_invalid', 'La période apprise par le modèle empiète sur sa coupure historique.', method=name)
        for field, inclusive in (('artifact_cutoff', True), ('selection_through', False)):
            if payload.get(field) is not None:
                try:
                    limit = _day(payload[field])
                    if cutoff is None or limit > cutoff or (not inclusive and limit == cutoff):
                        raise ValueError()
                except (ValueError, TypeError):
                    issue('artifact_period_invalid', 'La sélection ou la période de l’artefact empiète sur la coupure du groupe.', method=name)
        distribution = payload['distribution']
        if distribution['type'] == 'block_product':
            if component:
                issue('nested_components_invalid', 'Les produits de blocs ne peuvent pas être imbriqués.', method=name)
                return
            if (payload.get('league') != 'mixed' or payload.get('provider') != models.PROVIDER
                    or payload.get('assumes_block_independence') is not True):
                issue('mixed_provenance_invalid', 'Le groupe mixte doit expliciter ses compétitions et son hypothèse d’indépendance.', method=name)
            leagues = set()
            for block in distribution['components']:
                selected = [targets[index] for index in block['indices']]
                league = str(block.get('league'))
                if league in leagues or any(row['league'] != league for row in selected):
                    issue('component_league_mismatch', 'Les positions du composant ne correspondent pas à sa compétition.', method=name)
                leagues.add(league)
                check_method(name+' · '+league, block['method'], selected, component=True, common=cutoff,
                             parent_decision=decision)
            if sorted(leagues, key=str) != sorted({row['league'] for row in targets}, key=str):
                issue('component_leagues_incomplete', 'Les composants ne couvrent pas exactement les compétitions du groupe.', method=name)
        elif any(str(payload.get('league')) != row['league'] for row in targets):
            issue('method_league_mismatch', 'La compétition de la méthode diffère de celle des affiches.', method=name)

    methods = entry.get('methods')
    if (not isinstance(methods, dict) or not methods or len(methods) > len(models.METHODS)
            or set(methods)-set(models.METHODS)):
        issue('methods_invalid', 'Une ou plusieurs méthodes nommées compatibles sont nécessaires.')
    elif normalized is not None:
        for name in models.METHODS:
            if name not in methods:
                continue
            payload = methods[name]
            check_method(name, payload, normalized)
            try:
                value = group_diagnostics.inspect(payload)
                summaries.append({key:value[key] for key in ('top1_probability', 'top3_probability',
                                  'remaining_probability', 'assumptions')} | dict(name=name, version=payload['version']))
            except (ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError):
                pass  # The explicit method check above owns the failure label.
    if len(cutoffs) > 1:
        issue('common_cutoff_mismatch', 'Les méthodes utilisent des coupures historiques différentes.')
    if len(decisions) > 1:
        issue('common_decision_mismatch', 'Les méthodes utilisent des heures de décision différentes.')
    status = 'invalid' if any(row['severity'] == 'invalid' for row in issues) else 'unverified' if issues else 'valid'
    identity = entry.get('id')
    safe_identity = identity if type(identity) in (str, int) else None
    safe_size = size if type(size) is int and 2 <= size <= 20 else None
    return dict(version=VERSION, entry_id=safe_identity, size=safe_size, status=status, issues=issues,
                common_cutoff=next(iter(cutoffs)) if len(cutoffs) == 1 else None,
                decision_at=next(iter(decisions)) if len(decisions) == 1 else None,
                methods=summaries, input_sha256=input_hash,
                interpretation='Contrôle de cohérence du contenu enregistré ; aucune attestation de visibilité fournisseur ni de COMMIT avant match.')


def require_recordable(fixtures, methods, created_at):
    """Reject contradictory new claims without inventing missing legacy proof."""
    value = inspect(dict(source=models.SOURCE, size=len(fixtures), fixtures=fixtures,
                         methods=methods, created_at=created_at))
    if value['status'] == 'invalid':
        raise ValueError(' · '.join(row['message'] for row in value['issues'] if row['severity'] == 'invalid'))
    return value
