"""Read-only, date-exact calendar export from the active local fixture store.

Only compact calendar fields cross the SQLite/Python boundary. Exporting never
collects, modifies, migrates or predicts. Calendar refresh receipts are reported
separately from stored fixture counts; neither certifies complete API coverage.
"""
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import calendar_data
import fixture_store
from halftime_domain import CLASS_ORDER

VERSION = 'calendar-period-export-v1'
ZONE = ZoneInfo('Africa/Kinshasa')
COLUMNS = (
    'Date', 'Heure (Kinshasa)', 'Fuseau horaire', 'Coup d’envoi UTC', 'ID du match',
    'Pays', 'Compétition', 'ID de compétition', 'Saison', 'Domicile', 'Extérieur',
    'Score actuel', 'Mi-temps', 'À 90 minutes', 'Statut', 'Code statut API',
    'Minute', 'Verdict final', 'Motif du verdict', 'Source', 'Extraction UTC',
)
DAY_COLUMNS = ('Date', 'Matchs stockés', 'Matchs exportés', 'État du relevé', 'Dernier relevé UTC')


class ExportDataError(ValueError):
    """A safe user-facing problem in the local export request or database."""


def _now(value):
    current = value or datetime.now(timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise ExportDataError('L’heure d’extraction doit préciser son fuseau horaire.')
    return current.astimezone(timezone.utc)


def _dates(start, end):
    if type(start) is not date or type(end) is not date:
        raise ExportDataError('Choisissez une date de début et de fin sans heure.')
    if not date(1900, 1, 1) <= start <= end <= date(2100, 12, 31):
        raise ExportDataError('La période doit être ordonnée entre 1900 et 2100.')
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def _bounds(start, end):
    dates = _dates(start, end)
    lower = datetime.combine(start, time.min, ZONE).astimezone(timezone.utc)
    upper = datetime.combine(end + timedelta(days=1), time.min, ZONE).astimezone(timezone.utc)
    return dates, lower.replace(tzinfo=None).isoformat(), upper.replace(tzinfo=None).isoformat()


def _database(path):
    if path is None:
        import api_football
        path = api_football.STORE_PATH
    try:
        database = fixture_store.active_path(Path(path))
    except (fixture_store.FixtureStoreError, TypeError, ValueError):
        raise ExportDataError('La base locale API-Football est indisponible ou invalide.') from None
    if database is None:
        raise ExportDataError('Cet export nécessite la base locale SQLite déjà migrée. '
                              'Aucune migration ni lecture intégrale du JSON n’est lancée ici.')
    return database


def _identifier(value):
    if (isinstance(value, bool) or not isinstance(value, (int, str))
            or not str(value).isascii() or not str(value).isdigit() or int(value) <= 0):
        raise ExportDataError('Un identifiant de compétition est invalide.')
    return str(int(value))


def _leagues(values):
    if values is None:
        return None
    if isinstance(values, (str, int)):
        values = (values,)
    try:
        return tuple(sorted({_identifier(value) for value in values}, key=int))
    except TypeError:
        raise ExportDataError('La sélection des compétitions est invalide.') from None


def _text(value, fallback):
    return value.strip() if isinstance(value, str) and value.strip() else fallback


_PROJECTION = '''match_id, kickoff_utc, league,
 json_extract(raw_json,
 '$.provider', '$.fixture.id', '$.fixture.date', '$.fixture.status',
 '$.league.id', '$.league.name', '$.league.country', '$.league.season',
 '$.teams.home', '$.teams.away', '$.goals', '$.score.halftime', '$.score.fulltime',
 '$._patternlab_import') AS calendar_projection'''
_ORDER = ' ORDER BY kickoff_utc, CAST(match_id AS INTEGER), match_id'


def _display(record, now):
    (provider, mid, stamp, state, league, name, country, season, home, away,
     goals, halftime, fulltime, quality) = json.loads(record['calendar_projection'])
    raw = dict(provider=provider, fixture=dict(id=mid, date=stamp, status=state),
               league=dict(id=league, name=_text(name, 'Compétition inconnue'),
                           country=_text(country, 'International'), season=season),
               teams=dict(home=home, away=away), goals=goals,
               score=dict(halftime=halftime, fulltime=fulltime))
    if quality is not None:
        raw['_patternlab_import'] = quality
    row = calendar_data.from_raw(raw)
    if (row['match_id'] != record['match_id'] or row['league'] != record['league']
            or row['timestamp'] != record['kickoff_utc']):
        raise ValueError('Les champs du match et son index local sont incohérents')
    kickoff = datetime.fromisoformat(row['timestamp']).replace(tzinfo=timezone.utc)
    # A corrupt provider final received before kickoff is not a real result.
    if kickoff > now and row['final_verdict'] in CLASS_ORDER:
        row['final_verdict'] = 'À vérifier'
        row['final_verdict_reason'] = 'Résultat final annoncé avant le coup d’envoi'
    local = kickoff.astimezone(ZONE)
    output = {
        'Date': local.date().isoformat(), 'Heure (Kinshasa)': local.strftime('%H:%M:%S'),
        'Fuseau horaire': str(ZONE), 'Coup d’envoi UTC': kickoff.isoformat(timespec='seconds'),
        'ID du match': row['match_id'], 'Pays': row['country'], 'Compétition': row['competition'],
        'ID de compétition': row['league'], 'Saison': season if type(season) is int else None,
        'Domicile': row['home'], 'Extérieur': row['away'], 'Score actuel': row['score'],
        'Mi-temps': row['halftime_score'], 'À 90 minutes': row['regulation_score'],
        'Statut': row['status_label'], 'Code statut API': row['raw_status'],
        'Minute': row['minute'], 'Verdict final': row['final_verdict'],
        'Motif du verdict': row['final_verdict_reason'],
        'Source': 'API-Football' if provider in (None, 'api-football', 'API-Football') else
                  'Source non confirmée : ' + _text(provider, 'inconnue'),
        'Extraction UTC': now.isoformat(timespec='seconds'),
    }
    return row, output


def competitions(start, end, *, path=None):
    """Small period-scoped competition controls, without loading match rows."""
    _days, lower, upper = _bounds(start, end)
    result = []
    try:
        with fixture_store._connection(_database(path)) as connection:
            fixture_store._meta(connection)
            # SQLite's single max() chooses metadata on a latest kickoff row.
            # Count and ID are the authoritative dropdown selection, not text.
            cursor = connection.execute('''SELECT league, count(*) AS matches, max(kickoff_utc),
                json_extract(raw_json, '$.league.name') AS name,
                json_extract(raw_json, '$.league.country') AS country
                FROM fixtures WHERE kickoff_utc>=? AND kickoff_utc<? AND league IS NOT NULL
                GROUP BY league''', (lower, upper))
            for row in cursor:
                try:
                    league = _identifier(row['league'])
                except ExportDataError:
                    continue
                result.append(dict(league=league, competition=_text(row['name'], 'Compétition '+league),
                                   country=_text(row['country'], 'International'), count=row['matches']))
    except fixture_store.FixtureStoreError:
        raise ExportDataError('La liste des compétitions locales est momentanément indisponible.') from None
    return sorted(result, key=lambda item: (item['country'].casefold(), item['competition'].casefold(), int(item['league'])))


def build(start, end, *, leagues=None, status='Tous', query='', now=None, path=None):
    """Export each stored match once across an inclusive Kinshasa date range.

    Coverage counts every indexed stored fixture before the requested filters,
    including invalid records which are separately reported. A calendar receipt
    shows a dated request was made, not a guarantee of complete/final results.
    """
    days, lower, upper = _bounds(start, end)
    selected, current = _leagues(leagues), _now(now)
    try:
        calendar_data.filter_rows([], status=status, query=query)
    except ValueError as error:
        raise ExportDataError(str(error)) from None
    rows, invalid, counts, exported, competitions = [], [], Counter(), Counter(), {}
    try:
        with fixture_store._connection(_database(path)) as connection:
            meta = fixture_store._meta(connection)
            cursor = connection.execute('SELECT ' + _PROJECTION + ' FROM fixtures '
                                        'WHERE kickoff_utc >= ? AND kickoff_utc < ?' + _ORDER,
                                        (lower, upper))
            for record in cursor:
                try:
                    stamp = datetime.fromisoformat(record['kickoff_utc']).replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    invalid.append(dict(match_id=record['match_id'], day=None,
                                        reason='Date indexée illisible : journée impossible à attribuer'))
                    continue
                day = stamp.astimezone(ZONE).date().isoformat()
                counts[day] += 1
                try:
                    display, output = _display(record, current)
                except (ValueError, TypeError, KeyError, AttributeError) as error:
                    invalid.append(dict(match_id=record['match_id'], day=day, reason=str(error)))
                    continue
                item = competitions.setdefault(display['league'], dict(league=display['league'],
                    competition=display['competition'], country=display['country'], count=0))
                item['count'] += 1
                if selected is not None and display['league'] not in selected:
                    continue
                if not calendar_data.filter_rows([display], status=status, query=query):
                    continue
                rows.append(output)
                exported[day] += 1
            receipts = dict(connection.execute('SELECT day, updated_at FROM calendar_updates '
                'WHERE timezone=? AND day>=? AND day<=?', (str(ZONE), start.isoformat(), end.isoformat())))
            unassignable = connection.execute('SELECT count(*) FROM fixtures WHERE kickoff_utc IS NULL').fetchone()[0]
            revision, store_uuid = int(meta['revision']), meta['store_uuid']
    except fixture_store.FixtureStoreError:
        raise ExportDataError('La lecture du calendrier local est momentanément indisponible.') from None
    coverage, missing, unconfirmed = [], [], []
    for day in days:
        iso = day.isoformat()
        receipt = receipts.get(iso)
        if receipt:
            state = 'Relevé calendrier enregistré' if counts[iso] else 'Relevé enregistré sans match stocké'
        elif counts[iso]:
            state = 'Rencontres stockées sans relevé calendrier daté'
            unconfirmed.append(iso)
        else:
            state = 'Aucune donnée locale ni relevé calendrier'
            missing.append(iso)
        coverage.append({'Date': iso, 'Matchs stockés': counts[iso], 'Matchs exportés': exported[iso],
                         'État du relevé': state, 'Dernier relevé UTC': receipt})
    return dict(version=VERSION, columns=list(COLUMNS), day_columns=list(DAY_COLUMNS), rows=rows, days=coverage,
        competitions=sorted(competitions.values(), key=lambda item:
                            (item['country'].casefold(), item['competition'].casefold(), int(item['league']))),
        metadata=dict(start=start.isoformat(), end=end.isoformat(), day_count=len(days),
            timezone=str(ZONE), exported_at_utc=current.isoformat(timespec='seconds'),
            revision=revision, store_uuid=store_uuid, leagues=list(selected) if selected is not None else None,
            status=status, query=query, stored_matches=sum(counts.values()), exported_matches=len(rows),
            missing_days=missing, unconfirmed_days=unconfirmed,
            invalid_records=invalid, unassignable_records=unassignable,
            coverage_note='Les compteurs décrivent la base locale. Un relevé daté ne garantit '
                          'ni l’exhaustivité du fournisseur ni la disponibilité des scores finaux.'))
