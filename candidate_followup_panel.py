"""Lazy local views of dated candidate observations; rendering never writes."""
import table_views
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

import streamlit as st

import api_football as api
import candidate_tracking as tracking
import candidate_followup_report as observations


NAMES = dict(candidate='Candidat', incumbent='CatBoost courant figé',
             clubs='Clubs', frequency='Répétitions seules')


def _signature(root):
    paths = (tracking.database_path(root), observations.snapshot_path(root))
    result = []
    for path in paths:
        for item in (path, path.with_name(path.name+'-wal')):
            try:
                stat = item.stat()
                result.append((str(item), stat.st_mtime_ns, stat.st_size))
            except OSError:
                result.append((str(item), None, None))
    return tuple(result)


@st.cache_data(ttl=60, max_entries=4, show_spinner=False)
def _read(study_id, root, signature, utc_day):
    return (observations.collect(Path(root), study_id=study_id),
            observations.snapshots(Path(root), study_id=study_id))


@st.cache_data(ttl=60, max_entries=4, show_spinner=False)
def _past(snapshot_id, root, signature):
    return observations.load_snapshot(snapshot_id, Path(root))


def _daily_rows(rows):
    return [{'Journée des matchs · Kinshasa': r['day'], 'Groupes': r['groups'],
             'Rencontres distinctes': r['unique_matches'], 'En attente': r['pending'],
             'Évalués': r['settled'], 'À vérifier': r['unresolved'], 'Annulés': r['void'],
             'Avant le coup d’envoi': r['before_kickoff'], 'Hors délai': r['late'],
             'Avance minimale (min)': round(r['min_lead_seconds']/60, 1)
                if r['min_lead_seconds'] is not None else None} for r in rows]


def _weekly_rows(rows):
    return [{'Semaine UTC · lundi': r['week_utc'], 'Groupes': r['groups'],
             'Rencontres distinctes': r['unique_matches'], 'En attente': r['pending'],
             'Évalués': r['settled'], 'À vérifier': r['unresolved'], 'Annulés': r['void'],
             'Semaine calendaire close': r['calendar_closed'],
             'Groupes admissibles aux blocs clos': r['eligible_groups']} for r in rows]


def render(study_id, root=None):
    expander = st.expander('Bilans datés du suivi', icon=':material/event_note:',
                          key=f'candidate_followup_{study_id}', on_change='rerun')
    if not expander.open:
        return
    with expander:
        st.caption('Lecture locale uniquement. Ouvrir ou filtrer ce bilan ne collecte aucun résultat, '
                   'ne calcule aucune nouvelle prévision et ne crée aucun relevé daté.')
        directory = Path(root if root is not None else api.DATA_DIR).resolve()
        try:
            signature = _signature(directory)
            report, saved = _read(study_id, str(directory), signature,
                                  datetime.now(timezone.utc).date().isoformat())
            current = next((value for value in report['studies'] if value['study_id'] == study_id), None)
            if current is None:
                st.info('Aucun bilan disponible pour ce suivi.')
                return
            indexed = {value['snapshot_id']: value for value in saved}
            key = f'candidate_followup_reading_{study_id}'
            choices = ['current', *indexed]
            if st.session_state.get(key) not in choices:
                st.session_state[key] = 'current'
            selected = st.selectbox('Lecture du bilan', choices,
                format_func=lambda value: 'État actuel' if value == 'current' else
                    f"Relevé du {indexed[value]['observed_at']} · {indexed[value]['groups']} groupes",
                key=key)
            as_of, downloaded = report['as_of'], report
            if selected != 'current':
                downloaded = _past(selected, str(directory), signature)
                current, as_of = downloaded['study'], downloaded['as_of']
            st.caption(f'État observé le {as_of} · Réutilisation de cette lecture pendant 60 secondes maximum.')
            notes = list(report.get('warnings', [])) + list(current.get('warnings', []))
            if notes:
                st.caption('Règles de lecture : ' + ' '.join(dict.fromkeys(notes)))
            if not current['implementation_compatible']:
                st.warning('Les moteurs actuels diffèrent des versions de ce suivi. Les observations '
                           'conservées restent visibles ; leur consultation ne réactive pas les prévisions.')
            sizes = [value['size'] for value in current['by_size']]
            if not sizes:
                st.info('Aucune taille de groupe enregistrée dans ce bilan.')
                return
            size_key = f'candidate_followup_size_{study_id}'
            if st.session_state.get(size_key) not in sizes:
                st.session_state[size_key] = 4 if 4 in sizes else sizes[0]
            size = st.segmented_control('Taille du bilan', sizes, required=True, key=size_key)
            scope = next(value for value in current['by_size'] if value['size'] == size)
            with st.container(horizontal=True):
                st.metric('Groupes pour cette taille', sum(scope['counts'].values()))
                st.metric('Rencontres distinctes', scope['unique_matches'])
                st.metric('Blocs clos admissibles', scope['closed_blocks'])
                st.metric('Groupes dans ces blocs', scope['eligible_groups'])
            st.caption('Les rencontres peuvent être partagées entre tailles. Une attente ou une exclusion '
                       'n’est pas une prédiction perdue. Les semaines ouvertes et les dépendances non '
                       'résolues restent hors des comparaisons closes.')
            period = st.segmented_control('Découpage du bilan', ['daily', 'weekly'],
                format_func=lambda value: 'Par journée · Kinshasa' if value == 'daily' else 'Par semaine · UTC',
                default='daily', required=True, key=f'candidate_followup_period_{study_id}')
            rows = [row for row in current[period] if row['size'] == size]
            field = 'day' if period == 'daily' else 'week_utc'
            periods = sorted({row[field] for row in rows}, reverse=True)
            period_key = f'candidate_followup_filter_{study_id}_{period}'
            if st.session_state.get(period_key) not in ['all', *periods]:
                st.session_state[period_key] = 'all'
            chosen = st.selectbox('Période à afficher', ['all', *periods],
                format_func=lambda value: 'Toutes les périodes observées' if value == 'all' else value,
                key=period_key)
            filtered = rows if chosen == 'all' else [row for row in rows if row[field] == chosen]
            if not filtered:
                st.info('Aucune observation pour cette taille et cette période.')
            elif period == 'daily':
                table_views.dataframe(_daily_rows(filtered), hide_index=True, width='stretch',
                             key=f'candidate_followup_daily_{study_id}', table_id='candidate_followup_panel:render:dataframe:1')
            else:
                table_views.dataframe(_weekly_rows(filtered), hide_index=True, width='stretch',
                             key=f'candidate_followup_weekly_{study_id}', table_id='candidate_followup_panel:render:dataframe:2')
                st.caption('Une semaine calendaire close peut encore contenir des résultats manquants. '
                           'L’admissibilité vient des dépendances du bilan global, conservées avant tout filtre.')
                table_views.dataframe([{'Semaine UTC · lundi': row['week_utc'],
                               'Méthode': NAMES.get(metric['method'], metric['method']),
                               'Groupes évalués observés': metric['groups'],
                               'Erreur probabiliste ↓': metric['loss'],
                               'Premier réalisé': metric['top1'], 'Un des trois réalisé': metric['top3']}
                              for row in filtered for metric in row['metrics']],
                             hide_index=True, width='stretch', key=f'candidate_followup_scores_{study_id}', table_id='candidate_followup_panel:render:dataframe:3')
                st.caption('Ces résultats observés décrivent les groupes terminés de la période. '
                           'Ils ne constituent pas un nouvel intervalle de confiance ni une autorisation de remplacement.')
            if saved:
                st.markdown('**Relevés conservés**')
                table_views.dataframe([{'Observé le (UTC)': value['observed_at'],
                               'Jour UTC du relevé': value['utc_day'], 'Groupes': value['groups'],
                               'Rencontres distinctes': value['unique_matches']}
                              for value in saved], hide_index=True, width='stretch',
                             key=f'candidate_followup_saved_{study_id}', table_id='candidate_followup_panel:render:dataframe:4')
                st.caption('Plusieurs relevés peuvent décrire les mêmes rencontres. '
                           'Le nombre de relevés ne multiplie pas les observations indépendantes.')
            else:
                st.info('Aucun relevé daté conservé pour le moment. Le planificateur les enregistrera ; '
                        'cette vue reste en lecture seule.')
            st.download_button('Télécharger ce bilan daté',
                json.dumps(downloaded, ensure_ascii=False, indent=2, allow_nan=False),
                file_name=f'patternlab_bilan_{study_id[:12]}_{selected[:12]}.json',
                mime='application/json', icon=':material/download:', on_click='ignore',
                key=f'candidate_followup_download_{study_id}')
        except (ValueError, OSError, sqlite3.Error, KeyError, TypeError):
            st.warning('Le bilan daté est indisponible ou invalide. Aucun chiffre nul n’est substitué '
                       'aux observations manquantes ; aucune action sur les données n’a été exécutée.')
