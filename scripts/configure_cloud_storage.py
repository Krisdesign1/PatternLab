"""Configure local Cloud secrets without displaying a database password.

This command makes no network requests and never initializes remote data.
The password is entered through a hidden terminal prompt, never an argument.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import re
import tempfile
import tomllib
from urllib.parse import quote
import warnings


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLOUD_KEYS = frozenset(('PATTERNLAB_DATABASE_URL', 'PATTERNLAB_WORKSPACE', 'PATTERNLAB_CLOUD_REQUIRED',
                        'PATTERNLAB_CLOUD_ADOPT_LOCAL', 'PATTERNLAB_CLOUD_ALLOW_INSECURE_LOCALHOST_TEST'))


class ConfigurationError(ValueError):
    """Fixed, credential-free configuration messages."""


def _validate(host, user, port, database, workspace):
    if not isinstance(host, str) or not re.fullmatch(
            r'aws-[0-9]+-[a-z0-9]+(?:-[a-z0-9]+)*\.pooler\.supabase\.com', host):
        raise ConfigurationError('Utilisez l’hôte Session pooler officiel de Supabase, sans URL ni chemin.')
    if not isinstance(user, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', user):
        raise ConfigurationError('Le nom d’utilisateur du pooler est invalide.')
    if type(port) is not int or not 1 <= port <= 65535:
        raise ConfigurationError('Le port PostgreSQL est invalide.')
    if not isinstance(database, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}', database):
        raise ConfigurationError('Le nom de la base PostgreSQL est invalide.')
    if not isinstance(workspace, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', workspace):
        raise ConfigurationError('Le nom de l’espace de stockage est invalide.')


def _existing_secrets(path):
    if path.is_symlink() or path.parent.is_symlink():
        raise ConfigurationError('Les secrets doivent être un fichier local, sans lien symbolique.')
    try:
        original = path.read_bytes() if path.exists() else b''
        parsed = tomllib.loads(original.decode('utf-8'))
    except (OSError, ValueError, UnicodeError):
        raise ConfigurationError('Le fichier de secrets est illisible ou son format TOML est invalide ; aucun changement effectué.') from None
    if CLOUD_KEYS.intersection(parsed):
        raise ConfigurationError('Des réglages Cloud existent déjà à la racine des secrets ; aucun changement effectué.')
    return original


def configure_storage(*, host, user, port=5432, database='postgres', workspace='patternlab-main',
                      project_root=None, password_prompt=None):
    """Atomically prepend root TOML keys while preserving existing bytes."""
    _validate(host, user, port, database, workspace)
    root = Path(project_root) if project_root is not None else PROJECT_ROOT
    path = root / '.streamlit' / 'secrets.toml'
    original = _existing_secrets(path)
    prompt = password_prompt if password_prompt is not None else getpass.getpass
    try:
        # getpass may otherwise fall back to echoing stdin when no terminal is
        # available. Refuse that fallback instead of risking a visible password.
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            password = prompt('Mot de passe PostgreSQL existant (saisie masquée) : ')
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        raise ConfigurationError('La saisie masquée est indisponible ou annulée ; aucun changement effectué.') from None
    except Exception:
        raise ConfigurationError('Impossible de saisir le mot de passe de façon sûre ; aucun changement effectué.') from None
    if not isinstance(password, str) or not password or '\x00' in password:
        raise ConfigurationError('Le mot de passe PostgreSQL est vide ou invalide ; aucun changement effectué.')
    try:
        password.encode('utf-8')
    except UnicodeError:
        raise ConfigurationError('Le mot de passe PostgreSQL est invalide ; aucun changement effectué.') from None
    dsn = (f'postgresql://{quote(user, safe="")}:{quote(password, safe="")}@'
           f'{host}:{port}/{quote(database, safe="")}?sslmode=require')
    prefix = (f'PATTERNLAB_DATABASE_URL = {json.dumps(dsn)}\n'
              f'PATTERNLAB_WORKSPACE = {json.dumps(workspace)}\n'
              'PATTERNLAB_CLOUD_REQUIRED = true\n\n').encode('utf-8')
    content = prefix + original
    temporary = None
    try:
        # Validate the entire result before creating or replacing any file.
        tomllib.loads(content.decode('utf-8'))
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if _existing_secrets(path) != original:
            raise ConfigurationError('Le fichier de secrets a changé pendant la saisie ; aucun changement effectué.')
        with tempfile.NamedTemporaryFile('wb', dir=path.parent, prefix='.configure-cloud-', delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if _existing_secrets(path) != original:
            raise ConfigurationError('Le fichier de secrets a changé pendant la saisie ; aucun changement effectué.')
        os.replace(temporary, path)
    except ConfigurationError:
        raise
    except (OSError, ValueError, UnicodeError):
        raise ConfigurationError('Impossible d’enregistrer les secrets privés ; aucun détail de connexion n’est affiché.') from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None, *, project_root=None, password_prompt=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, help='Hôte Session pooler officiel Supabase.')
    parser.add_argument('--user', required=True, help='Utilisateur du pooler indiqué par Supabase.')
    parser.add_argument('--port', default=5432, type=int)
    parser.add_argument('--database', default='postgres')
    parser.add_argument('--workspace', default='patternlab-main')
    options = parser.parse_args(argv)
    try:
        configure_storage(**vars(options), project_root=project_root, password_prompt=password_prompt)
    except ConfigurationError as error:
        print(str(error))
        return 1
    print('Secrets Cloud enregistrés localement. Aucune donnée transférée et aucune requête réseau effectuée.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
