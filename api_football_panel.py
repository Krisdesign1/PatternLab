"""Explicit API-Football connection, preview and local import controls."""
from datetime import datetime, timezone
import json

import streamlit as st
import cloud_storage

from api_football import (
    ApiFootballClient, ApiFootballError, DATA_DIR, FINISHED_STATUSES,
    STORE_PATH, load_fixtures, load_key, normalize_fixture, save_fixtures,
)

CHECK_PATH = DATA_DIR / 'access_check.json'


def _check_report():
    try:
        report = json.loads(CHECK_PATH.read_text(encoding='utf-8'))
        return report if isinstance(report, dict) else {}
    except (OSError, ValueError):
        return {}


@cloud_storage.persisted
def _save_check(report):
    # Only subscription/quota and a safe error message, never account details.
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CHECK_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


def fixture_preview(fixtures):
    table, usable, excluded = [], 0, []
    for item in fixtures:
        item = item if isinstance(item, dict) else {}
        event = item.get('fixture') or {}
        teams = item.get('teams') or {}
        event = event if isinstance(event, dict) else {}
        teams = teams if isinstance(teams, dict) else {}
        state = event.get('status') or {}
        state = state if isinstance(state, dict) else {}
        league = item.get('league') or {}
        league = league if isinstance(league, dict) else {}
        home, away = teams.get('home') or {}, teams.get('away') or {}
        home = home if isinstance(home, dict) else {}
        away = away if isinstance(away, dict) else {}
        status = state.get('short', '?')
        row = {'Identifiant': event.get('id'), 'Date (UTC)': event.get('date'),
               'Compétition': league.get('name'),
               'Domicile': home.get('name'),
               'Extérieur': away.get('name'), 'Statut': status,
               'Score à la pause': '', 'Score à 90 min': '', 'Résultat': ''}
        if status in FINISHED_STATUSES:
            try:
                match = normalize_fixture(item)
                row.update({'Score à la pause': match['ht'], 'Score à 90 min': match['ft'],
                            'Résultat': match['value']})
                usable += 1
            except ValueError as error:
                excluded.append({'Identifiant': event.get('id'), 'Motif': str(error)})
        table.append(row)
    return table, usable, excluded


def render_api_football():
    st.subheader('Connexion API-Football')
    st.caption('Les appels sont effectués uniquement avec les boutons ci-dessous. '
               'API-Football est l’unique source du calendrier, des scénarios et des modèles.')
    try:
        key = load_key()
    except ApiFootballError as error:
        st.error(str(error))
        return
    if not key:
        st.info('Configure la clé dans .streamlit/secrets.toml, section [api_football], '
                'champ key, puis relance ce panneau.')
        return
    st.caption('Clé locale configurée.')
    client = ApiFootballClient(key)
    report = _check_report()
    if st.button('Vérifier l’accès et le quota', icon=':material/sync:', key='api_check'):
        report = {'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
        try:
            with st.spinner('Vérification du compte…'):
                response = client.status()['response']
            if not isinstance(response, dict):
                raise ApiFootballError('Réponse du compte API-Football invalide.')
            report.update(subscription=response.get('subscription', {}),
                          requests=response.get('requests', {}), errors={})
        except ApiFootballError as error:
            report['errors'] = {'access': str(error).replace(key, '[clé masquée]')}
        try:
            _save_check(report)
        except OSError:
            st.warning('Le contrôle a abouti, mais son rapport local n’a pas pu être enregistré.')
    if report.get('errors'):
        errors = report['errors']
        message = '; '.join(str(value) for value in errors.values()) if isinstance(errors, dict) else str(errors)
        st.warning('Dernier contrôle : ' + message.replace(key, '[clé masquée]'))
        if 'suspend' in message.lower():
            st.markdown('Vérifie ou réactive ton compte sur le '
                        '[tableau de bord API-Football](https://dashboard.api-football.com).')
    elif report.get('subscription'):
        subscription, requests = report['subscription'], report.get('requests') or {}
        if subscription.get('active') is False:
            st.warning('Dernier contrôle : abonnement API-Football inactif. Vérifie le compte fournisseur.')
        else:
            st.success(f"Dernier contrôle : offre {subscription.get('plan', '?')} · "
                       f"{requests.get('current', '?')} / {requests.get('limit_day', '?')} requêtes quotidiennes utilisées.")

    with st.form('api_league_search'):
        search = st.text_input('Nom de la compétition à rechercher', placeholder='Premier League, Ligue 1…',
                               key='api_league_query')
        country = st.text_input('Pays facultatif (nom anglais)', placeholder='France, England…',
                                key='api_country_query')
        searched = st.form_submit_button('Rechercher les compétitions', icon=':material/search:')
    if searched:
        if len(search.strip()) < 3:
            st.warning('Saisis au moins trois caractères pour rechercher une compétition.')
        else:
            try:
                with st.spinner('Recherche des compétitions et saisons…'):
                    payload = client.leagues(search=search.strip(), country=country.strip() or None)
                if not isinstance(payload['response'], list):
                    raise ApiFootballError('Liste de compétitions API-Football invalide.')
                st.session_state['api_leagues'] = payload['response']
                st.session_state.pop('api_fixture_batch', None)
            except ApiFootballError as error:
                st.error(str(error))
    leagues = st.session_state.get('api_leagues', [])
    if searched and not leagues:
        st.info('Aucune compétition trouvée avec cette recherche.')
    if leagues:
        indexed = {str(item['league']['id']): item for item in leagues}
        selected = st.selectbox('Compétition API-Football', list(indexed),
                                format_func=lambda value: f"{indexed[value]['country']['name']} — {indexed[value]['league']['name']}",
                                key='api_league_selected')
        seasons = sorted({entry['year'] for entry in indexed[selected].get('seasons', [])}, reverse=True)
        if seasons:
            season = st.selectbox('Saison (année de début)', seasons, key='api_season_selected')
            st.caption('L’accès aux saisons dépend de l’abonnement. Les scores à la pause '
                       'sont contrôlés avant utilisation dans les analyses.')
            context = (selected, season)
            if st.button('Charger les matchs de cette saison', icon=':material/cloud_download:', key='api_fetch_season'):
                st.session_state.pop('api_fixture_batch', None)
                try:
                    with st.spinner('Chargement des résultats et du calendrier…'):
                        payload = client.fixtures(league=selected, season=season, timezone='UTC')
                    if not isinstance(payload['response'], list):
                        raise ApiFootballError('Liste de matchs API-Football invalide.')
                    if payload.get('paging', {}).get('total', 1) > 1:
                        raise ApiFootballError('La réponse contient plusieurs pages. Import interrompu pour éviter un historique incomplet.')
                    st.session_state['api_fixture_batch'] = {'context': context, 'fixtures': payload['response']}
                except ApiFootballError as error:
                    st.error(str(error))
            batch = st.session_state.get('api_fixture_batch')
            if batch and batch['context'] == context:
                table, usable, excluded = fixture_preview(batch['fixtures'])
                st.write(f"**{len(table)} matchs reçus**, dont **{usable} résultats HT/FT utilisables**.")
                if table:
                    st.dataframe(table[:50], hide_index=True, width='stretch')
                    st.caption('Aperçu limité aux 50 premiers matchs ; l’import conserve tout le lot reçu, y compris le calendrier.')
                    if st.button('Enregistrer ce lot dans PatternLab', icon=':material/save:', key='api_import'):
                        try:
                            count = save_fixtures(batch['fixtures'], season_imports=[
                                {'competition_code': selected, 'league': selected, 'season': season,
                                 'match_ids': [str(row['fixture']['id']) for row in batch['fixtures']]}
                            ])
                            st.success(f'{count} matchs API-Football conservés au total. '
                                       'Retrouvez les précédents dans Explorer les scénarios. '
                                       'La source de l’accueil se choisit dans Données et réglages.')
                        except ApiFootballError as error:
                            st.error(str(error))
                if excluded:
                    st.warning(f'{len(excluded)} match(s) terminé(s) exclus des analyses : scores absents ou incohérents.')
                    st.dataframe(excluded, hide_index=True, width='stretch')

    try:
        stored = load_fixtures()
    except ApiFootballError as error:
        st.error(str(error))
        return
    if stored:
        table, usable, excluded = fixture_preview(stored)
        st.caption(f'{len(stored)} matchs enregistrés localement · {usable} résultats utilisables · '
                   f'{len(excluded)} résultats exclus.')
        with st.expander('Calendrier et résultats API enregistrés'):
            st.dataframe(table, hide_index=True, width='stretch')
            st.download_button('Sauvegarder les matchs API-Football', STORE_PATH.read_bytes(),
                               file_name='api_football_fixtures.json', mime='application/json', key='api_backup')
