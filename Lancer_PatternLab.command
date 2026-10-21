#!/bin/bash
cd "$(dirname "$0")"

echo "======================================="
echo "  PatternLab - installation / lancement"
echo "======================================="
echo

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 n'est pas installé."
  echo "Installe Python 3 depuis https://www.python.org/downloads/macos/"
  echo "puis relance ce fichier."
  read -n 1 -s -r -p "Appuie sur une touche pour fermer..."
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "Création de l'environnement local..."
  python3 -m venv .venv
fi

source .venv/bin/activate

echo "Installation / vérification des dépendances..."
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo
echo "Démarrage de PatternLab..."
echo "La page va s'ouvrir dans ton navigateur."
echo "Pour arrêter l'application : reviens ici et fais Ctrl+C."
echo
python -m streamlit run app.py
