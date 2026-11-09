"""Display independently dated recent histories without inventing joint matches."""
from datetime import datetime


def sequence(values):
    return ' → '.join(values)


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
