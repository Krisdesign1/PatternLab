"""Read-only historical evidence; a Streamlit visit never starts a replay."""
import table_views
import json

import streamlit as st

import argumented_selection_study as study


POLICIES={'argumented':'Règle actuelle · au moins 5/10',
    'agreement_only':'Accord modèle/championnat seul',
    'strict_recent7':'Confirmation renforcée · au moins 7/10',
    'recent365':'Règle actuelle · dix précédents datant d’au plus 365 jours'}
COHORTS={'selected':'Sélection de la règle','top_clubs':'Plus fortes probabilités Clubs',
         'top_baseline':'Plus fortes fréquences du championnat'}


def _pct(value):return None if value is None else 100*value


@st.cache_data(ttl=60,max_entries=2,show_spinner=False)
def _read(signature):
    return study.load_report()


def _rows(scope):
    return [{'Sélection':name,'Matchs retenus':scope[key]['N'],
        'Matchs évalués':scope[key]['evaluated_N'],'Choix corrects':scope[key]['correct'],
        'Réussite observée (%)':_pct(scope[key]['accuracy']),
        'Confiance moyenne (%)':_pct(scope[key].get('mean_confidence')),
        'Part du vivier (%)':_pct(scope[key].get('coverage')),
        'Erreur probabiliste ↓':scope[key]['logloss'],'Brier ↓':scope[key]['brier']}
        for key,name in COHORTS.items()]


def render():
    st.subheader('Test chronologique sur l’historique',icon=':material/science:')
    st.caption('Les règles sont comparées aux classements simples avec le même nombre de matchs par journée, '
        'dans le même vivier de rencontres disposant d’au moins dix précédents par rôle.')
    try:report=_read(study.signature())
    except (ValueError,OSError,KeyError,TypeError):
        st.error('Le rapport historique est illisible ou altéré. Aucun résultat n’est recalculé dans cette page.')
        return
    if report is None:
        st.info('Aucun rejeu historique terminé pour cette règle. Les tests de fonctionnement du code ne mesurent pas sa précision.')
        return
    st.info('Évaluation exploratoire : cet historique a déjà été étudié. Les données actuelles corrigées et '
        'les affiches reconstruites ne prouvent pas quelles informations étaient réellement disponibles à l’époque.')
    plan=report['protocol']
    st.caption(f"Période : {plan['start']} → {plan['end']} exclu · Source : API-Football · "
        f"Copie figée : {report['inventory']['rows']} résultats antérieurs disponibles, y compris l’historique de préparation.")
    selected=st.selectbox('Règle à examiner',list(POLICIES),format_func=POLICIES.get,key='arg_study_policy')
    result=report['policies'][selected]
    if (result['comparisons']['top_clubs']['delta_accuracy'] is not None
            and result['comparisons']['top_clubs']['delta_accuracy']<0
            and result['comparisons']['top_baseline']['delta_accuracy']<0):
        st.warning('Dans ce rejeu, cette règle obtient moins de choix corrects que les deux classements simples '
                   'de même taille par journée. Elle n’a pas démontré de bénéfice sur cet historique.')
    table_views.dataframe(_rows(result['overall']),hide_index=True,width='stretch',
        column_config={name:st.column_config.NumberColumn(format='%.2f') for name in
            ('Réussite observée (%)','Confiance moyenne (%)','Part du vivier (%)')},key='arg_study_overall', table_id='argumented_selection_study_panel:render:dataframe:1')
    st.caption('Le vivier comprend les matchs terminés avec scores et identités exploitables présents dans cette copie. '
        'Il ne représente pas toutes les occasions historiques de l’API. Les intervalles ci-dessous sont descriptifs.')
    st.caption('Le championnat et Clubs désignent le même favori sur les matchs retenus par cette règle : '
        'leur réussite sur ces mêmes matchs est donc identique par construction. Le tableau compare ici '
        'des sélections distinctes de même taille, prises dans le même vivier chaque jour.')
    comparisons=[]
    for key,value in result['comparisons'].items():
        boot=value['bootstrap']
        comparisons.append({'Comparaison':COHORTS[key],
            'Même nombre retenu chaque jour':value['equal_counts_every_day'],
            'Matchs évalués par sélection':value['paired_evaluated_N'],
            'Écart de choix corrects':value['delta_correct'],
            'Écart de réussite (points)':_pct(value['delta_accuracy']),
            'Semaines complètes':boot['weeks'],
            'Intervalle descriptif (points)':f"[{100*boot['lower']:+.2f} ; {100*boot['upper']:+.2f}]"
                if boot['lower'] is not None and boot['upper'] is not None else 'Effectif insuffisant'})
    table_views.dataframe(comparisons,hide_index=True,width='stretch',key='arg_study_comparisons', table_id='argumented_selection_study_panel:render:dataframe:2')
    st.markdown('**Résultats par année**')
    years=[]
    for year,scope in result['by_year'].items():
        years.extend([dict(Année=year,**row) for row in _rows(scope)])
    table_views.dataframe(years,hide_index=True,width='stretch',key='arg_study_years', table_id='argumented_selection_study_panel:render:dataframe:3')
    st.markdown('**Effet des deux variantes fixées avant le calcul**')
    variants=[]
    for key,value in report['policies'].items():
        overall=value['overall'];metrics=overall['selected']
        variants.append({'Règle':POLICIES[key],'Matchs retenus':metrics['N'],
            'Réussite observée (%)':_pct(metrics['accuracy']),
            'Écart au classement Clubs (points)':_pct(value['comparisons']['top_clubs']['delta_accuracy']),
            'Écart au classement championnat (points)':_pct(value['comparisons']['top_baseline']['delta_accuracy'])})
    table_views.dataframe(variants,hide_index=True,width='stretch',key='arg_study_variants', table_id='argumented_selection_study_panel:render:dataframe:4')
    st.caption('Chaque variante possède ses propres classements de comparaison de taille égale par jour. '
        'Le tableau ne choisit ni n’active automatiquement une nouvelle règle.')
    st.markdown('**Fiabilité des pourcentages de la sélection**')
    bins=result['overall']['selected']['confidence_bins']
    table_views.dataframe([{'Pourcentage annoncé':f"{100*b['lower']:.0f}–{100*b['upper']:.0f} %",
        'Matchs évalués':b['evaluated_N'],'Confiance moyenne (%)':_pct(b['evaluated_mean_confidence']),
        'Réussite observée (%)':_pct(b['accuracy'])} for b in bins],hide_index=True,width='stretch',key='arg_study_calibration', table_id='argumented_selection_study_panel:render:dataframe:5')
    st.markdown('**Répartition par compétition**')
    leagues=[]
    for row in result['by_league']:
        league=' · '.join([*row['countries'],*row['competitions']])
        leagues.append({'Compétition':league,'Matchs du vivier':row['common_pool_N'],
            'Sélection argumentée · matchs':row['selected']['N'],
            'Sélection argumentée · réussite (%)':_pct(row['selected']['accuracy']),
            'Classement Clubs · matchs':row['top_clubs']['N'],
            'Classement Clubs · réussite (%)':_pct(row['top_clubs']['accuracy']),
            'Classement championnat · matchs':row['top_baseline']['N'],
            'Classement championnat · réussite (%)':_pct(row['top_baseline']['accuracy'])})
    table_views.dataframe(leagues,hide_index=True,width='stretch',key='arg_study_leagues', table_id='argumented_selection_study_panel:render:dataframe:6')
    st.caption('Les effectifs sont appariés par journée sur l’ensemble des compétitions. Ils peuvent différer '
        'dans une compétition donnée ; ce tableau décrit la composition, pas une comparaison appariée par ligue.')
    st.caption('Aucune ancienne prévision n’est créée ou modifiée par ce rejeu. Le suivi réel avant match reste séparé. '
        'Ouvrir ce bilan ne lance ni appel API, ni entraînement, ni calcul historique.')
    st.download_button('Exporter le bilan historique',json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),
        file_name='patternlab_test_selection_argumentee.json',mime='application/json',on_click='ignore',key='arg_study_export')
