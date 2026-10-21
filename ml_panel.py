"""Interface de l'expérience ML ; aucun accès en écriture à SQLite."""
import pandas as pd
import streamlit as st

from analytics import VALID
from ml_model import (
    MIN_COUPONS, dataset_fingerprint, export_experiment, predict_next,
    train_experiment,
)


def render_training(rows):
    st.subheader('Entraîner un modèle', icon=':material/model_training:')
    st.write(
        'Le modèle apprend quelle valeur suit la dernière valeur d’un coupon. '
        'Il estime trois probabilités : **1st**, **Égal** et **2nd**.'
    )
    st.caption(
        'Modèle de Markov d’ordre 1 : apprentissage direct des transitions avec lissage. '
        'Aucune donnée fictive n’est ajoutée à votre historique.'
    )
    enough = len(rows) >= MIN_COUPONS
    if not enough:
        st.info(f'Ajoutez au moins {MIN_COUPONS} coupons pour lancer un premier essai.')
    st.warning(
        'Expérience exploratoire : un bon score sur quelques coupons ne démontre pas '
        'une capacité à prévoir les prochains résultats. Les transitions d’un même '
        'coupon ne sont pas des observations indépendantes.'
    )
    st.caption(
        'Test : les 20 % derniers coupons enregistrés sont mis de côté avant '
        'l’apprentissage. L’ordre utilisé est celui de l’ajout, pas la date réelle '
        'des événements. Les données initiales sont incluses si elles sont encore présentes.'
    )
    if st.button('Entraîner et évaluer', icon=':material/play_arrow:',
                 type='primary', disabled=not enough, key='ml_train'):
        try:
            with st.spinner('Apprentissage et évaluation en cours…'):
                experiment = train_experiment(rows)
            st.session_state.ml_experiment = experiment
            st.success('Entraînement terminé. Les résultats du test sont disponibles ci-dessous.')
        except ValueError as exc:
            st.error(str(exc))

    experiment = st.session_state.get('ml_experiment')
    if experiment is None:
        return
    if experiment['fingerprint'] != dataset_fingerprint(rows):
        # Un modèle devenu obsolète (notamment après suppression) ne reste pas actif.
        del st.session_state['ml_experiment']
        st.info('L’historique a changé. Relancez l’entraînement pour utiliser les données actuelles.')
        return

    evaluation = experiment['evaluation']
    st.markdown('#### Résultats sur les coupons réservés au test')
    c1, c2, c3 = st.columns(3)
    c1.metric('Coupons d’apprentissage', len(experiment['train_ids']), border=True)
    c2.metric('Coupons de test', len(experiment['test_ids']), border=True)
    c3.metric('Valeurs suivantes testées', evaluation['predictions'], border=True)
    comparison = []
    for key, label in [('model', 'Modèle de Markov'), ('baseline', 'Référence sans contexte')]:
        score = evaluation[key]
        comparison.append({
            'Méthode': label,
            'Prédictions correctes': f"{score['correct']}/{evaluation['predictions']}",
            'Exactitude (%)': round(score['accuracy'] * 100, 1),
            'Erreur probabiliste (log loss)': round(score['log_loss'], 3),
        })
    st.dataframe(pd.DataFrame(comparison), hide_index=True, width='stretch')
    st.caption(
        'La référence utilise les fréquences des valeurs suivantes du lot d’apprentissage, '
        'sans tenir compte de la dernière valeur. Pour la log loss, plus bas est meilleur. '
        'En cas d’égalité, le score d’exactitude choisit 1st, puis Égal, puis 2nd.'
    )
    delta = (evaluation['model']['accuracy'] - evaluation['baseline']['accuracy']) * 100
    st.write(f'Écart d’exactitude sur ce test : **{delta:+.1f} points** par rapport à la référence.')
    st.caption(
        'Ce test mesure la prochaine valeur en connaissant les valeurs précédentes réelles ; '
        'il ne mesure pas la prédiction d’un coupon entier. Répéter l’entraînement sur la '
        'même base reproduit les mêmes résultats.'
    )
    overlap = experiment['test_patterns_already_in_train']
    if overlap:
        st.warning(
            f'{overlap} coupon(s) du test ont une séquence identique à une séquence '
            'd’apprentissage. Cela peut faciliter le test ; vérifiez les réimports accidentels.'
        )

    st.markdown('#### Estimer la prochaine valeur')
    st.caption(
        f"Après le test, un second modèle a été ajusté sur les {len(rows)} coupons disponibles "
        'pour cette estimation. Les scores ci-dessus concernent uniquement le modèle '
        'entraîné sans les coupons de test.'
    )
    last = st.selectbox('Dernière valeur connue dans le coupon', VALID, key='ml_last')
    prediction = predict_next(experiment['model'], (last,))
    st.dataframe(pd.DataFrame([
        {'Valeur suivante': value, 'Probabilité estimée (%)': round(probability * 100, 1)}
        for value, probability in prediction['probabilities'].items()
    ]), hide_index=True, width='stretch')
    if prediction['used_global_fallback']:
        st.info('Aucune transition observée après cette valeur : utilisation des fréquences globales.')
    else:
        st.caption(
            f"Estimation fondée sur {prediction['context_support']} transition(s) observée(s) "
            f'après {last}, avec lissage. Ces pourcentages ne sont pas des garanties.'
        )
    with st.expander('Comprendre ce qui a été appris', icon=':material/account_tree:'):
        st.write('Effectifs des transitions du modèle final, avant lissage :')
        st.dataframe(pd.DataFrame([
            {'Après': previous, **experiment['model']['counts'][previous]}
            for previous in VALID
        ]), hide_index=True, width='stretch')
        st.caption(
            'Le lissage ajoute un pseudo-effectif de 1 à chaque possibilité pour éviter '
            'les probabilités nulles. Il ne crée aucun coupon dans la base.'
        )
        st.write(f"Séquences distinctes dans la base : **{experiment['unique_patterns']}**.")
        st.caption(
            'Prochaine étape : ajouter de nouveaux coupons réels dans l’ordre, puis '
            'évaluer à nouveau. Ce premier modèle n’utilise ni le score, ni les équipes, '
            'ni les conditions des événements.'
        )
    st.download_button(
        'Exporter le modèle et le rapport', data=export_experiment(experiment),
        file_name='patternlab_entrainement.json', mime='application/json',
        icon=':material/download:', key='ml_export',
    )
    st.caption(
        'Le modèle reste disponible dans cette session. Après fermeture, relancez '
        'l’entraînement ; le fichier JSON permet de conserver ses paramètres et les résultats.'
    )
