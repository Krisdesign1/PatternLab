"""Explicitly register future evidence criteria once, without API requests."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import journal_protocol as protocol


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--status', action='store_true', help='Lire seulement le protocole, sans écriture.')
    args = parser.parse_args()
    try:
        value = protocol.load_protocol() if args.status else protocol.register_protocol()
    except (protocol.ProtocolError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(dict(status='registered' if value else 'absent',
        **({k:value[k] for k in ('version','registered_at','baseline','primary','secondary','min_blocks','fingerprint')} if value else {})), ensure_ascii=False))


if __name__ == '__main__':
    main()
