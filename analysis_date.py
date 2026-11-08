"""One selected Kinshasa match day, independent from the real prediction clock."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import streamlit as st

ZONE = ZoneInfo('Africa/Kinshasa')
_DATETIME_TYPE = datetime
DAY_KEY = 'analysis_selected_day'
WIDGET_KEY = 'analysis_day_widget'


def local_today(now=None):
    if isinstance(now, _DATETIME_TYPE):
        if now.tzinfo is None:
            raise ValueError('L’heure actuelle doit préciser son fuseau.')
        return now.astimezone(ZONE).date()
    if isinstance(now, date):
        return now
    return datetime.now(ZONE).date()


def _date(value):
    if isinstance(value, _DATETIME_TYPE):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def get_selected_day(now=None):
    """Default follows midnight; an explicit date stays selected across pages."""
    today = local_today(now)
    selected = _date(st.session_state.get(DAY_KEY))
    if (selected is None or not date(1900, 1, 1) <= selected <= date(2100, 12, 31)
            or st.session_state.get('analysis_follow_today', False)):
        selected = today
    return selected


def set_selected_day(day, *, follow_today=False):
    selected = _date(day)
    if selected is None or not date(1900, 1, 1) <= selected <= date(2100, 12, 31):
        raise ValueError('Choisissez une date valide entre 1900 et 2100.')
    st.session_state[DAY_KEY] = selected
    st.session_state['analysis_follow_today'] = follow_today
    st.session_state[WIDGET_KEY] = selected


def _choose_date():
    set_selected_day(st.session_state.get(WIDGET_KEY))


def _choose_relative_day(offset, *, follow_today=False):
    # Resolve at click time: the browser may have stayed open across midnight.
    set_selected_day(local_today() + timedelta(days=offset), follow_today=follow_today)


def day_refresh_needed(now=None):
    selected = _date(st.session_state.get(DAY_KEY))
    return bool(selected is not None and st.session_state.get('analysis_follow_today', False)
                and selected != local_today(now))


@st.fragment(run_every=60)
def install_date_watchdog():
    # Page fragments may advance independently; refresh the common header too.
    if day_refresh_needed():
        st.rerun(scope='app')


def render_date_selector(now=None):
    today = local_today(now)
    if DAY_KEY not in st.session_state:
        st.session_state['analysis_follow_today'] = True
    selected = get_selected_day(now)
    st.session_state[DAY_KEY] = selected
    # Synchronize before creating the widget, including at local midnight.
    st.session_state[WIDGET_KEY] = selected
    with st.container(horizontal=True, vertical_alignment='bottom'):
        st.date_input('Date des matchs', key=WIDGET_KEY, format='DD/MM/YYYY',
                      min_value=date(1900, 1, 1), max_value=date(2100, 12, 31),
                      on_change=_choose_date, persist_state='session',
                      help='Date commune au calendrier, aux probabilités et aux analyses. Heures de Kinshasa.')
        st.button('Aujourd’hui', key='analysis_today', on_click=_choose_relative_day,
                  args=(0,), kwargs={'follow_today': True})
        st.button('Demain', key='analysis_tomorrow', on_click=_choose_relative_day,
                  args=(1,))
    st.caption(f"Journée sélectionnée : {selected.strftime('%d/%m/%Y')} · Africa/Kinshasa · "
               'Les prévisions sont enregistrées avant le coup d’envoi uniquement.')
    return selected
