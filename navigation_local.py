"""Five workflow entries, with existing page URLs kept as secondary views.

This module only routes pages. Opening a section never imports or executes the
other panels in that section, which keeps navigation cheap on a cold start.
"""
import streamlit as st


PAGES = (
    ('ui_pages/analyze.py', 'Matchs', ':material/calendar_month:', True, True),
    ('ui_pages/journal.py', 'Mes analyses', ':material/history_edu:', False, True),
    ('ui_pages/daily_evaluation.py', 'Bilan et preuves', ':material/fact_check:', False, True),
    ('ui_pages/explore.py', 'Explorer les scénarios', ':material/schema:', False, True),
    ('ui_pages/settings.py', 'Pilotage', ':material/tune:', False, True),
    ('ui_pages/high_probabilities.py', 'Fortes probabilités', ':material/trending_up:', False, False),
    ('ui_pages/competition_trends.py', 'Tendances des compétitions', ':material/bar_chart:', False, False),
    ('ui_pages/club_trends.py', 'Tendances des clubs', ':material/groups:', False, False),
    ('ui_pages/argumented_selection.py', 'Sélection argumentée', ':material/checklist:', False, False),
    ('ui_pages/daily.py', 'Analyses du jour', ':material/stars:', False, False),
    ('ui_pages/selection_study.py', 'Comparatif de sélection', ':material/science:', False, False),
    ('ui_pages/bet_simulation.py', 'Simulation de paris', ':material/calculate:', False, False),
    ('ui_pages/automation.py', 'Automatisation', ':material/schedule:', False, False),
    ('ui_pages/models.py', 'Modèles et validation', ':material/model_training:', False, False),
    ('ui_pages/collection.py', 'Progression de la collecte', ':material/downloading:', False, False),
    ('ui_pages/acceptance.py', 'État de réception', ':material/fact_check:', False, False),
)

# The empty pathname is the original default page. Every other route is still
# inferred from its original filename, preserving bookmarks and switch_page().
WORKFLOWS = (
    ('today', 'Matchs et analyses', (
        ('', 'Choisir mes matchs', 'ui_pages/analyze.py'),
        ('high_probabilities', 'Fortes probabilités', 'ui_pages/high_probabilities.py'),
        ('competition_trends', 'Tendances des compétitions', 'ui_pages/competition_trends.py'),
        ('club_trends', 'Tendances des clubs', 'ui_pages/club_trends.py'),
        ('argumented_selection', 'Sélection argumentée', 'ui_pages/argumented_selection.py'),
        ('daily', 'Analyses proposées', 'ui_pages/daily.py'),
    )),
    ('evidence', 'Bilan et preuves', (
        ('daily_evaluation', 'Bilan des analyses', 'ui_pages/daily_evaluation.py'),
        ('bet_simulation', 'Simulation de paris', 'ui_pages/bet_simulation.py'),
        ('selection_study', 'Étude de sélection', 'ui_pages/selection_study.py'),
    )),
    ('manage', 'Pilotage', (
        ('settings', 'Données et sauvegardes', 'ui_pages/settings.py'),
        ('automation', 'Automatisation', 'ui_pages/automation.py'),
        ('collection', 'Collecte des statistiques', 'ui_pages/collection.py'),
        ('models', 'Modèles et validation', 'ui_pages/models.py'),
        ('acceptance', 'État de réception', 'ui_pages/acceptance.py'),
    )),
)


def pages():
    """Register stable routes while showing five workflow entries."""
    return [st.Page(path, title=title, icon=icon, default=default,
                    visibility='visible' if visible else 'hidden')
            for path, title, icon, default, visible in PAGES]


def render_workflow_navigation(page):
    """Show only the relevant secondary routes, before any panel loads data."""
    for name, label, views in WORKFLOWS:
        targets = {route: path for route, _label, path in views}
        if page.url_path not in targets:
            continue
        key = f'workflow_{name}'
        # This is URL navigation, not a second source of page state. A bookmark
        # or a panel's switch_page() must select its actual destination here.
        route_key = f'{key}_route'
        # Widgets absent from the previous page are cleaned by Streamlit.
        # The route marker outlives that cleanup, so restore a missing value
        # when returning to the same secondary page from another workflow.
        if (st.session_state.get(route_key) != page.url_path
                or st.session_state.get(key) is None):
            st.session_state[key] = page.url_path
            st.session_state[route_key] = page.url_path
        labels = {route: title for route, title, _path in views}
        selected = st.segmented_control(label, options=list(targets), format_func=labels.get,
                                         key=key, required=True, wrap=True)
        # Switch after this run's st.navigation() has registered the pages.
        # Widget callbacks run earlier and cannot safely resolve hidden routes.
        if selected in targets and selected != page.url_path:
            st.switch_page(targets[selected])
        return
