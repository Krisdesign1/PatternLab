"""Local presentation adapters; no forecast, training or journal mutation."""
from math import isclose

import streamlit as st

import contextual_scenarios as contextual
import prospective_models as snapshots
from scenario_predictor import fit_sequences


ENGINE_METHOD = {'statistical':'combined', 'catboost':'catboost', 'context_neighbors':'context_neighbors'}
METHOD_NAMES = {'frequency':'Répétitions seules', 'clubs':'Clubs seuls', 'combined':'Clubs + répétitions',
                'catboost':'CatBoost', 'context_neighbors':'Répétitions par contexte'}


def result_column(label='Résultat', **kwargs):
    colors=['#8fc1ff','#d7b9ff','#ffc58a'] if st.context.theme.type=='dark' else ['#175da7','#7040a0','#934b08']
    return st.column_config.MultiselectColumn(label,options=['1st','Égal','2nd'],color=colors,**kwargs)


def forecast_methods(payload):
    """Reuse an analysis' probabilities; never infer additional learned models.

    Statistical analyses already contain every ingredient of the three exact
    distributions. Other engines retain their selected ranking as supplied.
    The journal supplies complete immutable method payloads separately.
    """
    estimate = payload['forecast']
    engine = payload.get('engine', 'statistical')
    name = ENGINE_METHOD[engine]
    ranking = [dict(sequence=list(row['sequence']), probability=row['probability'])
               for row in estimate['ranking']]
    if len(ranking) != 3:
        raise ValueError('Le classement doit conserver exactement les trois premiers scénarios de cette analyse.')
    selected = dict(version=estimate.get('version', engine), ranking=ranking)
    if engine != 'statistical':
        return {name:selected}
    size = len(payload['fixtures'])
    before = estimate['before']
    blocks = [block for block in payload['result'].get('blocks', [])
              if block.get('matches') and max(contextual._day(row['timestamp']) for row in block['matches']) < before]
    fit = fit_sequences([block['sequence'] for block in blocks], size)
    if fit[1] != estimate['training_groups']:
        raise ValueError('Les précédents de cette analyse ont changé ; relancez son classement.')
    positions = [item['probabilities'] for item in estimate['club_estimates']]
    provenance = dict(source=payload['source'], league=estimate['league'], training_before=before)
    methods = {
        'frequency':snapshots._method('league-frequency-v1', snapshots._distribution('frequency',size,fit=fit), provenance),
        'clubs':snapshots._method(snapshots.CLUB_VERSION, snapshots._distribution('independent',size,distributions=positions), provenance),
        'combined':snapshots._method(contextual.VERSION, snapshots._distribution('corrected',size,
            fit=fit,distributions=positions,alpha=.5,normalization=estimate['normalization']),provenance,alpha=.5),
    }
    # A layout update must never replace the selected model's ordering or mass.
    for calculated, supplied in zip(methods['combined']['ranking'], ranking):
        if calculated['sequence'] != supplied['sequence'] or not isclose(
                calculated['probability'],supplied['probability'],rel_tol=1e-9,abs_tol=1e-12):
            raise ValueError('Le tableau ne correspond plus aux probabilités calculées pour ce groupe.')
    return methods


def render_diagnostic(estimate, fixtures, *, engine='statistical', saved_methods=None):
    import scenario_diagnostics as diagnostic
    try:
        report = diagnostic.analyze(dict(estimate,fixtures=fixtures),engine=engine,saved_methods=saved_methods)
    except (ValueError,KeyError,TypeError) as error:
        st.warning('Explication indisponible : '+str(error))
        return
    with st.expander('Pourquoi ce scénario arrive-t-il premier ?'):
        for sentence in report['explanation']:
            st.markdown(sentence)
        if report['rows']:
            rows=[]
            for position,item in enumerate(report['rows'],1):
                probabilities=item.get('probabilities') or {}
                baseline=item.get('baseline') or {}
                deltas=item.get('delta_points') or {}
                label=report['leader'][position-1] if report.get('leader') else item.get('first_choice')
                percent=lambda value:'—' if value is None else f'{100*value:.2f} %'
                delta=deltas.get(label)
                rows.append({'Ordre':position,'Domicile':item.get('home') or '—','Extérieur':item.get('away') or '—',
                    '1st':percent(probabilities.get('1st')),'Égal':percent(probabilities.get('Égal')),
                    '2nd':percent(probabilities.get('2nd')),'Valeur dans le premier scénario':label or '—',
                    'Tendance pour cette valeur':percent(baseline.get(label)),
                    'Écart à la tendance (points)':'—' if delta is None else f'{delta:+.2f}',
                    'Précédents domicile':item.get('home_matches') if item.get('home_matches') is not None else '—',
                    'Précédents extérieur':item.get('away_matches') if item.get('away_matches') is not None else '—'})
            st.caption('Probabilités par affiche de la composante de base, avant la correction des scénarios. '
                       'Elles ne sont pas les probabilités marginales du modèle corrigé.')
            st.dataframe(rows,hide_index=True,width='stretch')


def render_leader(estimate):
    import scenario_tables as tables
    from scenario_odds import scenario_odds_text
    leader=estimate['ranking'][0]
    with st.container(border=True):
        st.badge('Premier scénario du moteur choisi',color='blue',icon=':material/schema:')
        tables.render_sequence(leader['sequence'])
        st.metric('Estimation du scénario complet',f"{100*leader['probability']:.2f} %")
        st.caption(f"Cote cumulée indicative : {scenario_odds_text(leader['sequence'])} · Référence fixe manuelle")


def render_analysis_matrix(payload):
    import scenario_tables as tables
    try:
        methods=forecast_methods(payload)
        tables.render_matrix(methods,key='workspace_scenario_matrix',title='Les méthodes, scénario par scénario')
        st.caption('Chaque ligne représente un scénario distinct. Les rangs restent propres à chaque méthode ; '
                   'leur accord ne constitue pas plusieurs confirmations indépendantes.')
    except (ValueError,KeyError,TypeError) as error:
        st.warning('Comparaison des scénarios indisponible : '+str(error))


def render_match_results(rows, *, key=None):
    """Readable native badges in score tables; exported data stays unmodified."""
    display=[dict(row,**{'Résultat':[row['Résultat']] if row.get('Résultat') in ('1st','Égal','2nd') else []}) for row in rows]
    st.dataframe(display,hide_index=True,width='stretch',key=key,column_config={'Résultat':result_column()})
