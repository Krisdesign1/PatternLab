"""Read-only, bounded-memory descriptive trends from the local fixture database.

The frozen store projection is the import-quality boundary: quarantine, invalid
providers and incomplete results have no normalized_json. We recheck its small
HT/FT projection while streaming; full raw payloads are never loaded into Python.
This describes the current corrected catalogue, not an as-of prediction replay.
"""
from collections import Counter, deque
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import fixture_store
from halftime_domain import CLASS_ORDER, FINISHED_STATUSES, ContractError, parse_score, regulation_result

ZONE = ZoneInfo('Africa/Kinshasa')
VERSION = 'competition-trends-v1'


class TrendsError(ValueError):
    """A safe user-facing failure of the descriptive view."""


def _now(value):
    value = value or datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TrendsError('L’heure de consultation doit préciser son fuseau horaire.')
    return value.astimezone(timezone.utc)


def _day(value):
    if type(value) is not date:
        raise TrendsError('La période doit contenir des dates sans heure.')
    return value


def _seasons(values):
    if values is None:
        return None
    try:
        result = tuple(sorted(set(values)))
    except (TypeError, ValueError):
        raise TrendsError('La sélection des saisons est invalide.') from None
    if any(type(value) is not int or not 1900 <= value <= 2100 for value in result):
        raise TrendsError('La sélection des saisons est invalide.')
    return result


def _database(path):
    if path is None:
        import api_football
        path = api_football.STORE_PATH
    try:
        database = fixture_store.active_path(Path(path))
    except (fixture_store.FixtureStoreError, TypeError, ValueError):
        raise TrendsError('La base locale structurée API-Football est indisponible.') from None
    if database is None:
        raise TrendsError('Les tendances nécessitent la base locale SQLite déjà migrée. '
                          'Aucune migration ni lecture intégrale du JSON n’est lancée depuis cet écran.')
    return database


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError('Date absente')
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)


def _utc_text(value):
    return value.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds')


def _bounds(start, end, seasons, now, league=None):
    start, end, seasons, now = _day(start), _day(end), _seasons(seasons), _now(now)
    if start > end:
        raise TrendsError('Le début de la période doit précéder sa fin.')
    lower = datetime.combine(start, time.min, ZONE).astimezone(timezone.utc)
    upper = min(datetime.combine(end + timedelta(days=1), time.min, ZONE).astimezone(timezone.utc), now)
    clauses = ['kickoff_utc >= ?', 'kickoff_utc < ?']
    args = [_utc_text(lower), _utc_text(upper)]
    if seasons is not None:
        clauses.append('season IN (' + ','.join('?' for _ in seasons) + ')' if seasons else '0')
        args.extend(seasons)
    if league is not None:
        league = _identity(league)
        clauses.append('league = ?')
        args.append(league)
    return ' AND '.join(clauses), args, now


def _identity(value):
    if (isinstance(value, bool) or not isinstance(value, (int, str))
            or not str(value).isascii() or not str(value).isdigit() or int(value) <= 0):
        raise TrendsError('Identifiant de compétition ou de rencontre invalide.')
    return str(int(value))


# Valid projections are compact (~500 bytes), unlike full event payloads. Invalid
# rows expose only the metadata and two score pairs needed to explain exclusions.
_COLUMNS = '''match_id, kickoff_utc, league, season, status, issue, expected,
    normalized_json IS NOT NULL AS has_projection,
    CASE WHEN normalized_json IS NOT NULL THEN normalized_json ELSE
      json_extract(raw_json, '$.league.name', '$.league.country', '$.teams.home.name',
                   '$.teams.away.name', '$.score.halftime', '$.score.fulltime',
                   '$._patternlab_import.quarantined', '$._patternlab_import.reason')
    END AS projection'''
_ORDER = ' ORDER BY kickoff_utc, CAST(match_id AS INTEGER), match_id'


def _score(value):
    try:
        return '-'.join(map(str, parse_score(value)))
    except ContractError:
        return None


def _text(value, fallback):
    return value.strip() if isinstance(value, str) and value.strip() else fallback


class _RecordContext:
    """Bounded memoization and evidence for one sequential read, never shared.

    A season repeats the same score strings and identities many times. Cache
    their pure parsing only; every fixture still crosses every provenance,
    identity, date and result check. Mutable values and invalid values keep the
    original validation path. The last payload/result are reused by club views
    rather than decoding and parsing the same fixture a second time.
    """
    def __init__(self):
        self._identity = lru_cache(maxsize=4096, typed=True)(_identity)
        self._score_pair = lru_cache(maxsize=256)(parse_score)
        self.payload = self.outcome = None

    def identity(self, value):
        return self._identity(value) if type(value) in (int, str) else _identity(value)

    def score_pair(self, value):
        return self._score_pair(value) if isinstance(value, str) else parse_score(value)

    def score(self, value):
        try:
            return '-'.join(map(str, self.score_pair(value)))
        except ContractError:
            return None


def _record(row, *, _context=None):
    identity = _context.identity if _context is not None else _identity
    score = _context.score if _context is not None else _score
    if _context is not None:
        _context.payload = _context.outcome = None
    stamp = _stamp(row['kickoff_utc'])
    result = dict(match_id=row['match_id'], league=row['league'], season=row['season'],
                  timestamp=_utc_text(stamp), local_day=stamp.astimezone(ZONE).date().isoformat(),
                  kickoff_local=stamp.astimezone(ZONE).isoformat(timespec='seconds'),
                  status=row['status'], raw_status=row['status'], included=False, value=None,
                  reason=None, ht=None, ft=None, home='Club non confirmé', away='Club non confirmé',
                  competition=str(row['league'] or 'Compétition inconnue'), country='International')
    if row['has_projection']:
        try:
            payload = json.loads(row['projection'])
            if _context is not None:
                _context.payload = payload
            result.update(home=_text(payload.get('home'), result['home']), away=_text(payload.get('away'), result['away']),
                          competition=_text(payload.get('competition'), result['competition']),
                          country=_text(payload.get('country'), result['country']),
                          ht=score(payload.get('ht')), ft=score(payload.get('ft')))
            if payload.get('provider') != 'api-football':
                raise ValueError('Source API-Football non confirmée')
            if row['status'] not in FINISHED_STATUSES:
                raise ValueError('Projection finale associée à un match non terminé')
            if (identity(payload.get('match_id')) != identity(row['match_id'])
                    or identity(payload.get('league')) != identity(row['league'])
                    or payload.get('season') != row['season']
                    or _stamp(payload.get('timestamp')) != stamp):
                raise ValueError('Projection et index de la rencontre incohérents')
            if (type(payload.get('season')) is not int or not 1900 <= payload['season'] <= 2100
                    or identity(payload.get('home_id')) == identity(payload.get('away_id'))
                    or not all(isinstance(payload.get(key), str) and payload[key].strip()
                               for key in ('home', 'away', 'competition'))):
                raise ValueError('Identité ou saison de la rencontre non confirmée')
            ht, ft = payload.get('ht'), payload.get('ft')
            if _context is not None:
                ht, ft = _context.score_pair(ht), _context.score_pair(ft)
            outcome = regulation_result(ht, ft, status=row['status'])
            if payload.get('value') != outcome.label:
                raise ValueError('Verdict conservé incohérent avec les scores')
            result.update(included=True, value=outcome.label, reason='Scores HT/90 minutes exploitables')
            if _context is not None:
                _context.outcome = outcome
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            result['reason'] = 'Projection à vérifier : ' + str(error)
    else:
        try:
            name, country, home, away, ht, ft, quarantined, quality_reason = json.loads(row['projection'])
            result.update(competition=_text(name, result['competition']), country=_text(country, result['country']),
                          home=_text(home, result['home']), away=_text(away, result['away']), ht=score(ht), ft=score(ft))
            if quarantined:
                result['reason'] = row['issue'] or 'Correction d’import en quarantaine'
        except (ValueError, TypeError):
            result['reason'] = 'Métadonnées de la rencontre à vérifier'
        result['reason'] = result['reason'] or row['issue'] or (
            'Résultat final sans scores HT/90 minutes exploitables' if row['status'] in FINISHED_STATUSES
            else 'Match non terminé ou résultat administratif')
    result['excluded'] = not result['included'] and bool(row['expected'] or row['status'] in FINISHED_STATUSES
                                                        or row['has_projection'])
    return result


def catalogue(path=None, *, now=None):
    """Return small controls/coverage metadata; never parse full fixture payloads."""
    current = _now(now)
    try:
        with fixture_store._connection(_database(path)) as connection:
            meta = fixture_store._meta(connection)
            bound = _utc_text(current)
            first, last, count = connection.execute(
                'SELECT min(kickoff_utc), max(kickoff_utc), count(*) FROM fixtures WHERE kickoff_utc < ?',
                (bound,)).fetchone()
            # The catalogue offers every stored season (including schedules).
            # A kickoff predicate here makes SQLite chase every large payload
            # in date-index order just to list seven integers. The distribution
            # query below still enforces the selected period and current clock.
            seasons = [row[0] for row in connection.execute(
                'SELECT DISTINCT season FROM fixtures WHERE season BETWEEN 1900 AND 2100 ORDER BY season DESC')]
            unassigned = connection.execute('SELECT count(*) FROM fixtures WHERE kickoff_utc IS NULL').fetchone()[0]
            return dict(seasons=seasons, first_day=_stamp(first).astimezone(ZONE).date().isoformat() if first else None,
                        last_day=_stamp(last).astimezone(ZONE).date().isoformat() if last else None,
                        observed_matches=count, unassigned_matches=unassigned, timezone=str(ZONE),
                        revision=int(meta['revision']), store_uuid=meta['store_uuid'], updated_at=meta.get('updated_at'))
    except fixture_store.FixtureStoreError:
        raise TrendsError('La lecture des tendances est momentanément indisponible.') from None


def _percent(counts, total):
    return {value: 100 * counts[value] / total if total else None for value in CLASS_ORDER}


def build(start, end, *, seasons=None, recent=30, now=None, path=None):
    """Summarize all selected competitions with O(leagues × recent) memory.

    Recent N and earlier baseline are disjoint subsets of the same selected
    period/seasons. Each stored fixture ID appears once; only current corrected
    results are described, without claiming historical publication times.
    """
    if type(recent) is not int or recent < 1:
        raise TrendsError('Le nombre de matchs récents doit être un entier positif.')
    where, args, current = _bounds(start, end, seasons, now)
    groups, reasons = {}, Counter()
    unassigned = Counter(matches=0, excluded=0, not_finished=0, observed=0)
    try:
        with fixture_store._connection(_database(path)) as connection:
            meta = fixture_store._meta(connection)
            cursor = connection.execute('SELECT ' + _COLUMNS + ' FROM fixtures WHERE ' + where + _ORDER, args)
            for row in cursor:
                record = _record(row)
                league = record['league']
                try:
                    league = _identity(league)
                except TrendsError:
                    unassigned['observed'] += 1
                    if record['excluded']:
                        unassigned['excluded'] += 1
                        reasons['Compétition non identifiée : ' + record['reason']] += 1
                    else:
                        unassigned['not_finished'] += 1
                    continue
                item = groups.setdefault(league, dict(league=league, competition=record['competition'],
                    country=record['country'], matches=0, excluded=0, not_finished=0, observed=0,
                    counts=Counter({value: 0 for value in CLASS_ORDER}), latest=deque(maxlen=recent),
                    first_day=None, last_day=None, seasons=set()))
                item['observed'] += 1
                item['competition'], item['country'] = record['competition'], record['country']
                if type(record['season']) is int:
                    item['seasons'].add(record['season'])
                if record['included']:
                    item['matches'] += 1
                    item['counts'][record['value']] += 1
                    item['latest'].append(record['value'])
                    item['first_day'] = item['first_day'] or record['local_day']
                    item['last_day'] = record['local_day']
                elif record['excluded']:
                    item['excluded'] += 1
                    reasons[record['reason']] += 1
                else:
                    item['not_finished'] += 1
            revision, store_uuid = int(meta['revision']), meta['store_uuid']
    except fixture_store.FixtureStoreError:
        raise TrendsError('La lecture des tendances est momentanément indisponible.') from None
    rows = []
    for item in groups.values():
        latest = item.pop('latest')
        item['seasons'] = sorted(item['seasons'])
        item['counts'] = dict(item['counts'])
        item['recent_matches'] = len(latest)
        item['recent_counts'] = {value: latest.count(value) for value in CLASS_ORDER}
        item['previous_matches'] = item['matches'] - len(latest)
        item['previous_counts'] = {value: item['counts'][value] - item['recent_counts'][value] for value in CLASS_ORDER}
        item['percentages'] = _percent(item['counts'], item['matches'])
        item['recent_percentages'] = _percent(item['recent_counts'], item['recent_matches'])
        item['previous_percentages'] = _percent(item['previous_counts'], item['previous_matches'])
        item['delta_points'] = {value: item['recent_percentages'][value] - item['previous_percentages'][value]
            if item['recent_matches'] and item['previous_matches'] else None for value in CLASS_ORDER}
        maximum = max(item['counts'].values()) if item['matches'] else None
        item['leaders'] = [value for value in CLASS_ORDER if maximum is not None and item['counts'][value] == maximum]
        rows.append(item)
    rows.sort(key=lambda item: (item['country'].casefold(), item['competition'].casefold(), str(item['league'])))
    totals = {key: sum(item[key] for item in rows) + unassigned[key]
              for key in ('matches', 'excluded', 'not_finished', 'observed')}
    totals['competitions'] = len(rows)
    return dict(version=VERSION, rows=rows, totals=totals, exclusion_reasons=dict(sorted(reasons.items())),
                start=start.isoformat(), end=end.isoformat(), recent=recent,
                timezone=str(ZONE), as_of=current.isoformat(), revision=revision, store_uuid=store_uuid,
                unassigned_matches=unassigned['observed'], unassigned=dict(unassigned))


def details(league, start, end, *, seasons=None, now=None, path=None, limit=50, offset=0):
    """Read one competition's score evidence, with an explicit display page."""
    if type(offset) is not int or offset < 0 or (limit is not None and (type(limit) is not int or limit < 1)):
        raise TrendsError('La pagination des rencontres est invalide.')
    where, args, current = _bounds(start, end, seasons, now, league)
    try:
        with fixture_store._connection(_database(path)) as connection:
            meta = fixture_store._meta(connection)
            total = connection.execute('SELECT count(*) FROM fixtures WHERE ' + where, args).fetchone()[0]
            query = 'SELECT ' + _COLUMNS + ' FROM fixtures WHERE ' + where
            query += ' ORDER BY kickoff_utc DESC, CAST(match_id AS INTEGER) DESC, match_id DESC'
            query += ' LIMIT ? OFFSET ?'
            rows = [_record(row) for row in connection.execute(query, [*args, -1 if limit is None else limit, offset])]
            return dict(rows=rows, total=total, offset=offset, limit=limit, timezone=str(ZONE),
                        as_of=current.isoformat(), revision=int(meta['revision']), store_uuid=meta['store_uuid'])
    except fixture_store.FixtureStoreError:
        raise TrendsError('La lecture des rencontres est momentanément indisponible.') from None
