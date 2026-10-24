"""Auditable settlement of the same provider fixture after a kickoff correction.

Forecast contents and their recorded order are immutable. Only a confirmed final
result may use a different kickoff, with the same provider ID, league and home /
away IDs, and a causal forecast made before both recorded and actual kickoffs.
"""
from datetime import date, datetime, timezone


VERSION = 'same-fixture-final-schedule-v1'
FIELDS = ('match_id', 'league', 'home_id', 'away_id', 'provider')


def stamp(value):
    value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _causal(method, cutoff_day):
    if not isinstance(method, dict) or method.get('source') != 'API-Football':
        raise ValueError('Provenance de la prévision non vérifiable.')
    cutoff = method.get('training_before')
    if not isinstance(cutoff, str) or date.fromisoformat(cutoff).isoformat() != cutoff or cutoff > cutoff_day:
        raise ValueError('Historique non antérieur au nouvel horaire.')
    trained = method.get('trained_through')
    if trained is not None and date.fromisoformat(trained).isoformat() >= cutoff:
        raise ValueError('Fin d’apprentissage incompatible avec le nouvel horaire.')
    distribution = method.get('distribution', {})
    if distribution.get('type') == 'block_product':
        for component in distribution['components']:
            _causal(component['method'], cutoff_day)


def resolve(entry, fixture, actual, *, status, checked_at):
    """Return an audit for a safe final kickoff correction, else raise.

    This accepts a score-free actual fixture validated by the provider adapter.
    No names are used for identity matching. A changed match order is reported,
    never used to reorder the frozen scenario.
    """
    if status not in ('FT', 'AET', 'PEN'):
        raise ValueError('Horaire modifié : résultat final confirmé nécessaire.')
    if (fixture.get('provider') != 'api-football'
            or any(str(actual.get(key)) != str(fixture.get(key)) for key in FIELDS)):
        raise ValueError('Les identifiants des clubs ou de la compétition ont changé.')
    original, effective = stamp(fixture['timestamp']), stamp(actual['timestamp'])
    created, checked = stamp(entry['created_at']), stamp(checked_at)
    if original == effective:
        raise ValueError('Aucune correction horaire à rapprocher.')
    if created >= min(original, effective) or checked < effective:
        raise ValueError('Prévision ou résultat incompatible avec le nouvel horaire.')
    if not entry.get('methods'):
        raise ValueError('Provenance de la prévision absente.')
    for method in entry['methods'].values():
        _causal(method, min(original, effective).date().isoformat())
    return dict(version=VERSION, match_id=str(fixture['match_id']),
                original_timestamp=fixture['timestamp'], effective_timestamp=actual['timestamp'],
                provider='api-football', league=str(fixture['league']),
                home_id=str(fixture['home_id']), away_id=str(fixture['away_id']),
                raw_status=status, forecast_created_at=entry['created_at'],
                scenario_order='recorded_order_unchanged')


def observation_kickoff(entry, fixture, observation):
    """Validate the saved correction before a reliability report can use it."""
    audit = observation.get('schedule_resolution')
    if audit is None:
        return stamp(fixture['timestamp'])
    if not isinstance(audit, dict) or audit.get('version') != VERSION:
        raise ValueError('Correction horaire inconnue.')
    actual = dict(fixture, timestamp=audit.get('effective_timestamp'))
    expected = resolve(entry, fixture, actual, status=audit.get('raw_status'), checked_at=entry.get('checked_at'))
    if audit != expected:
        raise ValueError('Correction horaire incohérente.')
    return stamp(actual['timestamp'])
