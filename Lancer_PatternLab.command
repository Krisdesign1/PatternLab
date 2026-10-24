#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

# This launcher always owns a local workspace, even if the same account also
# has private connection settings for the separately hosted site.
export PATTERNLAB_STORAGE_MODE=local

echo "======================================="
echo "  PatternLab - installation / lancement"
echo "======================================="
echo

if [ ! -d ".venv" ]; then
  if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 n'est pas installé."
    echo "Installe Python 3 depuis https://www.python.org/downloads/macos/"
    exit 1
  fi
  echo "Création de l'environnement local..."
  python3 -m venv .venv
fi

python=".venv/bin/python"
if [ ! -x "$python" ]; then
  echo "L'environnement .venv est incomplet : .venv/bin/python est absent."
  exit 1
fi

requirements_signature=$("$python" -c 'import hashlib,pathlib,sys; print(hashlib.sha256(pathlib.Path("requirements.txt").read_bytes() + repr(sys.version_info[:2]).encode()).hexdigest())')
marker=".venv/.patternlab-requirements.sha256"
installed_signature=""
if [ -f "$marker" ]; then
  installed_signature=$(cat "$marker")
fi

# Locating modules is inexpensive and does not import CatBoost or connect to a
# service. Ordinary launches do not run pip or contact the package registry.
if [ "$requirements_signature" != "$installed_signature" ] || ! "$python" -c 'import importlib.util,sys; sys.exit(any(importlib.util.find_spec(name) is None for name in ("streamlit","pandas","reportlab","certifi","catboost","psycopg","authlib")))'; then
  echo "Installation des dépendances nouvelles ou manquantes..."
  "$python" -m pip install -r requirements.txt
  temporary_marker=$(mktemp ".venv/.patternlab-requirements.XXXXXX")
  printf '%s\n' "$requirements_signature" > "$temporary_marker"
  mv "$temporary_marker" "$marker"
fi

echo
echo "Démarrage de PatternLab en mode local..."
echo "La page va s'ouvrir dans ton navigateur."
echo "Pour arrêter l'application : reviens ici et fais Ctrl+C."
echo
"$python" -m streamlit run app.py --server.address 127.0.0.1
