# PatternLab

Application Streamlit d’analyse des scénarios de football, avec **API-Football comme source unique**.

Pour chaque match, PatternLab distingue les périodes qui comptent le plus de buts :
**1st**, **Égal** ou **2nd**. L’application classe des scénarios pour des groupes ordonnés
de 2 à 20 matchs, conserve les prévisions enregistrées avant match et les compare aux
résultats observés. Elle propose également des analyses du jour, un historique et
des outils de validation chronologique.

La valeur prédictive des méthodes reste à mesurer ; un scénario proposé n’est pas
un résultat garanti.

## Installation

Depuis le dossier du projet :

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .streamlit/secrets.example.toml .streamlit/secrets.toml
```

Renseigner sa clé API-Football dans `.streamlit/secrets.toml`, puis lancer :

```bash
streamlit run app.py
```

La variable d’environnement `API_FOOTBALL_KEY` peut également fournir la clé.
Les compétitions et saisons accessibles dépendent du compte API-Football utilisé.

## Données et confidentialité

Ce dépôt contient le code, les tests, les ressources graphiques et la documentation.
Les clés API, les données téléchargées, les modèles entraînés, le journal des
prédictions et les rapports locaux générés sont exclus de Git. Ils restent sur la
machine qui exécute l’application et ne sont pas sauvegardés par un envoi du code.

Sur une nouvelle installation, utiliser **Données et réglages** pour connecter l’API
et importer les historiques nécessaires. Aucun historique personnel ni modèle
entraîné n’est fourni dans ce dépôt.

## Vérification

```bash
python -m unittest discover -p 'test_*.py'
```

Le [guide complet](README_MAC.md) détaille les pages, les méthodes, l’automatisation
et le suivi des résultats. Sur Mac, `Lancer_PatternLab.command` permet aussi de lancer
l’application.
