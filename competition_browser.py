"""Native, paginated browsing of every competition on the selected day."""
from datetime import datetime, timezone
from html import escape
from math import ceil

import streamlit as st

import competition_overview as overview
from coupon_sizes import validate_size
from scenario_tables import styles_html


ALL = '__all__'  # Same sentinel as calendar_panel.ALL; no calendar side effects.
SORTS = {'matches': 'Matchs du jour · décroissant', 'history': 'Historique exploitable · décroissant',
         'name': 'Nom de compétition · alphabétique'}


def table_html(items, *, size=4, theme='light'):
    """A bounded, escaped display table; IDs remain in native controls only."""
    size = validate_size(size)
    dark = theme == 'dark'
    good, good_bg, wait, wait_bg, neutral, neutral_bg = (
        ('#b6e4cc','#203d34','#ffd39a','#4c3519','#c2cee0','#233149') if dark else
        ('#246546','#e8f5ee','#8a4b08','#fff0df','#4b5c73','#edf1f6'))
    css = f'''<style>
.pl-competition-root .pl-scenario-scroll {{ border:0; border-radius:0; }}
.pl-competition-root .pl-competition-table {{ min-width:860px; }}
.pl-competition-root .pl-competition-table th, .pl-competition-root .pl-competition-table td {{ padding:1.12rem 1rem; text-align:left; }}
.pl-competition-root .pl-competition-table thead th {{ background:transparent; white-space:normal; line-height:1.55; padding-top:.7rem; padding-bottom:.85rem; }}
.pl-competition-root .pl-competition-table thead th:first-child {{ min-width:13rem; }}
.pl-competition-root .pl-competition-table tbody th {{ min-width:13rem; max-width:24rem; font-weight:400; }}
.pl-competition-root .pl-competition-name {{ display:block; font-weight:650; line-height:1.45; overflow-wrap:anywhere; }}
.pl-competition-root .pl-competition-value {{ font-weight:650; font-size:1.05rem; font-variant-numeric:tabular-nums; }}
.pl-competition-root .pl-competition-meta {{ display:block; color:var(--pl-muted); font-size:.875rem; line-height:1.5; margin-top:.32rem; font-weight:400; text-transform:none; letter-spacing:0; }}
.pl-competition-root .pl-competition-badge {{ display:inline-block; padding:.3rem .65rem; border-radius:.45rem; font-size:.875rem; line-height:1.35; font-weight:600; white-space:nowrap; }}
.pl-competition-root .pl-competition-good {{ color:{good}; background:{good_bg}; }}
.pl-competition-root .pl-competition-wait {{ color:{wait}; background:{wait_bg}; }}
.pl-competition-root .pl-competition-neutral {{ color:{neutral}; background:{neutral_bg}; }}
</style>'''
    body=[]
    for item in items:
        name, country = escape(str(item['competition'])), escape(str(item['country']))
        reason = escape(str(item.get('preparation_reason') or ''), quote=True)
        history = item.get('history_matches')
        groups = item.get('history_groups')
        history_value = '—' if history is None else escape(str(history))
        history_meta = 'Historique à vérifier' if groups is None else f'{groups} groupes complets'
        missing = item.get('missing_matches')
        state = 'Prêt' if item.get('history_ready') else 'À vérifier' if missing is None else 'À compléter'
        css_state = 'good' if item.get('history_ready') else 'neutral' if missing is None else 'wait'
        prep_meta = (f'Pour {size} matchs' if item.get('history_ready') else
                     'Données à vérifier' if missing is None else f'Encore {missing} résultat(s)')
        historical = item.get('historical_day', False)
        available = item.get('available', 0)
        enough = item.get('fixture_ready', False)
        available_label = ('Simulation possible' if historical else 'Disponibles') if enough else 'Pas assez d’affiches'
        available_meta = f'{available}/{size} matchs ' + ('terminés' if historical else 'à venir')
        body.append('<tr>'
            f'<th scope="row"><strong class="pl-competition-name">{name}</strong><span class="pl-competition-meta">{country}</span></th>'
            f'<td><span class="pl-competition-value">{escape(str(item["matches"]))}</span></td>'
            f'<td><span class="pl-competition-value">{escape(str(item["upcoming"]))}</span></td>'
            f'<td><span class="pl-competition-value">{history_value}</span><span class="pl-competition-meta">{escape(history_meta)}</span></td>'
            f'<td title="{reason}"><span class="pl-competition-badge pl-competition-{css_state}">{state}</span>'
            f'<span class="pl-competition-meta">{escape(prep_meta)}</span></td>'
            f'<td><span class="pl-competition-badge pl-competition-{"good" if enough else "neutral"}">{available_label}</span>'
            f'<span class="pl-competition-meta">{escape(available_meta)}</span></td></tr>')
    return (styles_html(theme)+css+'<div class="pl-scenario-root pl-competition-root">'
            '<div class="pl-scenario-scroll" tabindex="0" role="region" aria-label="Compétitions de la journée">'
            '<table class="pl-scenario-table pl-competition-table">'
            '<caption class="pl-scenario-sr">Compétitions, matchs et préparation de l’historique</caption>'
            '<thead><tr><th scope="col">Compétition</th><th scope="col">Matchs du jour</th>'
            '<th scope="col">À venir</th><th scope="col">Historique exploitable</th>'
            f'<th scope="col">Préparation<span class="pl-competition-meta">Groupe de {size} matchs</span></th>'
            '<th scope="col">Affiches disponibles</th></tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div></div>')


@st.cache_data(ttl=60, max_entries=4, show_spinner=False)
def _overview(snapshot, rows, day, size, minute, _now):
    return overview.build(snapshot, rows, day, size=size, now=_now)


def sorted_options(snapshot, rows, day, *, size=4, sort='matches', now=None):
    """Return reliable league IDs in the same order as the browser's rows."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError('L’heure actuelle doit préciser son fuseau.')
    current = current.astimezone(timezone.utc)
    values = _overview(snapshot, rows, day, size, current.strftime('%Y-%m-%dT%H:%M'), current)
    return [item['league'] for item in overview.select(values, sort=sort)]


def render(snapshot, rows, day, *, selected_league=ALL, key='competition_browser'):
    """Return a league ID only when its explicit analysis button is pressed."""
    st.subheader('Choisir une compétition', icon=':material/trophy:')
    st.caption(f"Compétitions du {day.strftime('%d/%m/%Y')} · Horaires Africa/Kinshasa. "
               'Les filtres ci-dessous modifient uniquement cette liste.')
    with st.container(horizontal=True):
        size = st.selectbox('Préparation pour un groupe de', list(range(2,21)), index=2,
                             format_func=lambda value:f'{value} matchs', key=f'{key}_size')
        sort = st.selectbox('Trier les compétitions', list(SORTS), format_func=SORTS.get, key=f'{key}_sort')
        per_page = st.selectbox('Compétitions par page', [10,20,50], key=f'{key}_page_size')
    current = datetime.now(timezone.utc)
    try:
        items = _overview(snapshot, rows, day, size, current.strftime('%Y-%m-%dT%H:%M'), current)
    except (ValueError, OSError) as error:
        st.warning('La préparation locale des compétitions est indisponible : '+str(error))
        return None
    countries = sorted({item['country'] for item in items}, key=str.casefold)
    with st.container(horizontal=True):
        country = st.selectbox('Pays des compétitions', [None]+countries,
                               format_func=lambda value:value or 'Tous les pays', key=f'{key}_country')
        query = st.text_input('Rechercher une compétition', placeholder='Nom ou pays', key=f'{key}_query')
    filtered = overview.select(items, country=country, query=query, sort=sort)
    context = (day.isoformat(), size, sort, per_page, country, query.strip().casefold())
    if st.session_state.get(f'{key}_context') != context:
        st.session_state[f'{key}_context'] = context
        st.session_state[f'{key}_page'] = 1
    pages = max(1, ceil(len(filtered)/per_page))
    page_key = f'{key}_page'
    if st.session_state.get(page_key, 1) > pages:
        st.session_state[page_key] = 1
    page = st.selectbox('Page des compétitions', list(range(1,pages+1)),
                        format_func=lambda value:f'Page {value} sur {pages}', key=page_key)
    visible = filtered[(page-1)*per_page:page*per_page]
    st.caption(f'{len(filtered)} compétition(s) trouvée(s) sur {len(items)} · '
               f'Page {page}/{pages} · La taille de {size} matchs sert uniquement à consulter la préparation ; '
               'elle ne sélectionne pas vos matchs.')
    if not visible:
        st.info('Aucune compétition ne correspond à ces filtres pour la journée choisie.')
        return None
    st.html(table_html(visible, size=size, theme='dark' if st.context.theme.type == 'dark' else 'light'))
    st.caption('La préparation historique concerne Clubs + répétitions, pas la précision du modèle. '
               'Elle reste distincte du nombre d’affiches disponibles. CatBoost et contexte sont vérifiés au calcul. '
               'Les résultats du jour UTC actuel sont exclus de l’historique, même pour une date future.')
    options = [item['league'] for item in visible]
    indexed = {item['league']:item for item in visible}
    selection_key = f'{key}_selection'
    if st.session_state.get(selection_key) not in options:
        st.session_state[selection_key] = str(selected_league) if str(selected_league) in options else options[0]
    with st.container(horizontal=True, vertical_alignment='bottom'):
        selected = st.selectbox('Compétition à analyser sur cette page', options,
            format_func=lambda value:f"{indexed[value]['country']} · {indexed[value]['competition']}", key=selection_key)
        action = st.button('Analyser cette compétition', icon=':material/arrow_forward:', type='primary', key=f'{key}_analyze')
    if action and selected in indexed:
        return selected
    return None
