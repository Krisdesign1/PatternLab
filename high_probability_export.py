"""Pure exports of already projected, saved pre-match probabilities.

``build`` consumes the rows produced by ``high_probability_archive_view`` and
optionally annotated by ``result_explanations``. It never loads storage, calls a
provider, reads the clock or recalculates a forecast. ``minimum`` uses the saved
probability scale, from 0 to 1; filtering happens before display rounding.
"""
from copy import deepcopy
import csv
from datetime import date, datetime, timezone
from io import StringIO
import json
from math import fsum, isclose, isfinite
from zoneinfo import ZoneInfo

from scenario_odds import DEFAULT_ODDS


VERSION = 'high-probability-export-v1'
VALUES = ('1st', 'Égal', '2nd')
ZONE = ZoneInfo('Africa/Kinshasa')
ODDS_SOURCE = 'Références fixes manuelles ; aucune cote de marché API'
COLUMNS = (
    'Journée', 'ID du match', 'Match', 'Club domicile', 'Club extérieur', 'Pays',
    'Compétition', 'ID de compétition', 'Coup d’envoi (Kinshasa)', 'Catégorie',
    'Probabilité (%)', 'Cote de référence', 'Source de la cote', 'Tendance (%)',
    'Écart (points)', '1st (%)', 'Égal (%)', '2nd (%)', 'Score à la pause',
    'Score à 90 minutes', 'Résultat réel', 'État', 'Statut', 'Motif', 'Verdict',
    'Capture UTC', 'Référence archivée', 'Version du modèle', 'Provenance',
    'Source prévision', 'Coupure historique UTC', 'Résultats historiques',
    'Journées historiques',
)
DECIMAL_COLUMNS = frozenset((
    'Probabilité (%)', 'Cote de référence', 'Tendance (%)', 'Écart (points)',
    '1st (%)', 'Égal (%)', '2nd (%)',
))


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError('Horodatage archivé absent ou invalide.')
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, TypeError) as error:
        raise ValueError('Horodatage archivé absent ou invalide.') from error
    # This matches the existing archive's interpretation of old UTC stamps.
    return (stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None
            else stamp.astimezone(timezone.utc))


def _day(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if not isinstance(value, str):
        raise ValueError('Une journée doit être une date ISO AAAA-MM-JJ.')
    try:
        normalized = date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise ValueError('Une journée doit être une date ISO AAAA-MM-JJ.') from error
    if normalized != value:
        raise ValueError('Une journée doit être une date ISO AAAA-MM-JJ.')
    return normalized


def _days(values):
    if values is None:
        return None
    if isinstance(values, (str, date)):
        values = [values]
    return {_day(value) for value in values}


def _outcomes(values):
    if values is None or (isinstance(values, str) and values in ('all', 'Toutes', 'toutes')):
        return VALUES
    if isinstance(values, str):
        values = [values]
    requested = set(values)
    if not requested.issubset(VALUES):
        raise ValueError('Catégorie inconnue : choisir 1st, Égal ou 2nd.')
    return tuple(value for value in VALUES if value in requested)


def _probabilities(values):
    if (not isinstance(values, dict) or set(values) != set(VALUES)
            or any(type(value) not in (int, float) or not isfinite(value)
                   or not 0 <= value <= 1 for value in values.values())
            or not isclose(fsum(values.values()), 1, rel_tol=1e-9, abs_tol=1e-9)):
        raise ValueError('Les trois probabilités archivées sont invalides.')
    return {key: float(values[key]) for key in VALUES}


def _identifier(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError('Identifiant de match archivé invalide.')
    value = str(value)
    if not value.isascii() or not value.isdecimal() or int(value) <= 0:
        raise ValueError('Identifiant de match archivé invalide.')
    return str(int(value))


def _explanation(row, identifier):
    explanation = row.get('result_explanation') or {}
    if not isinstance(explanation, dict):
        return None, None
    detail = next((item for item in explanation.get('matches', [])
                   if isinstance(item, dict) and str(item.get('match_id')) == identifier), {})
    return (detail.get('label') or explanation.get('label'),
            detail.get('reason') or explanation.get('summary'))


def _result(row, kickoff, now, outcome):
    status = row.get('status')
    if status not in ('settled', 'pending', 'unresolved', 'void'):
        status = 'unresolved'
    # Even malformed input cannot give a future fixture a final result.
    if kickoff > now:
        status = status if status in ('void', 'unresolved') else 'pending'
        actual, ht, ft = None, None, None
    else:
        actual = row.get('actual') if status == 'settled' and row.get('actual') in VALUES else None
        ht, ft = row.get('ht'), row.get('ft')
    if status == 'settled' and actual in VALUES:
        verdict = 'Réalisé' if actual == outcome else 'Non réalisé'
    elif status == 'void':
        verdict = 'Annulé du suivi'
    elif status == 'pending' and kickoff > now:
        verdict = 'En attente'
    else:
        verdict = 'Non évalué'
    return status, actual, ht, ft, verdict


def build(rows, *, days, outcomes, minimum=0, now):
    """Return one detached row per selected local day, match and category.

    ``days`` accepts ISO dates or date objects, individually or in an iterable;
    ``None`` selects every saved day. ``outcomes`` accepts one category, an
    iterable, or ``'all'``/``None``. Repeated identical fixtures use their first
    projected row. Contradictory identities, probabilities or saved results for
    the same day/match raise ``ValueError`` before probability filtering.
    """
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('Une horloge avec fuseau horaire est nécessaire.')
    if type(minimum) not in (int, float) or not isfinite(minimum) or not 0 <= minimum <= 1:
        raise ValueError('Le seuil de probabilité doit être compris entre 0 et 1.')
    selected_days, selected_outcomes = _days(days), _outcomes(outcomes)
    unique = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('fixture'), dict):
            raise ValueError('Une rencontre archivée est invalide.')
        fixture = row['fixture']
        kickoff = _stamp(fixture.get('timestamp'))
        day = kickoff.astimezone(ZONE).date().isoformat()
        if row.get('day', day) != day:
            raise ValueError('La journée archivée diffère du coup d’envoi à Kinshasa.')
        if selected_days is not None and day not in selected_days:
            continue
        identifier = _identifier(fixture.get('match_id'))
        if row.get('match_id') is not None and _identifier(row['match_id']) != identifier:
            raise ValueError('Les identifiants de la rencontre archivée diffèrent.')
        if fixture.get('provider') != 'api-football' or any(
                not isinstance(fixture.get(side), str) or not fixture[side].strip()
                for side in ('home', 'away')):
            raise ValueError('L’identité de la rencontre archivée est invalide.')
        created = _stamp(row.get('created_at'))
        if created >= kickoff or created > now:
            raise ValueError('La capture archivée doit précéder le match et l’export.')
        probabilities = _probabilities(row.get('probabilities'))
        identity = (kickoff.isoformat(), fixture.get('provider'), row.get('competition'),
                    *(str(fixture.get(field)) for field in
                      ('league', 'home_id', 'away_id', 'home', 'away', 'country', 'competition')))
        signature = (identity, probabilities,
                     tuple(row.get(field) for field in ('status', 'actual', 'ht', 'ft')))
        key = day, identifier
        if key in unique:
            if unique[key][0] != signature:
                raise ValueError(f'Archives contradictoires pour le match {identifier}, journée {day}.')
            continue
        unique[key] = (signature, row, kickoff, created, probabilities)

    result = []
    for (day, identifier), (_signature, row, kickoff, created, probabilities) in unique.items():
        fixture = row['fixture']
        try:
            baseline = _probabilities(row.get('baseline'))
        except ValueError:
            baseline = None
        state_label, reason = _explanation(row, identifier)
        for outcome in selected_outcomes:
            if probabilities[outcome] < minimum:
                continue
            status, actual, ht, ft, verdict = _result(row, kickoff, now, outcome)
            trend = baseline[outcome] * 100 if baseline is not None else None
            result.append({
                'Journée': day, 'ID du match': identifier,
                'Match': f'{fixture["home"]} — {fixture["away"]}',
                'Club domicile': fixture['home'], 'Club extérieur': fixture['away'],
                'Pays': fixture.get('country'),
                'Compétition': row.get('competition') or fixture.get('competition') or fixture.get('league'),
                'ID de compétition': str(fixture['league']) if fixture.get('league') is not None else None,
                'Coup d’envoi (Kinshasa)': kickoff.astimezone(ZONE).isoformat(),
                'Catégorie': outcome, 'Probabilité (%)': probabilities[outcome] * 100,
                'Cote de référence': DEFAULT_ODDS[outcome], 'Source de la cote': ODDS_SOURCE,
                'Tendance (%)': trend,
                'Écart (points)': (probabilities[outcome] - baseline[outcome]) * 100
                    if baseline is not None else None,
                **{f'{label} (%)': probabilities[label] * 100 for label in VALUES},
                'Score à la pause': deepcopy(ht), 'Score à 90 minutes': deepcopy(ft),
                'Résultat réel': actual, 'État': status,
                'Statut': (verdict if kickoff > now and row.get('status') == 'settled'
                           else state_label or verdict),
                'Motif': reason, 'Verdict': verdict, 'Capture UTC': created.isoformat(),
                'Référence archivée': row.get('id'), 'Version du modèle': row.get('model_version'),
                'Provenance': 'Classement archivé' if row.get('source_kind') == 'ranking_archive'
                    else 'Prévision déjà enregistrée' if row.get('source_kind') == 'saved_group'
                    else row.get('source_kind'),
                'Source prévision': row.get('source'), 'Coupure historique UTC': row.get('before'),
                'Résultats historiques': row.get('training'), 'Journées historiques': row.get('history_days'),
            })
    return deepcopy(result)


def _csv_cell(key, value):
    if value is None:
        return ''
    if key in DECIMAL_COLUMNS:
        if type(value) not in (int, float) or not isfinite(value):
            raise ValueError(f'Valeur numérique invalide pour {key}.')
        return f'{value:.2f}'.replace('.', ',')
    text = str(value)
    # Protect external text, including whitespace/control-prefixed formulas.
    # Numeric negative percentage-point gaps above remain genuine numbers.
    significant = text.lstrip(' \ufeff')
    if significant.startswith(('=', '+', '-', '@', '\t', '\r', '\n')):
        return "'" + text
    return text


def to_csv(rows):
    """Return Excel-compatible UTF-8 BOM CSV, with French decimal cells."""
    buffer = StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS, delimiter=';', lineterminator='\r\n')
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _csv_cell(key, row.get(key)) for key in COLUMNS})
    return buffer.getvalue().encode('utf-8-sig')


def to_json(rows, *, days=None, outcomes=None):
    """Return structured UTF-8 JSON, preserving numbers and missing values.

    Optional selection arguments preserve requested dates/categories even when
    no row meets the threshold. Without them, metadata describes exported rows.
    """
    rows = list(rows)
    selected_days = _days(days) if days is not None else {row['Journée'] for row in rows}
    selected_outcomes = (_outcomes(outcomes) if outcomes is not None else
                         tuple(value for value in VALUES if any(row['Catégorie'] == value for row in rows)))
    payload = {
        'version': VERSION, 'timezone': 'Africa/Kinshasa',
        'selection': {'days': sorted(selected_days), 'outcomes': list(selected_outcomes)},
        'odds_reference': {'kind': 'manual_fixed', 'source': ODDS_SOURCE,
                           'values': dict(DEFAULT_ODDS), 'market_api': False},
        'rows': rows,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
