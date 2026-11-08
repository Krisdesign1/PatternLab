"""Local import quality review; no API requests, forecasts or model fitting."""
import table_views
import streamlit as st
import admin_access
import api_football as api
import fixture_import_policy as policy


def _scores(raw, period):
    score = raw.get('score')
    values = score.get(period) if isinstance(score, dict) else None
    values = values if isinstance(values, dict) else {}
    return ('—' if any(type(values.get(side)) is not int for side in ('home', 'away'))
            else f"{values['home']}-{values['away']}")


def _team(raw, side):
    teams = raw.get('teams')
    team = teams.get(side) if isinstance(teams, dict) else None
    return team.get('name') if isinstance(team, dict) else '—'


def _reviewable(raw):
    try:
        if policy.identity(raw) is None:
            return False
        if policy.status(raw) in policy.FINISHED:
            api.normalize_fixture(policy.provider_raw(raw))
        return policy.status(raw) in policy.FINISHED | policy.ADMINISTRATIVE | policy.EXPLICIT_NONFINAL
    except (ValueError, KeyError, TypeError):
        return False


def render_import_review():
    box = st.expander('Corrections de données à examiner', key='import_quality_open', on_change='rerun')
    if not box.open:
        return
    with box:
        try:
            quarantined = api.import_quarantines(api.STORE_PATH)
        except (api.ApiFootballError, OSError, ValueError):
            st.warning('Les corrections conservées ne peuvent pas être consultées.')
            return
        st.caption('Un relevé contradictoire est conservé avec la version précédente. '
                   'La rencontre est écartée des nouveaux calculs tant que son conflit n’est pas résolu. '
                   'La consultation ne modifie pas les prévisions déjà enregistrées et ne contacte pas l’API.')
        if not quarantined:
            st.info('Aucune correction d’import en quarantaine dans ce stockage.')
            return
        indexed = {str(raw['fixture']['id']): raw for raw in quarantined}
        mid = st.selectbox('Rencontre à examiner', list(indexed), key='import_quality_match',
            format_func=lambda key: f"{_team(indexed[key], 'home')} — {_team(indexed[key], 'away')} · {key}")
        current = indexed[mid]
        st.warning(policy.quality_error(current))
        try:
            records = api.load_import_observations(api.STORE_PATH, ids=[mid])
        except (api.ApiFootballError, OSError, ValueError):
            st.warning('L’historique de réception ne peut pas être vérifié ; aucune revue n’est possible.')
            return
        if not records:
            st.warning('L’observation d’origine n’est pas conservée ; aucune version n’est choisie automatiquement.')
            return
        latest = records[-1]
        table_views.dataframe([{'Enregistrement local UTC': record['observed_at_utc'],
                       'Décision d’import': record['decision'],
                       'Statut reçu': policy.status(record['received_raw']) or '—',
                       'Pause reçue': _scores(record['received_raw'], 'halftime'),
                       '90 minutes reçues': _scores(record['received_raw'], 'fulltime'),
                       'Motif de revue': (record.get('review') or {}).get('reason', '')}
                      for record in records], hide_index=True, width='stretch', key='import_quality_observations', table_id='fixture_import_panel:render_import_review:dataframe:1')
        received, prior = latest['received_raw'], policy.provider_raw(current)
        table_views.dataframe([{'Version': label, 'Statut': policy.status(raw) or '—',
                       'Pause': _scores(raw, 'halftime'), '90 minutes': _scores(raw, 'fulltime'),
                       'Domicile': _team(raw, 'home'), 'Extérieur': _team(raw, 'away')}
                      for label, raw in (('Dernière réponse reçue', received), ('Version conservée', prior))],
                     hide_index=True, width='stretch', key='import_quality_versions', table_id='fixture_import_panel:render_import_review:dataframe:2')
        st.caption('Ces dates indiquent l’enregistrement local des observations, pas les heures historiques de publication '
                   'du fournisseur. Une version récupérée au début du suivi ne reçoit pas de date passée inventée.')
        reason = st.text_input('Motif de la version retenue', key=f'import_quality_reason_{latest["id"]}', max_chars=500)
        owner = admin_access.can_admin()
        with st.container(horizontal=True):
            for choice, label, raw in (('received', 'Retenir la réponse reçue', received),
                                       ('prior', 'Rétablir la version conservée', prior)):
                if st.button(label, key=f'import_quality_choose_{choice}',
                             disabled=not owner or len(reason.strip()) < 3 or not _reviewable(raw)):
                    admin_access.require_admin()
                    try:
                        api.review_import(mid, path=api.STORE_PATH, choice=choice,
                                          expected_observation=latest['id'], reason=reason)
                    except (api.ApiFootballError, OSError, ValueError):
                        st.warning('La revue ne peut pas être appliquée ou une nouvelle réponse est arrivée. '
                                   'Relisez les versions avant de choisir.')
                    else:
                        st.success('Version retenue et motif conservé. Aucun modèle n’a été entraîné.')
                        st.rerun()
        st.caption('Un score manquant ne peut pas être validé comme résultat complet. '
                   'Les résultats du journal suivront leur rapprochement habituel ; cette revue ne réécrit pas les prévisions.')
