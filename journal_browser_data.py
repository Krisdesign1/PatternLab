"""Pure dated journal rows from immutable forecasts and saved result states.

No history, current API status or model is consulted. A past kickoff alone
means results are awaited; it never proves that a match is live or successful.
"""
from datetime import date, datetime, timezone
from math import isfinite
import unicodedata
from zoneinfo import ZoneInfo

from coupon_sizes import validate_size
import journal_performance as performance
import scenario_outcomes


ZONE = ZoneInfo('Africa/Kinshasa')
METHOD_ORDER = ('combined', 'frequency', 'clubs', 'catboost', 'context_neighbors')
METHOD_NAMES = {'combined': 'Clubs + répétitions', 'frequency': 'Répétitions seules',
                'clubs': 'Clubs seuls', 'catboost': 'CatBoost',
                'context_neighbors': 'Répétitions par contexte'}
STATES = {'pending': 'En attente', 'awaiting_results': 'Résultats attendus',
          'in_progress': 'Résultats partiels', 'settled': 'Évalué',
          'void': 'Annulé', 'unresolved': 'À vérifier'}
ORIGINS = {'manual': 'Sélection manuelle', 'automatic': 'Regroupement automatique'}
VALUES = ('1st', 'Égal', '2nd')


def _stamp(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        raise ValueError('Une date enregistrée dans le journal est invalide.') from None


def _now(value):
    value = datetime.now(timezone.utc) if value is None else value
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('L’heure de consultation doit préciser son fuseau.')
    return value.astimezone(timezone.utc)


def _identity(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError('Un identifiant du journal est invalide.')
    text = str(value)
    if not text.isascii() or not text.isdecimal() or int(text) <= 0:
        raise ValueError('Un identifiant du journal est invalide.')
    return str(int(text))


def _primary(entry, size):
    methods = entry.get('methods') or {}
    if not isinstance(methods, dict):
        raise ValueError('Les méthodes enregistrées sont invalides.')
    metadata = entry.get('metadata') or {}
    selected = {'statistical': 'combined'}.get(metadata.get('selected_engine'), metadata.get('selected_engine'))
    name = selected if selected in METHOD_ORDER and selected in methods else next(
        (method for method in METHOD_ORDER if method in methods), None)
    if name is None:
        return dict(primary_method=None, primary_method_name='Non conservée', primary_sequence=None,
                    primary_probability=None, primary_version=None, primary_generation=None)
    payload = methods[name]
    if not isinstance(payload, dict):
        raise ValueError('La méthode principale enregistrée est invalide.')
    sequence = probability = None
    ranking = payload.get('ranking')
    if ranking:
        if not isinstance(ranking, (list, tuple)) or not isinstance(ranking[0], dict):
            raise ValueError('Le premier scénario enregistré est invalide.')
        first = ranking[0]
        sequence, probability = first.get('sequence'), first.get('probability')
        if (not isinstance(sequence, (list, tuple)) or len(sequence) != size
                or any(value not in VALUES for value in sequence)
                or isinstance(probability, bool) or not isinstance(probability, (int, float))
                or not isfinite(probability) or not 0 <= probability <= 1):
            raise ValueError('Le premier scénario ou sa probabilité enregistrée est invalide.')
        sequence = list(sequence)
    return dict(primary_method=name, primary_method_name=METHOD_NAMES[name], primary_sequence=sequence,
                primary_probability=probability, primary_version=payload.get('version'),
                primary_generation=payload.get('generation'))


def _row(entry, current):
    if entry.get('source') != 'API-Football':
        raise ValueError('Seules les prévisions du journal API-Football sont disponibles.')
    identifier = entry.get('id')
    if not isinstance(identifier, str) or not identifier:
        raise ValueError('Une prévision doit conserver son identifiant complet.')
    league, size = _identity(entry['league']), validate_size(entry['size'])
    fixtures = entry['fixtures']
    if len(fixtures) != size:
        raise ValueError('La taille enregistrée doit correspondre au groupe complet.')
    frozen, ids, stamps = [], set(), []
    for fixture in fixtures:
        if fixture.get('provider') != 'api-football' or _identity(fixture['league']) != league:
            raise ValueError('Les affiches doivent conserver la source et la compétition du journal.')
        mid = _identity(fixture['match_id'])
        if mid in ids:
            raise ValueError('Le groupe enregistré contient un match répété.')
        home, away = fixture.get('home'), fixture.get('away')
        if not all(isinstance(name, str) and name.strip() for name in (home,away)) or home == away:
            raise ValueError('Les deux clubs enregistrés sont nécessaires.')
        stamp = _stamp(fixture['timestamp'])
        ids.add(mid);stamps.append(stamp)
        frozen.append({key: fixture[key] for key in ('match_id','league','provider','home','away','home_id','away_id','timestamp','season')
                       if key in fixture})
    if stamps != sorted(stamps):
        raise ValueError('Le groupe enregistré doit conserver son ordre chronologique.')
    created = _stamp(entry['created_at'])
    status = entry.get('status')
    if status not in ('pending','settled','void','unresolved'):
        raise ValueError('L’état enregistré du groupe est invalide.')
    observed = performance.observed_count(entry)
    if status == 'pending':
        state = ('in_progress' if observed else 'awaiting_results' if min(stamps) <= current else 'pending')
    else:
        state = status
    metadata = entry.get('metadata') or {}
    if not isinstance(metadata, dict):
        raise ValueError('Les informations enregistrées du groupe sont invalides.')
    origin = performance.origin(entry)
    names = [f"{fixture['home']} — {fixture['away']}" for fixture in frozen]
    local = [stamp.astimezone(ZONE) for stamp in stamps]
    result = dict(id=identifier, entry_id=identifier, source='API-Football', league=league,
        competition=metadata.get('competition') or league, country=metadata.get('country') or 'Pays non précisé',
        size=size, origin=origin, origin_label=ORIGINS[origin],
        journal_status=status, state=state, status=state, status_label=STATES[state],
        created_at=created.isoformat(), created_local=created.astimezone(ZONE).strftime('%d/%m/%Y à %H:%M'),
        kickoff_range=f"{min(local).strftime('%H:%M')}–{max(local).strftime('%H:%M')}",
        fixtures=frozen, clubs=[name for fixture in frozen for name in (fixture['home'],fixture['away'])],
        match_names=names, observed_matches=observed, expected_matches=size, progress=observed,total=size,
        scenario_outcome=scenario_outcomes.summarize(entry),
        day=local[0].date().isoformat())
    result.update(_primary(entry,size))
    return result


def _ordered(rows):
    # Stable ID tie-breaker, independent of an input file's order.
    return sorted(sorted(rows,key=lambda row:row['id']),key=lambda row:_stamp(row['created_at']),reverse=True)


def build_rows(entries, now=None, *, day=None):
    """Keep complete groups for a local day, defaulting to today in Kinshasa.

    Unknown/mixed kickoff days follow ``entries_on_day`` and remain outside the
    list. Retained entries must be genuine API-Football journal shapes. Copies
    contain only frozen display fields; scores never select the primary method.
    """
    current = _now(now)
    today = current.astimezone(ZONE).date()
    day = today if day is None else day
    if not isinstance(day, date) or isinstance(day, datetime) or day > today:
        raise ValueError('La journée consultée doit être aujourd’hui ou une date passée à Kinshasa.')
    entries = list(entries)
    if any(not isinstance(entry,dict) for entry in entries):
        raise ValueError('Les prévisions doivent conserver leur structure enregistrée.')
    selected = performance.entries_on_day(entries,day)
    rows, identities = [], set()
    for entry in selected:
        try:
            row = _row(entry,current)
        except (KeyError,TypeError,AttributeError):
            raise ValueError('Une prévision enregistrée est incomplète ou invalide.') from None
        if row['id'] in identities:
            raise ValueError('Une prévision ne peut pas être affichée deux fois.')
        identities.add(row['id']);rows.append(row)
    return _ordered(rows)


def _search(value):
    return ''.join(char for char in unicodedata.normalize('NFKD',str(value).casefold()) if not unicodedata.combining(char))


def filter_rows(rows, league=None, size=None, origin='all', state='all', query='', verdict='all'):
    """Filter every size/league by default, with accent-insensitive club search."""
    origin, state = origin or 'all', state or 'all'
    if origin not in ('all','manual','automatic') or state not in ('all',*STATES):
        raise ValueError('Filtre de prévision inconnu.')
    if verdict not in ('all', 'hit', 'miss', 'pending', 'void', 'unresolved', 'unavailable'):
        raise ValueError('Filtre de réalisation des scénarios inconnu.')
    if size is not None:validate_size(size)
    needle = _search(query.strip())
    result=[]
    for row in rows:
        haystack = ' '.join(str(value) for value in (row['id'],row['league'],row['competition'],row['country'],*row['match_names']))
        if ((league is None or row['league']==str(league)) and (size is None or row['size']==size)
                and (origin=='all' or row['origin']==origin) and (state=='all' or row['state']==state)
                and (verdict=='all' or (row.get('scenario_outcome') or {}).get('state')==verdict)
                and (not needle or needle in _search(haystack))):
            result.append(row)
    return _ordered(result)


def select(rows, league=None, size=None, origin=None, status=None, query='', *, state=None):
    """UI alias; ``status`` denotes the display state, not the saved raw state."""
    if status is not None and state is not None and status != state:
        raise ValueError('Choisissez un seul état de prévision.')
    return filter_rows(rows,league,size,origin or 'all',state or status or 'all',query)
