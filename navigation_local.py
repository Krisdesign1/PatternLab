"""Five workflow entries, with existing page URLs kept as secondary views.

This module only routes pages. Opening a section never imports or executes the
other panels in that section, which keeps navigation cheap on a cold start.
"""
import streamlit as st


PAGES = (
    ('ui_pages/analyze.py', 'Aujourd’hui', ':material/calendar_month:', True, True),
    ('ui_pages/journal.py', 'Mes analyses', ':material/history_edu:', False, True),
    ('ui_pages/daily_evaluation.py', 'Bilan et preuves', ':material/fact_check:', False, True),
    ('ui_pages/explore.py', 'Explorer les scénarios', ':material/schema:', False, True),
    ('ui_pages/settings.py', 'Pilotage', ':material/tune:', False, True),
    ('ui_pages/high_probabilities.py', 'Fortes probabilités', ':material/trending_up:', False, False),
    ('ui_pages/daily.py', 'Analyses du jour', ':material/stars:', False, False),
    ('ui_pages/selection_study.py', 'Comparatif de sélection', ':material/science:', False, False),
    ('ui_pages/automation.py', 'Automatisation', ':material/schedule:', False, False),
    ('ui_pages/models.py', 'Modèles et validation', ':material/model_training:', False, False),
)

# The empty pathname is the original default page. Every other route is still
# inferred from its original filename, preserving bookmarks and switch_page().
WORKFLOWS = (
    ('today', 'Aujourd’hui', (
        ('', 'Choisir mes matchs', 'ui_pages/analyze.py'),
        ('high_probabilities', 'Fortes probabilités', 'ui_pages/high_probabilities.py'),
        ('daily', 'Analyses proposées', 'ui_pages/daily.py'),
    )),
    ('evidence', 'Bilan et preuves', (
        ('daily_evaluation', 'Bilan des analyses', 'ui_pages/daily_evaluation.py'),
        ('selection_study', 'Étude de sélection', 'ui_pages/selection_study.py'),
    )),
    ('manage', 'Pilotage', (
        ('settings', 'Données et sauvegardes', 'ui_pages/settings.py'),
        ('automation', 'Automatisation', 'ui_pages/automation.py'),
        ('models', 'Modèles et validation', 'ui_pages/models.py'),
    )),
)


def pages():
    """Register all ten stable routes while showing five workflow entries."""
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
        if st.session_state.get(route_key) != page.url_path:
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
