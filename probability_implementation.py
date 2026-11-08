"""Read-only fingerprints of the fixed individual probability implementation."""
from hashlib import sha256
from pathlib import Path


ROOT = Path(__file__).resolve().parent
LEGACY_SOURCE_FILES = ('match_predictor.py', 'high_probability_engine.py',
                       'league_model.py', 'club_identity.py')
SOURCE_FILES = (*LEGACY_SOURCE_FILES, 'halftime_domain.py')


def validate_hashes(value, *, current=False):
    """Read legacy evidence unchanged; new captures must attest the full core."""
    allowed = (set(SOURCE_FILES),) if current else (set(SOURCE_FILES), set(LEGACY_SOURCE_FILES))
    if (not isinstance(value, dict) or set(value) not in allowed
            or any(not isinstance(digest, str) or len(digest) != 64
                   or any(char not in '0123456789abcdef' for char in digest)
                   for digest in value.values())):
        raise ValueError('Les empreintes de l’implémentation des probabilités sont invalides.')
    return dict(value)


def code_hashes():
    """Return fresh digests without importing models or writing any files.

    The sources total only a few dozen kilobytes. Reading them once per
    archive capture avoids a cache that could conceal a source revision.
    """
    return {name: sha256((ROOT/name).read_bytes()).hexdigest() for name in SOURCE_FILES}
