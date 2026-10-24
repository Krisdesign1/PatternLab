"""Explicit local JSON -> SQLite migration, or a consistent JSON export.

No API request, model fit, journal mutation or remote access is performed.
The original JSON is retained. Run without --activate to inspect the store.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_football as api
import cloud_storage
import fixture_store


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=api.STORE_PATH)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--activate', action='store_true', help='Valider puis activer la copie SQLite locale.')
    action.add_argument('--export', type=Path, help='Exporter la version courante, sans remplacer un fichier existant.')
    args = parser.parse_args(argv)
    try:
        if cloud_storage.configured():
            raise ValueError('Cette opération exige le mode local. Aucun accès distant effectué.')
        if args.export is not None:
            payload = api.export_store(args.source)
            content = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
            # Exclusive create prevents silently overwriting the retained JSON.
            with args.export.open('xb') as handle:
                handle.write(content)
            result = dict(export=str(args.export), fixtures=len(payload['fixtures']), bytes=len(content))
        elif args.activate:
            result = fixture_store.migrate_json(args.source)
        else:
            database = api.active_store(args.source)
            if database is not None:
                result = dict(fixture_store.statistics(database), database=str(database), active=True)
            else:
                payload = api.export_store(args.source)
                rows, audit = api.load_matches(args.source)
                result = dict(active=False, fixtures=len(payload['fixtures']), usable=len(rows),
                              excluded=len(audit['issues']), source=str(args.source))
        if 'history_counts' in result:
            result = dict(result)
            result['competitions_with_results'] = len(result.pop('history_counts'))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (api.ApiFootballError, fixture_store.FixtureStoreError, cloud_storage.CloudStorageError,
            ValueError, OSError):
        print('Opération locale interrompue : source/configuration invalide ou destination déjà existante. '
              'Aucun secret affiché ; le JSON source est conservé.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
