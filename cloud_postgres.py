"""Small PostgreSQL store for fenced, versioned workspace checkpoints.

No API-Football calls occur here. Every public error is deliberately free of
connection strings and driver exception text.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import re
from urllib.parse import parse_qs, urlsplit


class BackendError(RuntimeError):
    pass


class BackendBusy(BackendError):
    pass


class LeaseLost(BackendError):
    pass


@dataclass(frozen=True)
class Lease:
    owner: str
    fence: int


def validate_dsn(dsn, *, allow_localhost=False):
    """Require encrypted production connections; accept no arbitrary schemes."""
    if not isinstance(dsn, str) or not dsn.strip():
        raise BackendError('La connexion de stockage distant est absente.')
    try:
        if dsn.startswith(('postgres://', 'postgresql://')):
            parsed = urlsplit(dsn)
            host = parsed.hostname
            modes = parse_qs(parsed.query).get('sslmode', [])
            mode = modes[-1] if modes else None
        else:
            from psycopg.conninfo import conninfo_to_dict
            parsed = conninfo_to_dict(dsn)
            host, mode = parsed.get('host'), parsed.get('sslmode')
        local = host in ('localhost', '127.0.0.1', '::1')
        if mode not in ('require', 'verify-ca', 'verify-full') and not (allow_localhost and local):
            raise ValueError()
        if not host:
            raise ValueError()
    except Exception as error:
        if isinstance(error, BackendError):
            raise
        raise BackendError('La connexion distante doit utiliser TLS (sslmode=require ou vérification renforcée).') from None
    return dsn


class PostgresBackend:
    def __init__(self, dsn, workspace, *, lease_seconds=300, allow_localhost=False):
        self.dsn = validate_dsn(dsn, allow_localhost=allow_localhost)
        if not isinstance(workspace, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', workspace):
            raise BackendError('Le nom de l’espace de stockage distant est invalide.')
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise BackendError('La durée du verrou distant est invalide.')
        self.workspace, self.lease_seconds = workspace, lease_seconds
        try:
            from psycopg.conninfo import conninfo_to_dict
            target = conninfo_to_dict(self.dsn)
            identity = {name: target.get(name, '') for name in ('host', 'hostaddr', 'port', 'dbname', 'user')}
            self.identity = sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        except Exception:
            raise BackendError('La connexion PostgreSQL configurée est invalide.') from None
        self._ready = False

    @contextmanager
    def _connection(self):
        try:
            import psycopg
            with psycopg.connect(self.dsn, connect_timeout=10,
                                 options='-c statement_timeout=10000 -c lock_timeout=10000') as con:
                yield con
        except BackendError:
            raise
        except Exception:
            raise BackendError('Le stockage distant est indisponible ; aucune écriture locale n’est autorisée.') from None

    def initialize(self):
        if self._ready:
            return
        with self._connection() as con:
            con.execute('CREATE SCHEMA IF NOT EXISTS patternlab')
            con.execute('''CREATE TABLE IF NOT EXISTS patternlab.workspace_heads (
                workspace TEXT PRIMARY KEY, revision BIGINT NOT NULL DEFAULT 0,
                manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
                previous_manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
                lease_owner TEXT, lease_until TIMESTAMPTZ, fence BIGINT NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp())''')
            con.execute('''CREATE TABLE IF NOT EXISTS patternlab.workspace_blobs (
                workspace TEXT NOT NULL REFERENCES patternlab.workspace_heads(workspace),
                digest TEXT NOT NULL, content BYTEA NOT NULL, raw_size BIGINT NOT NULL,
                PRIMARY KEY(workspace,digest), CHECK(raw_size >= 0))''')
        self._ready = True

    def status(self):
        self.initialize()
        with self._connection() as con:
            row = con.execute('SELECT revision,manifest,updated_at FROM patternlab.workspace_heads WHERE workspace=%s',
                              (self.workspace,)).fetchone()
        if row is None:
            return dict(revision=0, manifest={}, updated_at=None)
        return dict(revision=row[0], manifest=row[1], updated_at=row[2].isoformat())

    def acquire(self, owner):
        self.initialize()
        with self._connection() as con:
            con.execute('INSERT INTO patternlab.workspace_heads(workspace) VALUES(%s) ON CONFLICT DO NOTHING',
                        (self.workspace,))
            row = con.execute('''UPDATE patternlab.workspace_heads
                SET lease_owner=%s, lease_until=clock_timestamp()+(%s * interval '1 second'), fence=fence+1
                WHERE workspace=%s AND (lease_until IS NULL OR lease_until <= clock_timestamp())
                RETURNING fence,revision,manifest''', (owner, self.lease_seconds, self.workspace)).fetchone()
            if row is None:
                raise BackendBusy('Un autre traitement utilise les données ; réessayez après sa fin.')
        return Lease(owner, row[0]), row[1], row[2]

    def _where(self, lease):
        return self.workspace, lease.owner, lease.fence

    def assert_lease(self, lease):
        with self._connection() as con:
            row = con.execute('''SELECT 1 FROM patternlab.workspace_heads
                WHERE workspace=%s AND lease_owner=%s AND fence=%s AND lease_until > clock_timestamp()''',
                              self._where(lease)).fetchone()
            if row is None:
                raise LeaseLost('Le verrou distant a expiré ; le traitement est arrêté sans publier ses données.')

    def renew(self, lease):
        with self._connection() as con:
            row = con.execute('''UPDATE patternlab.workspace_heads
                SET lease_until=clock_timestamp()+(%s * interval '1 second')
                WHERE workspace=%s AND lease_owner=%s AND fence=%s AND lease_until > clock_timestamp()
                RETURNING fence''', (self.lease_seconds, *self._where(lease))).fetchone()
            if row is None:
                raise LeaseLost('Le verrou distant a expiré ; il ne peut pas être renouvelé.')

    def release(self, lease):
        with self._connection() as con:
            con.execute('''UPDATE patternlab.workspace_heads SET lease_owner=NULL,lease_until=NULL
                WHERE workspace=%s AND lease_owner=%s AND fence=%s''', self._where(lease))

    def read_blob(self, digest):
        with self._connection() as con:
            row = con.execute('SELECT content,raw_size FROM patternlab.workspace_blobs WHERE workspace=%s AND digest=%s',
                              (self.workspace, digest)).fetchone()
            if row is None:
                raise BackendError('Un fichier du point de reprise distant est absent.')
        return bytes(row[0]), row[1]

    def upload(self, lease, digest, content, raw_size):
        # Lock the head row during every object write so a competing acquisition
        # cannot race an expired owner between validation and INSERT.
        with self._connection() as con:
            row = con.execute('''SELECT 1 FROM patternlab.workspace_heads
                WHERE workspace=%s AND lease_owner=%s AND fence=%s AND lease_until > clock_timestamp()
                FOR UPDATE''', self._where(lease)).fetchone()
            if row is None:
                raise LeaseLost('Le verrou distant a expiré ; transfert annulé.')
            con.execute('''INSERT INTO patternlab.workspace_blobs(workspace,digest,content,raw_size)
                VALUES(%s,%s,%s,%s) ON CONFLICT(workspace,digest) DO NOTHING''',
                        (self.workspace, digest, content, raw_size))

    def publish(self, lease, expected_revision, manifest):
        entries = manifest.get('files', {})
        with self._connection() as con:
            head = con.execute('''SELECT revision,manifest FROM patternlab.workspace_heads
                WHERE workspace=%s AND lease_owner=%s AND fence=%s AND lease_until > clock_timestamp()
                FOR UPDATE''', self._where(lease)).fetchone()
            if head is None or head[0] != expected_revision:
                raise LeaseLost('La version distante a changé ; aucun état concurrent n’a été remplacé.')
            for entry in entries.values():
                blob = con.execute('''SELECT raw_size,octet_length(content) FROM patternlab.workspace_blobs
                    WHERE workspace=%s AND digest=%s''', (self.workspace, entry['sha256'])).fetchone()
                if blob != (entry['size'], entry['compressed_size']):
                    raise BackendError('Le point de reprise est incomplet ; publication annulée.')
            # Re-check database time after the potentially large validation.
            row = con.execute('''UPDATE patternlab.workspace_heads
                SET previous_manifest=manifest, manifest=%s::jsonb,revision=revision+1,updated_at=clock_timestamp()
                WHERE workspace=%s AND lease_owner=%s AND fence=%s AND lease_until > clock_timestamp()
                  AND revision=%s RETURNING revision''',
                (json.dumps(manifest), *self._where(lease), expected_revision)).fetchone()
            if row is None:
                raise LeaseLost('Le verrou distant a expiré avant la publication ; état conservé.')
            keep = {item['sha256'] for value in (manifest, head[1]) for item in value.get('files', {}).values()}
            if keep:
                con.execute('DELETE FROM patternlab.workspace_blobs WHERE workspace=%s AND NOT (digest=ANY(%s))',
                            (self.workspace, sorted(keep)))
            else:
                con.execute('DELETE FROM patternlab.workspace_blobs WHERE workspace=%s', (self.workspace,))
        return row[0]
