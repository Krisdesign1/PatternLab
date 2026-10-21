from league_pattern_panel import render_leagues
from ux_theme import render_page_header

render_page_header('Explorer les scénarios',
                   'Retrouver les suites observées et les précédents qui les composent.',
                   eyebrow='L’HISTORIQUE DES RÉPÉTITIONS', icon=':material/schema:')
render_leagues([], expert=False, default_source='API-Football')
