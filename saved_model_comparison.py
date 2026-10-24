"""Paired descriptive match scores from immutable prospective distributions.

No model, provider, clock, result reconciliation or file is accessed. Every
method is scored on the same first saved forecast for a match in one exact
competition / group-size / version cohort. The registered journal protocol
continues to govern the separate group-level uncertainty analysis.
"""
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
import json
from math import fsum, isclose, log, prod

from league_model import VALUES
import journal_protocol
import probability_reliability as saved
import prospective_models as models
import journal_uncertainty as uncertainty


VERSION = 'saved-paired-match-comparison-v1'
METHODS = ('frequency', 'clubs', 'catboost')
PAIRED_VERSION = 'saved-frequency-pairs-v1'
CHALLENGERS = ('clubs', 'combined', 'catboost', 'context_neighbors')


def _marginals(model):
    size, kind = model['size'], model['type']
    if kind == 'block_product':
        result = [None] * size
        for block in model['blocks']:
            for index, probabilities in zip(block['indices'], _marginals(block['model'])):
                result[index] = probabilities
        return result
    if kind == 'frequency':
        counts, n, prior = model['fit']
        weight = model['weight']
        return [{label: (sum(count for sequence, count in counts.items() if sequence[index] == label)
                         + weight * prior[index][label]) / (n + weight)
                 for label in VALUES} for index in range(size)]
    base = model['distributions']
    if kind == 'independent':
        return [dict(probabilities) for probabilities in base]
    if kind == 'context_mixture':
        return [{label: (1-model['eta']) * base[index][label]
                        + model['eta'] * fsum(mass for sequence, mass in model['local'].items()
                                              if sequence[index] == label)
                 for label in VALUES} for index in range(size)]
    # The normalized corrected joint distribution is a constant multiple of
    # the independent baseline plus finite adjustments on observed scenarios.
    adjustments = [(sequence, prod(p[label] for p, label in zip(base, sequence))
                     * (factor-model['unseen']))
                   for sequence, factor in model['corrections'].items()]
    return [{label: (model['unseen'] * base[index][label]
                     + fsum(mass for sequence, mass in adjustments if sequence[index] == label))
                    / model['normalization']
             for label in VALUES} for index in range(size)]


def marginals(method):
    """Exact marginals for all supported saved joints, without enumerating 3**N."""
    if not isinstance(method, dict) or not isinstance(method.get('distribution'), dict):
        raise ValueError('Distribution enregistrée absente.')
    size = method['distribution'].get('size')
    models.validate_method(method, size)
    result = _marginals(models._read_distribution(method['distribution']))
    for probabilities in result:
        if (set(probabilities) != set(VALUES)
                or any(not 0 <= value <= 1 for value in probabilities.values())
                or not isclose(fsum(probabilities.values()), 1, abs_tol=1e-9)):
            raise ValueError('Marginales enregistrées invalides.')
    return result


def _method_identity(method):
    version, generation = method.get('version'), method.get('generation', '')
    if not isinstance(version, str) or not version or not isinstance(generation, str):
        raise ValueError('Version de méthode absente.')
    lineage = []
    if method.get('distribution', {}).get('type') == 'block_product':
        lineage = [(block['indices'], str(block.get('league')), _method_identity(block['method']))
                   for block in method['distribution']['components']]
    return version, generation, json.dumps(lineage,separators=(',',':')) if lineage else ''


def _causal(method, fixtures, *, component=False):
    if method.get('source') != 'API-Football':
        raise ValueError('Source de méthode incompatible.')
    cutoff = method.get('training_before')
    if not isinstance(cutoff, str) or date.fromisoformat(cutoff).isoformat() != cutoff:
        raise ValueError('Coupure historique absente.')
    if any(cutoff > saved._stamp(row['timestamp']).date().isoformat() for row in fixtures):
        raise ValueError('Coupure historique postérieure au match.')
    trained = method.get('trained_through')
    if trained is not None and date.fromisoformat(trained).isoformat() >= cutoff:
        raise ValueError('Le modèle a appris sur la période cible.')
    if method['distribution']['type'] == 'block_product':
        fields = ('match_id','league','home_id','away_id','timestamp','provider')
        saved_fixtures = method.get('fixtures')
        if (component or method.get('league') != 'mixed' or method.get('provider') != 'api-football'
                or method.get('assumes_block_independence') is not True
                or not isinstance(saved_fixtures,list) or len(saved_fixtures) != len(fixtures)
                or any(any(str(original.get(field)) != str(target.get(field)) for field in fields)
                       for original,target in zip(saved_fixtures,fixtures))):
            raise ValueError('Provenance mixte invalide.')
        for block in method['distribution']['components']:
            selected = [fixtures[index] for index in block['indices']]
            _causal(block['method'], selected, component=True)
            if block['method'].get('training_before') != cutoff:
                raise ValueError('Coupures mixtes différentes.')
    elif str(method.get('league')) != fixtures[0]['league'] or any(
            row['league'] != fixtures[0]['league'] for row in fixtures):
        raise ValueError('Compétition de méthode incompatible.')


def _scores(records, method):
    n = len(records)
    if not n:
        return dict(evaluated=0, accuracy=None, logloss=None, brier=None, categories=[])
    probabilities = [row['methods'][method] for row in records]
    categories = []
    for label in VALUES:
        nominated = [(row, p) for row, p in zip(records, probabilities) if max(VALUES, key=p.get) == label]
        categories.append(dict(label=label, actual_count=sum(row['actual'] == label for row in records),
            predicted_count=len(nominated), predicted_hits=sum(row['actual'] == label for row, _ in nominated),
            mean_probability=fsum(p[label] for p in probabilities)/n,
            observed_rate=sum(row['actual'] == label for row in records)/n,
            brier=fsum((p[label]-int(row['actual'] == label))**2
                       for row, p in zip(records, probabilities))/n))
    return dict(evaluated=n, accuracy=sum(max(VALUES, key=p.get) == row['actual']
                for row, p in zip(records, probabilities))/n,
        logloss=fsum(-log(max(p[row['actual']], 1e-15)) for row, p in zip(records, probabilities))/n,
        brier=fsum(sum((p[label]-int(row['actual'] == label))**2 for label in VALUES)
                  for row, p in zip(records, probabilities))/n,
        categories=categories)


def build(entries, protocol):
    """Compare all three methods on common saved matches after registration.

    Missing CatBoost forecasts never become synthetic predictions or failures.
    Each cohort retains the earliest common snapshot even if it is still pending.
    Cohorts and sizes are never pooled into a claim of predictive superiority.
    """
    protocol = journal_protocol.validate_protocol(protocol)
    registered = saved._stamp(protocol['registered_at'])
    excluded, candidates = Counter(), []
    entries = list(entries)
    for entry in entries:
        try:
            if not isinstance(entry, dict) or entry.get('source') != 'API-Football':
                raise ValueError('Source du groupe invalide.')
            created = saved._stamp(entry['created_at'])
            if created < registered:
                excluded['before_protocol'] += 1
                continue
            if not isinstance(entry.get('id'), str) or not entry['id']:
                raise ValueError('Identité du groupe invalide.')
            fixtures = []
            for value in entry['fixtures']:
                row = dict(value)
                if row.get('provider') != 'api-football':
                    raise ValueError('Source du match invalide.')
                for field in ('match_id', 'league', 'home_id', 'away_id'):
                    row[field] = saved._id(row.get(field))
                if row['home_id'] == row['away_id'] or created >= saved._stamp(row['timestamp']):
                    raise ValueError('Prévision non antérieure au coup d’envoi.')
                fixtures.append(row)
            size = len(fixtures)
            if (size not in protocol['sizes'] or entry.get('size') != size
                    or len({row['match_id'] for row in fixtures}) != size):
                raise ValueError('Groupe invalide.')
            payloads = entry.get('methods', {})
            missing = [method for method in METHODS if method not in payloads]
            if missing:
                for method in missing:
                    excluded['missing_'+method] += 1
                continue
            identities = tuple(_method_identity(payloads[method]) for method in METHODS)
            positions = {}
            for method in METHODS:
                _causal(payloads[method], fixtures)
                positions[method] = marginals(payloads[method])
                if len(positions[method]) != size:
                    raise ValueError('Taille des probabilités incompatible.')
            for index, fixture in enumerate(fixtures):
                cohort = (fixture['league'], size, identities)
                candidates.append(dict(cohort=cohort, match_id=fixture['match_id'],
                    created=created, entry_id=entry['id'], fixture=fixture, _entry=entry,
                    methods={method: positions[method][index] for method in METHODS}))
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError, IndexError):
            excluded['invalid_snapshot'] += 1
    first = {}
    for row in sorted(candidates, key=lambda item: (item['created'], item['entry_id'])):
        first.setdefault((row['cohort'], row['match_id']), row)
    grouped = defaultdict(list)
    for row in first.values():
        selected = dict(row)
        selected.update(saved._result(selected.pop('_entry'), selected['fixture']))
        grouped[row['cohort']].append(selected)
    cohorts = []
    for (league, size, identities), records in sorted(grouped.items()):
        evaluated = [row for row in records if row['state'] == 'evaluated']
        scores = {method: _scores(evaluated, method) for method in METHODS}
        cohorts.append(dict(league=league, size=size,
            versions={method: dict(version=version, generation=generation, components=lineage)
                      for method, (version, generation, lineage) in zip(METHODS, identities)},
            forecasts=len(records), evaluated=len(evaluated),
            pending=sum(row['state'] == 'pending' for row in records),
            excluded=sum(row['state'] == 'excluded' for row in records), scores=scores,
            match_ids=sorted(row['match_id'] for row in records),
            loss_gain_vs_clubs={method: (scores['clubs']['logloss']-scores[method]['logloss']
                if evaluated else None) for method in METHODS if method != 'clubs'}))
    return dict(version=VERSION, protocol_version=protocol['version'],
        registered_at=protocol['registered_at'], entries_read=len(entries), cohorts=cohorts,
        duplicates_removed=len(candidates)-len(first), snapshot_exclusions=dict(excluded),
        interpretation='Descriptive common-match scores only; no automatic promotion or retraining.')


def _gain(record, challenger):
    """Positive values always favor the challenger; identical paired matches."""
    actual = record['actual']
    baseline, predicted = record['methods']['frequency'], record['methods'][challenger]
    loss = lambda p: -log(max(p[actual], 1e-15))
    brier = lambda p: fsum((p[label]-int(actual == label))**2 for label in VALUES)
    return (loss(baseline)-loss(predicted), brier(baseline)-brier(predicted),
            int(max(VALUES,key=predicted.get) == actual)-int(max(VALUES,key=baseline.get) == actual))


def _paired_uncertainty(records, challenger, protocol, current):
    """Conservative closed-week clusters; this is not a new registered test.

    Keep dependency components from all original weeks and fixtures, including
    pending observations, before choosing mature components. No per-match IID
    interval, p-value, model promotion or pooling across cohorts is produced.
    """
    components = uncertainty._components(records)
    blocks, immature = [], Counter()
    current_week = uncertainty._week(current)
    for component in components:
        if any(week >= current_week for row in component for week in row['weeks']):
            immature['open_week'] += len(component)
        elif any(row['state'] != 'evaluated' for row in component):
            immature['incomplete_block'] += len(component)
        else:
            gains = [_gain(row, challenger) for row in component]
            blocks.append((len(gains), *(fsum(gain[i] for gain in gains) for i in range(3))))
    count = sum(block[0] for block in blocks)
    outputs = {name:dict(estimate=fsum(block[index] for block in blocks)/count if count else None,
                        lower=None,upper=None)
               for index,name in enumerate(('logloss','brier','accuracy'),1)}
    status = 'insufficient'
    reason = (f"{len(blocks)} bloc(s) clos sur le minimum opérationnel de {protocol['min_blocks']}. "
              'Ce minimum ne garantit pas une puissance suffisante.')
    if len(blocks) >= protocol['min_blocks']:
        # Fixed protocol seed/coverage/draw count, shared draws for paired metrics.
        import random
        generator = random.Random(protocol['seed'])
        draws = [[],[],[]]
        for _ in range(protocol['bootstrap_samples']):
            chosen = generator.choices(blocks,k=len(blocks))
            n = sum(block[0] for block in chosen)
            for index,values in enumerate(draws,1):
                values.append(fsum(block[index] for block in chosen)/n)
        tail = (1-protocol['confidence'])/2
        for index,(name,values) in enumerate(zip(outputs,draws),1):
            means = [block[index]/block[0] for block in blocks]
            ordered = sorted(values)
            lower,upper = uncertainty._percentile(ordered,tail),uncertainty._percentile(ordered,1-tail)
            if (not isclose(min(means),max(means),abs_tol=1e-12,rel_tol=1e-12)
                    and not isclose(lower,upper,abs_tol=1e-12,rel_tol=1e-12)):
                outputs[name].update(lower=lower,upper=upper)
        status = 'descriptive'
        reason = ('Intervalles descriptifs par blocs hebdomadaires; dépendances résiduelles possibles. '
                  'Consultations répétées et comparaisons multiples non corrigées; aucun avantage confirmé.')
        if outputs['logloss']['lower'] is None or outputs['brier']['lower'] is None:
            reason += ' Un indicateur sans variation ne permet pas d’estimer sa précision.'
    return dict(status=status,reason=reason,blocks=len(blocks),matches=count,
                confidence=protocol['confidence'],gains=outputs,excluded=dict(immature))


def _pair_fixtures(entry, registered, current):
    if not isinstance(entry,dict) or entry.get('source') != 'API-Football':
        raise ValueError('Source invalide.')
    created = saved._stamp(entry['created_at'])
    if created < registered:
        return None
    if created > current or not isinstance(entry.get('id'),str) or not entry['id']:
        raise ValueError('Enregistrement invalide ou futur.')
    fixtures = []
    for fixture in entry['fixtures']:
        row = dict(fixture)
        if row.get('provider') != 'api-football':
            raise ValueError('Source du match invalide.')
        for field in ('match_id','league','home_id','away_id'):
            row[field] = saved._id(row.get(field))
        if row['home_id'] == row['away_id'] or created >= saved._stamp(row['timestamp']):
            raise ValueError('Prévision après le début du groupe.')
        fixtures.append(row)
    if entry.get('size') != len(fixtures) or len({row['match_id'] for row in fixtures}) != len(fixtures):
        raise ValueError('Taille du groupe invalide.')
    return created,fixtures


def _pair_method(payload, fixtures, created):
    _causal(payload,fixtures)
    # A future historical cutoff cannot prove that only available days were used.
    if payload['training_before'] > created.date().isoformat():
        raise ValueError('Coupure non vérifiable à la date d’enregistrement.')
    if payload.get('trained_through') is not None and date.fromisoformat(
            payload['trained_through']) >= created.date():
        raise ValueError('Fin d’apprentissage non antérieure au jour de prévision.')
    identity, positions = _method_identity(payload), marginals(payload)
    if len(positions) != len(fixtures):
        raise ValueError('Marginales incomplètes.')
    return identity,positions


def _frequency_reference(payload):
    distribution = payload['distribution']
    if distribution['type'] == 'block_product':
        return all(_frequency_reference(block['method']) for block in distribution['components'])
    return distribution['type'] == 'frequency'


def build_pairs(entries, protocol, *, now=None):
    """Supplementary prospective pairs versus the saved no-club frequency method.

    The registered protocol remains unchanged (Clubs baseline, joint logloss and
    top3). Its registration date and cluster settings are reused transparently;
    these additional per-match comparisons are descriptive, not preregistered
    superiority tests. Missing methods never force another pair out of the study.
    """
    protocol = journal_protocol.validate_protocol(protocol)
    current = now if now is not None else datetime.now(timezone.utc)
    if not isinstance(current,datetime) or current.tzinfo is None:
        raise ValueError('L’heure du bilan doit préciser son fuseau.')
    current = current.astimezone(timezone.utc)
    registered = saved._stamp(protocol['registered_at'])
    entries,excluded,candidates = list(entries),Counter(),[]
    for entry in entries:
        try:
            validated = _pair_fixtures(entry,registered,current)
            if validated is None:
                excluded['before_protocol'] += 1
                continue
            created,fixtures = validated
            if len(fixtures) not in protocol['sizes']:
                raise ValueError('Taille de groupe incompatible.')
            payloads = entry.get('methods',{})
            if not isinstance(payloads,dict) or 'frequency' not in payloads:
                excluded['missing_frequency'] += 1
                continue
            baseline_identity,baseline = _pair_method(payloads['frequency'],fixtures,created)
            if not _frequency_reference(payloads['frequency']):
                raise ValueError('La référence ne contient pas les répétitions figées.')
            # Dependencies are based on the entire original group, not only its
            # retained match, so spanning groups cannot break a week bridge.
            dependencies = uncertainty._metadata(entry,created)
        except (ValueError,KeyError,TypeError,AttributeError,OverflowError,IndexError):
            excluded['invalid_snapshot'] += 1
            continue
        for challenger in CHALLENGERS:
            if challenger not in payloads:
                excluded['missing_'+challenger] += 1
                continue
            try:
                identity,positions = _pair_method(payloads[challenger],fixtures,created)
                if payloads[challenger]['training_before'] != payloads['frequency']['training_before']:
                    raise ValueError('Coupures historiques différentes.')
                for index,fixture in enumerate(fixtures):
                    candidates.append(dict(cohort=(challenger,fixture['league'],len(fixtures),
                        baseline_identity,identity),match_id=fixture['match_id'],created=created,
                        entry_id=entry['id'],fixture=fixture,_entry=entry,
                        ids=dependencies['ids'],weeks=dependencies['weeks'],
                        methods={'frequency':baseline[index],challenger:positions[index]}))
            except (ValueError,KeyError,TypeError,AttributeError,OverflowError,IndexError):
                excluded['invalid_'+challenger] += 1
    first = {}
    for row in sorted(candidates,key=lambda item:(item['created'],item['entry_id'])):
        key = (row['cohort'],row['match_id'])
        if key not in first:
            first[key] = row
        else:
            # A repeated forecast may span another original block. Preserve its
            # dependencies even though its probabilities/results are not used.
            first[key]['ids'] = first[key]['ids'] | row['ids']
            first[key]['weeks'] = sorted(set(first[key]['weeks']) | set(row['weeks']))
    grouped = defaultdict(list)
    for row in first.values():
        record = dict(row)
        record.update(saved._result(record.pop('_entry'),record['fixture']))
        if record['state'] == 'evaluated' and saved._stamp(row['_entry']['checked_at']) > current:
            record.update(state='excluded',reason='observation_after_bilan')
        grouped[record['cohort']].append(record)
    cohorts = []
    for (challenger,league,size,baseline_identity,identity),records in sorted(grouped.items()):
        evaluated = [row for row in records if row['state'] == 'evaluated']
        names = ('frequency',challenger)
        scores = {method:_scores(evaluated,method) for method in names}
        calibration = {method:{label:saved._bins([dict(row,probabilities=row['methods'][method])
                         for row in records],label) for label in VALUES} for method in names}
        cohorts.append(dict(challenger=challenger,league=league,size=size,
            versions={name:dict(version=value[0],generation=value[1],components=value[2])
                      for name,value in zip(names,(baseline_identity,identity))},
            forecasts=len(records),evaluated=len(evaluated),
            pending=sum(row['state']=='pending' for row in records),
            excluded=sum(row['state']=='excluded' for row in records),
            dates=len({saved._stamp(row['fixture']['timestamp']).date() for row in evaluated}),
            match_ids=sorted(row['match_id'] for row in records),scores=scores,calibration=calibration,
            uncertainty=_paired_uncertainty(records,challenger,protocol,current)))
    return dict(version=PAIRED_VERSION,protocol_version=protocol['version'],
        registered_at=protocol['registered_at'],as_of=current.isoformat(),
        entries_read=len(entries),cohorts=cohorts,duplicates_removed=len(candidates)-len(first),
        snapshot_exclusions=dict(excluded),
        interpretation='Supplementary descriptive pairs; original Clubs/top3 protocol is unchanged.')
