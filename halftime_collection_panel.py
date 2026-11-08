"""Lightweight, read-only progress display for the local halftime collection."""
import table_views
from datetime import datetime, timezone
from math import ceil
from zoneinfo import ZoneInfo

import streamlit as st

import halftime_collection_status as status_source


ZONE = ZoneInfo('Africa/Kinshasa')
STATES = {
    'not_started': 'Collecte non commencée',
    'collecting': 'Collecte en cours',
    'paused': 'Collecte en pause',
    'inactive': 'Sans activité récente',
    'complete': 'Collecte terminée',
    'needs_attention': 'Collecte à vérifier',
    'unavailable': 'Suivi temporairement indisponible',
}


@st.cache_data(ttl=5, max_entries=4, show_spinner=False)
def _load_status(root=None):
    return status_source.load_status(root=root)


@st.cache_data(ttl=15, max_entries=1, show_spinner=False)
def _load_compact_status(root=None):
    return status_source.load_status(root=root, compact=True)


def _number(value):
    return f'{int(value or 0):,}'.replace(',', '\u202f')


def _local(value, *, seconds=False):
    if value is None:
        return '—'
    try:
        if isinstance(value, (int, float)):
            stamp = datetime.fromtimestamp(value, timezone.utc)
        elif isinstance(value, datetime):
            stamp = value
        else:
            stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(ZONE).strftime('%d/%m à %H:%M:%S' if seconds else '%d/%m à %H:%M')
    except (TypeError, ValueError, OverflowError):
        return '—'


def _duration(seconds):
    minutes = max(1, ceil(float(seconds) / 60))
    if minutes < 60:
        return f'environ {minutes} min'
    hours, rest = divmod(minutes, 60)
    return f'environ {hours} h {rest:02d}' if rest else f'environ {hours} h'


def _progress(view):
    total = int(view.get('total') or 0)
    resolved = int(view.get('resolved') or 0)
    return max(0., min(1., resolved / total)) if total else 0.


def _eta(view, *, compact=False):
    # Old estimates must disappear immediately after pause/inactivity.
    if view.get('status') != 'collecting' or view.get('eta_seconds') is None:
        return
    duration = _duration(view['eta_seconds'])
    finish = _local(view.get('estimated_finish_utc'))
    if compact:
        st.caption(f'Reste {duration} · fin estimée {finish}')
    else:
        st.info(f'Fin estimée : **{finish}**, heure de Kinshasa · {duration}.',
                icon=':material/schedule:')
        st.caption('Estimation fondée sur les derniers appels. Elle peut changer avec le réseau, '
                   'la veille du Mac et les quotas API partagés.')


def _completion(view, root):
    key = 'halftime_collection_completion:' + str(root or 'default')
    complete = view.get('status') == 'complete'
    if complete and not st.session_state.get(key, False):
        st.toast('Collecte des statistiques terminée.', icon=':material/check_circle:')
    st.session_state[key] = complete


def _comparison(view):
    comparison = view.get('comparison') or {}
    stage = comparison.get('stage', 'not_prepared')
    st.subheader('Et le comparatif des modèles ?', icon=':material/compare_arrows:')
    if stage == 'results_available':
        st.success('Une évaluation historique est disponible. La collecte et la comparaison sont des étapes distinctes.')
    elif stage == 'collection_ready':
        st.info('Les données du comparatif sont prêtes. La comparaison reste à exécuter.')
    elif stage == 'snapshot_waiting':
        st.info('Collecte terminée. Un nouvel instantané complet doit être préparé avant la comparaison.')
    elif stage == 'waiting_collection':
        st.info('Le comparatif attend la fin de la collecte sur les périodes prévues.')
    else:
        st.info('Aucun comparatif préparé n’a été trouvé pour cette collecte.')
    count = comparison.get('prepared_examples')
    if count is not None:
        st.caption(f"{_number(count)} profils dans le dernier instantané préparé. "
                   'Cet instantané reste figé pendant que la collecte avance.')
    if comparison.get('detail'):
        st.caption(comparison['detail'])
    if stage != 'results_available':
        st.caption('Une collecte terminée ne signifie pas que le modèle est entraîné ou que ses prédictions se sont améliorées.')


@st.fragment(run_every='15s', key='halftime_collection_compact')
def render_compact(root=None):
    """Compact sidebar card; importing/rendering never starts collection."""
    view = _load_compact_status(root)
    if not view.get('exists'):
        return
    with st.container(border=True):
        st.markdown('**Statistiques par mi-temps**')
        state = view.get('status', 'unavailable')
        st.caption(STATES.get(state, STATES['unavailable']))
        if state != 'unavailable':
            st.progress(_progress(view), text=f"{_number(view.get('resolved'))} / {_number(view.get('total'))} réponses")
            if state == 'complete':
                st.caption('Toutes les réponses prévues sont enregistrées.')
            else:
                st.caption(f"{_number(view.get('remaining'))} matchs restant à interroger")
            _eta(view, compact=True)
            _completion(view, root)
        st.page_link('ui_pages/collection.py', label='Voir la progression',
                     icon=':material/downloading:', width='stretch')


@st.fragment(run_every='15s', key='halftime_collection_details')
def _render_details(root=None):
    st.button('Actualiser le suivi', icon=':material/refresh:', key='halftime_collection_refresh',
              on_click=_load_status.clear)
    view = _load_status(root)
    state = view.get('status', 'unavailable')
    if not view.get('exists'):
        st.info('Aucune collecte de statistiques par mi-temps n’est préparée sur ce Mac.')
        return
    if state == 'unavailable':
        st.warning('Le suivi local est temporairement indisponible. Réessayez dans quelques instants.')
        return
    _completion(view, root)
    with st.container(border=True):
        if state == 'complete':
            st.success('Collecte terminée : toutes les réponses prévues sont enregistrées.', icon=':material/check_circle:')
        elif state == 'paused':
            st.warning('Collecte en pause : attente avant de reprendre les appels API.', icon=':material/pause_circle:')
            if view.get('provider_pause_until'):
                st.caption(f"Reprise autorisée à partir du {_local(view['provider_pause_until'])}, heure de Kinshasa.")
        elif state == 'inactive':
            st.warning('Aucune activité récente détectée. Le processus peut être arrêté ou le Mac en veille.', icon=':material/pause_circle:')
        elif state == 'needs_attention':
            st.warning('La collecte demande une vérification : des requêtes restent sans réponse exploitable.', icon=':material/info:')
        else:
            st.markdown(f"**{STATES.get(state, STATES['unavailable'])}**")
        progress = _progress(view)
        st.progress(progress, text=f"{progress:.1%} · {_number(view.get('resolved'))} réponses sur {_number(view.get('total'))}".replace('.', ','))
        with st.container(horizontal=True):
            st.metric('Réponses enregistrées', _number(view.get('resolved')), border=True)
            st.metric('Matchs restants', _number(view.get('remaining')), border=True)
            st.metric('Tirs complets par mi-temps', _number(view.get('ready')), border=True,
                      help='Tirs totaux et cadrés des deux équipes dans chacune des deux périodes. Les xG peuvent rester absents.')
        _eta(view)
        if view.get('eta_limited_by_quota'):
            st.caption('Estimation de fin suspendue : le quota API restant ne suffit pas à terminer la file aujourd’hui.')
        if view.get('rate_per_hour') is not None and state == 'collecting':
            st.caption(f"Rythme récent : environ {_number(view['rate_per_hour'])} matchs / heure.")
        st.caption(f"Dernière activité : {_local(view.get('last_activity_utc'), seconds=True)} · heure de Kinshasa.")
    if view.get('per_year'):
        st.subheader('Avancement par période')
        table_views.dataframe([{'Année des matchs': str(row['year']),
                       'Réponses enregistrées': row['resolved'],
                       'Matchs prévus': row['total'],
                       'Restants': row['remaining'],
                       'Tirs complets': row['ready']}
                      for row in view['per_year']], hide_index=True, width='stretch',
                     key='halftime_collection_years', table_id='halftime_collection_panel:_render_details:dataframe:1')
    st.caption(f"{_number(view.get('empty'))} réponses vides · {_number(view.get('partial'))} jeux partiels · "
               f"{_number(view.get('invalid'))} structures mises à l’écart · {_number(view.get('retry_errors'))} erreurs à reprendre.")
    st.caption('Une réponse vide est une absence de statistiques chez le fournisseur, pas un échec de prédiction. '
               'Les données partielles et les valeurs manquantes restent identifiées.')
    st.caption('Actualisation automatique toutes les 15 secondes tant que cette page reste ouverte. '
               'Ce suivi lit uniquement les données locales ; il ne lance aucun appel API ni entraînement.')
    _comparison(view)


def render_collection(root=None):
    st.title('Progression de la collecte', anchor=False)
    st.caption('Suivi des statistiques de mi-temps et de la préparation du comparatif.')
    st.caption('Périmètre du comparatif : Premier League, Championship, Ligue 1, Bundesliga, Liga, '
               'Serie A italienne et brésilienne · saisons API 2024–2026 · rencontres jouées '
               'du 1er janvier 2024 au 25 octobre 2026 inclus, en UTC.')
    _render_details(root)
