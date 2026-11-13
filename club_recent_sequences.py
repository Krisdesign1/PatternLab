"""Display independently dated recent histories without inventing joint matches."""
from datetime import datetime

from halftime_domain import CLASS_ORDER


def sequence(values):
    return ' → '.join(values)


def role_leaders(profile, side):
    percentages = (profile or {}).get(side + '_percentages', {})
    maximum = max((percentages[result] for result in CLASS_ORDER
                   if percentages.get(result) is not None), default=0)
    return [result for result in CLASS_ORDER if maximum > 0 and percentages.get(result) == maximum]


def role_columns(home_profile, away_profile):
    """Same club twice for all-club rows; opponent clubs for calendar rows."""
    home_profile, away_profile = home_profile or {}, away_profile or {}
    home = home_profile.get('recent_home_results', [])
    away = away_profile.get('recent_away_results', [])
    dates = lambda profile, side: ' → '.join(
        datetime.fromisoformat(value).strftime('%d/%m/%Y %H:%M')
        for value in profile.get('recent_' + side + '_dates', []))
    return {
        'Récents domicile (ancien → récent)': sequence(home),
        'Récents extérieur (ancien → récent)': sequence(away),
        'Effectif récent domicile': len(home), 'Effectif récent extérieur': len(away),
        'Dates des récents domicile (Kinshasa)': dates(home_profile, 'home'),
        'Dates des récents extérieur (Kinshasa)': dates(away_profile, 'away'),
        'ID matchs récents domicile': ' → '.join(home_profile.get('recent_home_ids', [])),
        'ID matchs récents extérieur': ' → '.join(away_profile.get('recent_away_ids', [])),
    }


def comparison_role_columns(before_profile, after_profile, side, show_after=True):
    """Compare one actual venue role for the same club and competition.

    Callers join profiles by exact team/competition IDs. Percentages remain
    unrounded in data/exports; deltas compare the one-decimal displayed values
    so they agree with the visible before/after cells. Missing history never
    becomes a measured zero. Hiding the after columns cannot alter the inputs.
    """
    if side not in ('home', 'away'):
        raise ValueError('Le rôle de comparaison doit être home ou away.')
    label = 'Domicile' if side == 'home' else 'Extérieur'
    venue = label.lower()
    columns = {}
    phases = [('avant', before_profile)]
    if show_after:
        phases.append(('après', after_profile))
    for phase, profile in phases:
        profile_data = profile or {}
        values = profile_data.get('recent_' + side + '_results', [])
        percentages = profile_data.get(side + '_percentages', {})
        columns[f'Tendance {venue} {phase}'] = role_leaders(profile, side)
        columns[f'Matchs historiques {venue} {phase}'] = profile_data.get(side + '_matches', 0) if profile is not None else None
        columns.update({f'{label} · {result} {phase} (%)': percentages.get(result) for result in CLASS_ORDER})
        columns[f'Récents {venue} {phase} (ancien → récent)'] = sequence(values)
        columns[f'Effectif récent {venue} {phase}'] = len(values)
        columns[f'Dates des récents {venue} {phase} (Kinshasa)'] = ' → '.join(
            datetime.fromisoformat(value).strftime('%d/%m/%Y %H:%M')
            for value in profile_data.get('recent_' + side + '_dates', []))
        columns[f'ID matchs récents {venue} {phase}'] = ' → '.join(
            profile_data.get('recent_' + side + '_ids', []))
    if show_after:
        for result in CLASS_ORDER:
            before = columns[f'{label} · {result} avant (%)']
            after = columns[f'{label} · {result} après (%)']
            columns[f'{label} · {result} écart (points)'] = (
                round(float(f'{after:.1f}') - float(f'{before:.1f}'), 1)
                if before is not None and after is not None else None)
    # Keep each before/after pair adjacent in the main table; provenance follows
    # the comparisons so dates and identifiers do not separate the percentages.
    order = [f'Tendance {venue} {phase}' for phase, _ in phases]
    order.extend(f'Matchs historiques {venue} {phase}' for phase, _ in phases)
    for result in CLASS_ORDER:
        order.extend(f'{label} · {result} {phase} (%)' for phase, _ in phases)
        if show_after:
            order.append(f'{label} · {result} écart (points)')
    order.extend(f'Récents {venue} {phase} (ancien → récent)' for phase, _ in phases)
    return {name: columns[name] for name in order} | {
        name: value for name, value in columns.items() if name not in order}
