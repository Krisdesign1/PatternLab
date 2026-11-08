"""Read-only evidence for the local daily-training procedure."""
import table_views
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import streamlit as st

import api_football as api
import daily_learning_tracking as tracking
import daily_learning_worker as worker


NAMES = {'candidate': 'Candidat réentraîné', 'clubs': 'Clubs actuel',
         'frequency': 'Tendance du championnat', 'incumbent': 'CatBoost courant figé'}
STATES = {'settled': 'Évalué', 'pending': 'En attente', 'unresolved': 'À vérifier', 'void': 'Annulé du suivi'}


def _now():
    return datetime.now(timezone.utc)


def _signature(root):
    base = Path(root)/'daily_learning'
    paths = [base/'status.json', base/'enabled.json', base/'protocol.json']
    paths.extend(sorted(base.glob('candidates/*/*/manifest.json')))
    return tracking.signature(root) + tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size)
                                            for p in paths if p.exists())


@st.cache_data(ttl=60, max_entries=4, show_spinner=False)
def _read(root, signature, minute):
    report = tracking.summary(root=root)
    candidates = []
    # Operational metadata only: do not reload complete training snapshots or
    # native models just to display their names and completion times.
    for path in sorted((Path(root)/'daily_learning').glob('candidates/*/*/manifest.json')):
        value = json.loads(path.read_text(encoding='utf-8'))
        digest = sha256(json.dumps({k:v for k,v in value.items() if k != 'manifest_sha256'},
            sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        if value.get('manifest_sha256') != digest:
            raise ValueError('Métadonnées du candidat altérées.')
        candidates.append(value)
    return dict(report=report, candidates=candidates, enabled=worker.is_enabled(root), status=worker.status(root))


def _percent(value):
    return None if value is None else 100*value


def _metrics(metrics):
    return [{'Méthode': NAMES[name], 'Matchs communs évalués': value['matches'],
        'Favoris corrects': value['correct'], 'Réussite (%)': _percent(value['accuracy']),
        'Erreur probabiliste ↓': value['logloss'], 'Brier ↓': value['brier']}
        for name,value in metrics.items()]


def _forecasts(rows):
    output=[]
    for row in rows:
        fixture=row['fixture']
        value={'Journée (Kinshasa)':row['day'], 'Match':f"{fixture['home']} — {fixture['away']}",
            'Compétition':fixture.get('competition',fixture['league']),'Pays':fixture.get('country'),
            'Coup d’envoi UTC':fixture['timestamp'],
            'Prévision conservée UTC':row['captured_at'], 'Statut':STATES[row['status']],
            'Résultat réel':row.get('actual'), 'Score pause':row.get('ht'), 'Score 90 minutes':row.get('ft'),
            'Historique appris':row['history_matches'], 'Appris jusqu’au':row['trained_through'],
            'Candidat':row['candidate_id'], 'Protocole':row['protocol_id']}
        for method, probabilities in row['methods'].items():
            for label in ('1st','Égal','2nd'):
                value[f'{NAMES[method]} · {label} (%)']=100*probabilities[label]
        value['Motif']=' · '.join(str(issue) for issue in row.get('issues',[]))
        output.append(value)
    return output


def render():
    st.subheader('Apprentissage quotidien', icon=':material/model_training:')
    st.caption('Un candidat est réentraîné lorsque son historique change, avec une recette conservée par compétition. '
               'Ses probabilités sont comparées sur de nouveaux matchs aux méthodes actuelles.')
    root=Path(api.DATA_DIR)
    try:
        data=_read(str(root),_signature(root),_now().strftime('%Y-%m-%dT%H:%M'))
    except (ValueError,OSError,KeyError,TypeError,sqlite3.Error):
        st.error('Le suivi quotidien est indisponible. Aucun score de remplacement n’est calculé.')
        return
    report=data['report'];counts=report['counts']
    if not data['enabled']:
        st.info('La boucle quotidienne est désactivée. Les prévisions déjà conservées restent consultables.')
    state=data['status']
    labels={'not_started':'Pas encore de préparation', 'building':'Entraînement en cours',
        'completed':'Dernier candidat préparé', 'idle':'File de préparation à jour', 'error':'Préparation à reprendre',
        'disabled':'Préparation désactivée', 'busy':'Un entraînement est déjà en cours'}
    st.caption(f"Service local : {'activé' if data['enabled'] else 'désactivé'} · "
               f"{labels.get(state['status'],state['status'])} · "
               f"Dernier passage : {state.get('finished_at',state.get('started_at','—'))}")
    if state.get('issues'):st.warning(' '.join(state['issues']))
    columns=st.columns(4)
    for col,label,value in zip(columns,('Matchs distincts conservés','Évalués communs','En attente','À vérifier ou annulés'),
        (counts['matches'],report['evaluated'],counts['pending'],counts['unresolved']+counts['void'])):
        col.metric(label,str(value))
    st.caption('Chaque match compte une fois pour cette procédure, même si plusieurs versions quotidiennes existent. '
               'Les anciennes prévisions restent figées. Aucun candidat ne remplace automatiquement le modèle courant.')
    views={'comparison':'Comparaison réelle','forecasts':'Prévisions conservées','training':'Entraînements'}
    view=st.segmented_control('Suivi à consulter',list(views),format_func=views.get,default='comparison',
                              required=True,key='daily_learning_view')
    if view=='training':
        values=sorted(data['candidates'],key=lambda v:v['completed_at'],reverse=True)
        if values:
            table_views.dataframe([{'Compétition API':v['league'],'Candidat':v['candidate_id'],
                'Terminé UTC':v['completed_at'],'Appris jusqu’au':v['trained_through'],
                'Résultats utilisés':v['history_matches'],'Journées utilisées':v['history_days'],
                'Recette':v['recipe_origin'],'Paramètres':json.dumps(v['params'],ensure_ascii=False),
                'CatBoost courant comparé':v['incumbent_generation'],'Modèle remplacé':v['serving_changed']}
                for v in values],hide_index=True,width='stretch',key='daily_learning_training', table_id='daily_learning_panel:render:dataframe:1')
        else:st.info('Aucun candidat préparé pour le moment.')
        st.caption('La file parcourt les compétitions disposant des données nécessaires. Un entraînement par passage '
                   'alterne avec les autres traitements ; un historique identique réutilise son candidat.')
    elif view=='forecasts':
        if report['rows']:
            table_views.dataframe(_forecasts(report['rows']),hide_index=True,width='stretch',key='daily_learning_forecasts', table_id='daily_learning_panel:render:dataframe:2')
        else:st.info('Aucune capture avant match pour le moment. Les anciens matchs ne sont pas ajoutés rétrospectivement.')
    else:
        table_views.dataframe(_metrics(report['metrics']),hide_index=True,width='stretch',key='daily_learning_metrics', table_id='daily_learning_panel:render:dataframe:3')
        st.caption('Une erreur probabiliste ou un Brier plus faible est préférable. Les taux restent vides avant les résultats. '
                   'Les scores manquants et les annulations sont exclus des réussites et des échecs.')
        if report['incumbent']['matches']:
            st.markdown('**Comparaison avec le CatBoost courant, sur les mêmes matchs disponibles**')
            table_views.dataframe(_metrics(report['incumbent']['metrics']),hide_index=True,width='stretch',key='daily_learning_incumbent', table_id='daily_learning_panel:render:dataframe:4')
        st.markdown('**Que valent les pourcentages les plus élevés ?**')
        table_views.dataframe([{'Seuil annoncé (%)':100*r['threshold'],'Matchs évalués':r['matches'],
            'Part des matchs évalués (%)':_percent(r['coverage']),
            'Confiance moyenne annoncée (%)':_percent(r['mean_confidence']),
            **{f'Réussite {NAMES[m]} (%)':_percent(r['metrics'][m]['accuracy']) for m in tracking.METHODS}}
            for r in report['thresholds']],hide_index=True,width='stretch',key='daily_learning_thresholds', table_id='daily_learning_panel:render:dataframe:5')
        if report['daily']:
            st.markdown('**Bilan par journée**')
            table_views.dataframe([{'Journée (Kinshasa)':r['day'],'Matchs conservés':r['counts']['matches'],
                'Évalués communs':r['evaluated'],'En attente':r['counts']['pending'],'À vérifier':r['counts']['unresolved'],
                'Annulés':r['counts']['void'],**{f'Réussite {NAMES[m]} (%)':_percent(r['metrics'][m]['accuracy'])
                for m in tracking.METHODS}} for r in report['daily']],hide_index=True,width='stretch',key='daily_learning_days', table_id='daily_learning_panel:render:dataframe:6')
        if report['weekly']:
            st.markdown('**Semaines UTC complètes**')
            table_views.dataframe([{'Début de semaine UTC':r['week_utc'],'Semaine close':r['calendar_closed'],
                'Bloc complet admissible':r['admissible'],'Évalués':r['evaluated'],
                'En attente':r['counts']['pending'],'À vérifier':r['counts']['unresolved']}
                for r in report['weekly']],hide_index=True,width='stretch',key='daily_learning_weeks', table_id='daily_learning_panel:render:dataframe:7')
        st.caption(f"{report['closed_weeks']} semaine(s) complète(s) admissible(s). Les clubs récurrents restent liés. "
                   'Un réentraînement quotidien ne prouve pas, à lui seul, une amélioration prédictive.')
    st.download_button('Exporter le suivi quotidien',json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),
        file_name='patternlab_apprentissage_quotidien.json',mime='application/json',on_click='ignore',key='daily_learning_export')
