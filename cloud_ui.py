"""Streamlit boundaries for the shared durable workspace, including fragments."""
from functools import wraps
from time import monotonic
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import streamlit as st

import cloud_storage


RETRY_SECONDS = 10
_RETRY_DUE = '_cloud_retry_due'
_WATCHDOG = '_cloud_retry_watchdog'


@st.fragment(run_every=RETRY_SECONDS)
def _retry_watchdog():
    """An unconditional call site survives cleanup by an earlier full run."""
    due = st.session_state.get(_RETRY_DUE)
    if due is not None and monotonic() >= due:
        st.session_state.pop(_RETRY_DUE, None)
        st.rerun(scope='app')


def install_retry_watchdog():
    """Call once, at a stable entrypoint position before guarded(page.run)."""
    st.session_state[_WATCHDOG] = True
    _retry_watchdog()


@st.fragment(run_every=RETRY_SECONDS)
def _retry_timer(retry_at):
    """Request a full page run without entering a storage transaction."""
    if monotonic() >= retry_at:
        st.rerun(scope='app')


def _arm_retry(retry_at):
    """Start the next retry only after a potentially slow preview is ready."""
    st.session_state[_RETRY_DUE] = retry_at
    if not st.session_state.get(_WATCHDOG):
        # Standalone guarded fragments/tests retain their retry behavior. The
        # real app installs one persistent timer before entering any page.
        _retry_timer(retry_at)


def _busy_notice(retry_at, retry_key):
    """Keep manual retry available, with optional deferred automatic arming."""
    st.session_state.pop(_RETRY_DUE, None)
    st.info('Synchronisation des données en cours. Les données déjà sauvegardées restent '
            'consultables ci-dessous. La page complète reprendra dès que le stockage sera disponible.')
    st.caption(f'Nouvelle tentative toutes les {RETRY_SECONDS} secondes. '
               'Aucune action de cette page n’a été exécutée pendant cette attente.')
    retry = st.button('Réessayer maintenant', icon=':material/refresh:', key=retry_key)
    if retry:
        # Navigation must run in a full app run, including when this notice
        # was reached from an independently refreshing page fragment.
        st.rerun(scope='app')
    if retry_at is not None:
        _arm_retry(retry_at)


def _read_preview(snapshot):
    """Only explicit paths and pure computations; never existing page bodies."""
    import api_football as api
    import calendar_data
    import high_probability_engine
    import prediction_journal as journal
    now = datetime.now(timezone.utc)
    day = now.astimezone(ZoneInfo('Africa/Kinshasa')).date()
    try:
        raw = api.load_fixtures(snapshot.root/'api_football/fixtures.json')
    except api.ApiFootballError:
        raise cloud_storage.CloudStorageError('Le calendrier sauvegardé est invalide.') from None
    return dict(revision=snapshot.revision, updated_at=snapshot.updated_at, day=day.isoformat(),
                calendar=calendar_data.day_rows(raw, day),
                entries=journal.list_entries(snapshot.root/'api_football/predictions.sqlite3'),
                probabilities=high_probability_engine.scan(raw, now=now))


@st.cache_data(ttl=300, max_entries=4, show_spinner=False)
def _snapshot_preview(identity, day, revision):
    # identity separates credentials/workspaces without putting a DSN in keys;
    # day prevents a cached preview crossing local midnight. The committed head
    # is part of the key: unchanged files must not be downloaded every retry.
    with cloud_storage.committed_snapshot(files=(
            'api_football/fixtures.json', 'api_football/predictions.sqlite3')) as snapshot:
        return _read_preview(snapshot)


def _current_probability_rows(report, minimum, outcome):
    """Drop elapsed kickoffs after the cache, without rereading saved files."""
    import high_probability_engine as engine
    current = datetime.now(timezone.utc)
    upcoming = []
    for row in report['rows']:
        try:
            kickoff = datetime.fromisoformat(row['fixture']['timestamp'].replace('Z', '+00:00'))
            if kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=timezone.utc)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if kickoff > current:
            upcoming.append(row)
    return engine.filter_rows(dict(report, rows=upcoming), minimum, outcome)


def _render_snapshot_preview():
    try:
        preview = _snapshot_preview(cloud_storage.snapshot_identity(),
                                    datetime.now(ZoneInfo('Africa/Kinshasa')).date().isoformat(),
                                    cloud_storage.committed_revision())
    except cloud_storage.CloudStorageError:
        st.caption('L’instantané sauvegardé n’est pas disponible pour cette tentative. '
                   'La reprise automatique reste active.')
        return
    st.subheader('Consultation des données sauvegardées', icon=':material/visibility:')
    st.caption(f"Version {preview['revision']} · Sauvegarde : {preview['updated_at'] or 'date indisponible'} · "
               'Lecture seule ; les scores sont ceux de cette sauvegarde.')
    view = st.segmented_control('Consulter pendant la mise à jour',
            ['Matchs du jour', 'Fortes probabilités', 'Analyses enregistrées'],
            default='Matchs du jour', required=True, key='cloud_snapshot_view')
    if view == 'Matchs du jour':
        rows = preview['calendar']
        st.caption(f"{len(rows)} match(s) pour le {preview['day']} · Heures de Kinshasa")
        query = st.text_input('Rechercher dans le calendrier sauvegardé', key='cloud_snapshot_search')
        if query:
            folded = query.casefold()
            rows = [r for r in rows if folded in ' '.join(str(r[k]) for k in
                    ('home','away','competition','country')).casefold()]
        st.dataframe([{'Heure':datetime.fromisoformat(r['timestamp']).replace(tzinfo=timezone.utc)
                         .astimezone(ZoneInfo('Africa/Kinshasa')).strftime('%H:%M'),
                'Compétition':r['competition'], 'Domicile':r['home'], 'Score':r['score'],
                'Extérieur':r['away'], 'Statut':r['status_label'],
                'Pause':r['halftime_score'], '90 minutes':r['regulation_score']} for r in rows],
                hide_index=True, width='stretch')
    elif view == 'Fortes probabilités':
        outcome = st.selectbox('Résultat recherché dans l’instantané', ['1st','Égal','2nd'],
                               key='cloud_snapshot_outcome')
        minimum = st.number_input('Probabilité minimale (%)', min_value=0, max_value=100,
                                  value=50, key='cloud_snapshot_minimum')
        rows = _current_probability_rows(preview['probabilities'], minimum/100, outcome)
        st.caption('Estimations Clubs calculées sur cette sauvegarde ; aucune nouvelle prévision '
                   'n’est enregistrée et aucun appel API n’est effectué.')
        st.dataframe([{'Compétition':r['fixture'].get('competition', r['fixture']['league']),
                'Domicile':r['fixture']['home'], 'Extérieur':r['fixture']['away'],
                **{label:round(100*r['probabilities'][label],2) for label in ('1st','Égal','2nd')}}
                for r in rows], hide_index=True, width='stretch')
        st.caption(f'{len(rows)} rencontre(s) correspondent au seuil.')
    else:
        entries = sorted(preview['entries'], key=lambda row:row['created_at'], reverse=True)
        labels = {'pending':'En attente','settled':'Terminée','void':'Annulé','unresolved':'À vérifier'}
        st.dataframe([{'Enregistrée le':r['created_at'], 'Matchs':r.get('size',len(r.get('fixtures',[]))),
            'Affiches':' · '.join(f"{f['home']} — {f['away']}" for f in r.get('fixtures',[])),
            'Statut':labels.get(r.get('status'),r.get('status','À vérifier'))} for r in entries],
            hide_index=True, width='stretch')
        if entries:
            index = st.selectbox('Ouvrir une analyse sauvegardée', range(len(entries)),
                    format_func=lambda i:f"{entries[i]['created_at']} · {entries[i].get('size','?')} matchs",
                    key='cloud_snapshot_entry')
            entry = entries[index]
            import scenario_tables
            scenario_tables.render_matrix(entry.get('methods',{}), actual=entry.get('actual'),
                                           key='cloud_snapshot_scenarios')


def guarded(function):
    """Restore one committed workspace and prevent offline writes in cloud mode."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            with cloud_storage.transaction():
                result = function(*args, **kwargs)
            st.session_state.pop(_RETRY_DUE, None)
            return result
        except cloud_storage.CloudStorageBusy:
            # Cancel an old deadline before reading: a cold snapshot can take
            # longer than ten seconds, and an early timer would repeatedly
            # interrupt the download/scan before its cache can be populated.
            _busy_notice(None,
                         f'cloud_retry_{function.__module__}.{function.__qualname__}')
            _render_snapshot_preview()
            _arm_retry(monotonic()+RETRY_SECONDS)
        except cloud_storage.CloudStorageError as error:
            st.error(str(error))
            st.caption('Le stockage partagé est indisponible. Les opérations sont suspendues '
                       'pour conserver les prévisions déjà enregistrées. '
                       'La connexion se configure dans les secrets de l’hébergement.')
        return None
    return wrapped


def render_storage_status():
    """Show only connection metadata; never display or edit credentials."""
    with st.container(border=True):
        st.markdown('#### Conservation des données')
        if not cloud_storage.configured():
            st.warning('Stockage local uniquement. L’historique et les prévisions ne sont '
                       'pas encore conservés dans une base distante.')
            st.caption('Le déploiement gratuit peut perdre ses fichiers locaux. '
                       'Le guide docs/CLOUD_GRATUIT.md décrit la connexion au stockage partagé '
                       'et l’import initial de l’historique existant.')
            return
        try:
            state = cloud_storage.status()
        except cloud_storage.CloudStorageError as error:
            st.error(str(error))
            return
        if not state.get('initialized', bool(state.get('revision'))):
            st.warning('La connexion est configurée ; l’import initial de l’historique reste à effectuer.')
            return
        st.success('Stockage partagé connecté. Les prévisions sont confirmées après leur sauvegarde distante.')
        with st.container(horizontal=True):
            st.metric('Version conservée', state.get('revision', '—'))
            if isinstance(state.get('file_count'), int):
                st.metric('Fichiers conservés', state['file_count'])
            if isinstance(state.get('compressed_bytes'), int):
                st.metric('Données compressées', f"{state['compressed_bytes'] / (1024 * 1024):.1f} Mo")
        if state.get('updated_at'):
            st.caption(f"Dernière sauvegarde : {state['updated_at']}")
        st.caption('Le site et le traitement automatique utilisent le même historique. '
                   'Leur disponibilité reste soumise aux limites des services gratuits.')
