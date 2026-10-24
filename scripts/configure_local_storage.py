"""Select local storage without deleting credentials, data or remote history.

No network connection is made. Use --owner to allow administration when the
Streamlit server is bound to a literal loopback address.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import tempfile
import tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODE_KEY = 'PATTERNLAB_STORAGE_MODE'


class ConfigurationError(ValueError):
    """Credential-free errors safe to show in the terminal."""


def _read(path):
    # Check the application root and private directory. macOS itself exposes
    # ordinary temporary directories through /var -> /private/var.
    if any(p.is_symlink() for p in (path, path.parent, path.parent.parent)):
        raise ConfigurationError('Les secrets doivent rester dans un chemin local sans lien symbolique.')
    try:
        original = path.read_bytes() if path.exists() else b''
        parsed = tomllib.loads(original.decode('utf-8'))
    except (OSError, ValueError, UnicodeError):
        raise ConfigurationError('Les secrets sont illisibles ou leur format TOML est invalide ; aucun changement effectué.') from None
    return original, parsed


def _statements(text):
    """Locate complete TOML statements without interpreting private values.

    Quoted and multiline strings, arrays, inline tables and comments can contain
    text resembling a setting. Only complete statements outside those values
    may be edited.
    """
    start, index, depth, quote = 0, 0, 0, None
    while index < len(text):
        character = text[index]
        if quote:
            if quote in ('"""', "'''"):
                if text.startswith(quote, index):
                    # TOML permits one/two content quotes before a closing
                    # triple delimiter. A valid document has at most five.
                    while index < len(text) and text[index] == quote[0]:
                        index += 1
                    quote = None
                    continue
                if quote == '"""' and character == '\\':
                    index += 2
                    continue
            elif character == quote:
                quote = None
            elif quote == '"' and character == '\\':
                index += 2
                continue
        elif character == '#':
            newline = text.find('\n', index)
            index = len(text) if newline < 0 else newline
            if index == len(text):
                break
            continue
        elif character in ('"', "'"):
            quote = character * 3 if text.startswith(character * 3, index) else character
            index += len(quote)
            continue
        elif character in '[{':
            depth += 1
        elif character in ']}':
            depth -= 1
        elif character == '\n' and depth == 0:
            yield start, index + 1, text[start:index + 1]
            start = index + 1
        index += 1
    if start < len(text):
        yield start, len(text), text[start:]


def _assignment_key(statement):
    quote = None
    for index, character in enumerate(statement):
        if quote:
            if character == quote and (index == 0 or statement[index - 1] != '\\' or quote == "'"):
                quote = None
        elif character in ('"', "'"):
            quote = character
        elif character == '=':
            try:
                value = tomllib.loads(statement[:index] + '=0')
                path = []
                while isinstance(value, dict) and len(value) == 1:
                    name, value = next(iter(value.items()))
                    path.append(name)
                return tuple(path) if value == 0 else None
            except ValueError:
                return None
        elif character == '#':
            return None
    return None


def _table_key(statement):
    try:
        value = tomllib.loads(statement.rstrip() + '\n__local_probe__=0\n')
        path = []
        while isinstance(value, dict) and len(value) == 1:
            name, value = next(iter(value.items()))
            if name == '__local_probe__':
                return tuple(path)
            path.append(name)
    except ValueError:
        pass
    return None


def _content(original, parsed, *, owner):
    text = original.decode('utf-8')
    newline = '\r\n' if '\r\n' in text else '\n'
    table, access_end = (), None
    mode_span, owner_span, access_header = None, None, None
    dotted_access = False
    statements = list(_statements(text))
    for start, end, raw in statements:
        stripped = raw.lstrip()
        if stripped.startswith('['):
            if table == ('access',):
                access_end = start
            table = _table_key(raw)
            if table == ('access',):
                access_header = end
                access_end = len(text)
            continue
        key = _assignment_key(raw)
        if key is None or table is None:
            continue
        full_key = table + key
        if table == () and len(key) > 1 and key[0] == 'access':
            dotted_access = True
        if full_key == (MODE_KEY,):
            mode_span = start, end
        elif full_key == ('access', 'local_owner'):
            owner_span = start, end
    replacements = []
    mode_line = f'{MODE_KEY} = "local"{newline}'
    if mode_span:
        replacements.append((*mode_span, mode_line))
    else:
        # Always prepend: new root settings cannot be appended after a table.
        replacements.append((0, 0, mode_line))
    if owner:
        access = parsed.get('access', {})
        if not isinstance(access, dict):
            raise ConfigurationError('La section access est invalide ; aucun changement effectué.')
        if owner_span:
            start, end = owner_span
            key = 'local_owner' if access_header is not None and start >= access_header else 'access.local_owner'
            replacements.append((start, end, f'{key} = true{newline}'))
        elif access_header is not None:
            prefix = '' if access_end == 0 or text[access_end - 1:access_end] == '\n' else newline
            replacements.append((access_end, access_end, prefix + f'local_owner = true{newline}'))
        elif 'access' not in parsed:
            prefix = '' if not text or text.endswith('\n') else newline
            replacements.append((len(text), len(text), prefix + f'{newline}[access]{newline}local_owner = true{newline}'))
        elif dotted_access:
            replacements.append((0, 0, f'access.local_owner = true{newline}'))
        elif access:
            # Dotted owner entries can be edited above. An inline/access dotted
            # configuration without that field cannot safely be extended by a
            # table without potentially changing the other private bytes.
            raise ConfigurationError('La section access utilise une forme TOML compacte ; ajoutez local_owner dans cette section avant de relancer --owner.')
        else:
            raise ConfigurationError('La section access doit être une table [access] pour utiliser --owner ; aucun changement effectué.')
    merged = {}
    for start, end, replacement in replacements:
        merged[start, end] = merged.get((start, end), '') + replacement
    for (start, end), replacement in sorted(merged.items(), reverse=True):
        text = text[:start] + replacement + text[end:]
    try:
        result = tomllib.loads(text)
        expected = dict(parsed)
        expected[MODE_KEY] = 'local'
        if owner:
            expected['access'] = {**parsed.get('access', {}), 'local_owner': True}
        if result != expected:
            raise ValueError()
    except (ValueError, TypeError):
        raise ConfigurationError('Impossible de modifier ces réglages sans altérer les autres secrets ; aucun changement effectué.') from None
    return text.encode('utf-8')


def configure_storage(*, project_root=None, owner=False):
    """Atomically select local mode; leave every unrelated setting untouched."""
    if type(owner) is not bool:
        raise ConfigurationError('Le choix du propriétaire local doit être explicite.')
    root = Path(project_root) if project_root is not None else PROJECT_ROOT
    path = root / '.streamlit' / 'secrets.toml'
    original, parsed = _read(path)
    content = _content(original, parsed, owner=owner)
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if _read(path)[0] != original:
            raise ConfigurationError('Les secrets ont changé pendant la préparation ; aucun remplacement effectué.')
        with tempfile.NamedTemporaryFile('wb', dir=path.parent, prefix='.configure-local-', delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if _read(path)[0] != original:
            raise ConfigurationError('Les secrets ont changé pendant la préparation ; aucun remplacement effectué.')
        os.replace(temporary, path)
    except ConfigurationError:
        raise
    except (OSError, ValueError, UnicodeError):
        raise ConfigurationError('Impossible d’enregistrer le mode local ; aucun détail privé n’est affiché.') from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None, *, project_root=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owner', action='store_true', help='Autoriser le propriétaire sur un serveur lié à 127.0.0.1 ou ::1.')
    options = parser.parse_args(argv)
    try:
        configure_storage(project_root=project_root, owner=options.owner)
    except ConfigurationError as error:
        print(str(error))
        return 1
    print('Mode local enregistré. Données et identifiants conservés ; aucun transfert ni accès réseau effectué.')
    print('Redémarrez les processus PatternLab pour appliquer ce choix à toutes les opérations locales.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
