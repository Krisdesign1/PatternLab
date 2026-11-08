"""Local resumable statistics sidecar, independent of forecasts and Cloud."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3

from halftime_statistics_quality import xg_consistency_issues


class StoreError(RuntimeError):
    pass


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def timestamp(value=None):
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError('Un instant avec fuseau horaire est requis.')
    return value.astimezone(timezone.utc).timestamp()


class Store:
    def __init__(self, path, *, readonly=False):
        self.path = Path(path)
        self.readonly = readonly
        if not readonly:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        if readonly and not self.path.exists():
            self.db = None
            return
        self.db = sqlite3.connect(self.path.resolve().as_uri() + ('?mode=ro' if readonly else '?mode=rwc'),
                                  uri=True, timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA busy_timeout=5000')
        if readonly:
            self.db.execute('PRAGMA query_only=ON')
        else:
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks(
                    match_id TEXT PRIMARY KEY,league TEXT NOT NULL,season INTEGER NOT NULL,
                    kickoff_utc TEXT NOT NULL,home_id TEXT NOT NULL,away_id TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',availability TEXT NOT NULL DEFAULT 'unobserved',
                    attempts INTEGER NOT NULL DEFAULT 0,next_at REAL NOT NULL DEFAULT 0,
                    latest_observation INTEGER,priority INTEGER NOT NULL DEFAULT 100);
                CREATE INDEX IF NOT EXISTS tasks_due ON tasks(state,next_at,priority,kickoff_utc);
                CREATE TABLE IF NOT EXISTS attempts(
                    id INTEGER PRIMARY KEY,match_id TEXT NOT NULL REFERENCES tasks(match_id),
                    source TEXT NOT NULL,started_at REAL NOT NULL,finished_at REAL,
                    envelope_json TEXT,sha256 TEXT UNIQUE,classification TEXT,parser_version TEXT,parsed_json TEXT);
                CREATE INDEX IF NOT EXISTS attempts_information_time ON attempts(match_id,finished_at,id);
                CREATE TABLE IF NOT EXISTS measurements(
                    observation_id INTEGER NOT NULL REFERENCES attempts(id),period TEXT NOT NULL,
                    team_id TEXT NOT NULL,metric TEXT NOT NULL,value REAL NOT NULL,
                    PRIMARY KEY(observation_id,period,team_id,metric));
                CREATE INDEX IF NOT EXISTS measurement_team ON measurements(team_id,period,metric);
                CREATE TABLE IF NOT EXISTS discoveries(
                    league TEXT NOT NULL,season INTEGER NOT NULL,observed_at REAL NOT NULL,
                    payload_json TEXT NOT NULL,PRIMARY KEY(league,season));
            ''')
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES('schema_version','1')")
        row = self.db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        if row is None or row[0] != '1':
            self.close()
            raise StoreError('Version du stockage de statistiques incompatible.')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    @contextmanager
    def transaction(self):
        if self.readonly:
            raise StoreError('Stockage ouvert en lecture seule.')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    @contextmanager
    def lock(self):
        """One collector process at a time; kernel releases lock after a crash."""
        if self.readonly:
            raise StoreError('Stockage ouvert en lecture seule.')
        path = self.path.with_suffix(self.path.suffix + '.collector.lock')
        with path.open('a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StoreError('Un collecteur de statistiques est déjà en cours.') from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def get_meta(self, key, default=None):
        row = self.db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (key, encoded(value)))

    def enqueue(self, fixtures, *, priority=100):
        created = changed = 0
        with self.transaction():
            for row in fixtures:
                values = tuple(row[k] for k in ('match_id','league','season','kickoff_utc','home_id','away_id'))
                old = self.db.execute('SELECT * FROM tasks WHERE match_id=?', (row['match_id'],)).fetchone()
                if old is None:
                    self.db.execute('''INSERT INTO tasks(match_id,league,season,kickoff_utc,home_id,away_id,priority)
                        VALUES (?,?,?,?,?,?,?)''', (*values,priority))
                    created += 1
                elif tuple(old[k] for k in ('match_id','league','season','kickoff_utc','home_id','away_id')) != values:
                    # Old raw observations remain; old projections are no longer current.
                    # Keep this boundary after a new observation arrives as well: a
                    # historical lookup must not relabel old measurements with the
                    # corrected fixture's league, kickoff or teams.
                    boundary = self.db.execute('SELECT COALESCE(MAX(id),0) FROM attempts WHERE match_id=?',
                                               (row['match_id'],)).fetchone()[0]
                    self.set_meta('task_identity_after:' + str(row['match_id']), boundary)
                    self.db.execute('''UPDATE tasks SET league=?,season=?,kickoff_utc=?,home_id=?,away_id=?,
                        state='pending',availability='unobserved',next_at=0,latest_observation=NULL WHERE match_id=?''',
                        (*values[1:],row['match_id']))
                    changed += 1
        return dict(created=created, metadata_changed=changed)

    def due(self, now):
        row = self.db.execute('''SELECT * FROM tasks WHERE state IN ('pending','retry') AND next_at<=?
            ORDER BY priority,attempts,kickoff_utc,CAST(match_id AS INTEGER) LIMIT 1''', (timestamp(now),)).fetchone()
        return dict(row) if row else None

    def reserve(self, match_id, *, source, now):
        with self.transaction():
            cursor = self.db.execute('INSERT INTO attempts(match_id,source,started_at) VALUES(?,?,?)',
                                     (match_id,source,timestamp(now)))
            self.db.execute('UPDATE tasks SET attempts=attempts+1 WHERE match_id=?', (match_id,))
            return cursor.lastrowid

    def recover(self, now):
        with self.transaction():
            cursor = self.db.execute("UPDATE attempts SET finished_at=?,classification='interrupted' WHERE finished_at IS NULL",
                                     (timestamp(now),))
            return cursor.rowcount

    def has_response(self, envelope):
        digest = hashlib.sha256(encoded(envelope).encode()).hexdigest()
        return self.db.execute('SELECT id FROM attempts WHERE sha256=?', (digest,)).fetchone() is not None

    def finish(self, attempt, task, envelope, parsed, *, state, availability, next_at=0, now=None):
        digest = hashlib.sha256(encoded(envelope).encode()).hexdigest()
        with self.transaction():
            self.db.execute('''UPDATE attempts SET finished_at=?,envelope_json=?,sha256=?,classification=?,
                parser_version=?,parsed_json=? WHERE id=? AND finished_at IS NULL''',
                (timestamp(now),encoded(envelope),digest,availability,parsed.get('parser_version'),encoded(parsed),attempt))
            for period, block in parsed.get('periods', {}).items():
                for metric, values in block.get('pairs', {}).items():
                    for team, value in zip((task['home_id'],task['away_id']),values):
                        self.db.execute('INSERT INTO measurements VALUES(?,?,?,?,?)', (attempt,period,team,metric,value))
            self.db.execute('''UPDATE tasks SET state=?,availability=?,next_at=?,latest_observation=?
                WHERE match_id=?''', (state,availability,next_at,attempt,task['match_id']))

    def status(self):
        if self.db is None:
            return dict(exists=False, path=str(self.path), queued=0, states={})
        states = {r[0]:r[1] for r in self.db.execute('SELECT state,COUNT(*) FROM tasks GROUP BY state')}
        available = {r[0]:r[1] for r in self.db.execute('SELECT availability,COUNT(*) FROM tasks GROUP BY availability')}
        return dict(exists=True,path=str(self.path),queued=sum(states.values()),states=states,availability=available,
                    attempts=self.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0],
                    measurements=self.db.execute('SELECT COUNT(*) FROM measurements').fetchone()[0],
                    next_retry_at=self.db.execute("SELECT MIN(next_at) FROM tasks WHERE state='retry'").fetchone()[0],
                    provider_pause_until=self.get_meta('provider_pause_until',0))

    def before(self, team_id, before, *, observed_before=None):
        """Only strictly older matches; optional actual information-time cutoff.

        With a cutoff, choose the latest completed response strictly before it,
        then expose only that response's ready/partial measurements. A newer
        empty, invalid or failed response does not revive older measurements;
        unfinished/interrupted requests without a response do not supersede one.
        Metadata changes invalidate prior-identity observations. Kickoff alone
        does not establish when a historical statistic was known.

        In either mode, inconsistent xG triplets are hidden for both teams and
        all periods in the returned projection; stored measurements stay intact.
        """
        if self.db is None:
            return []
        if observed_before is not None:
            query = '''WITH known AS (
                SELECT previous.id,previous.match_id,previous.finished_at,previous.classification,
                    ROW_NUMBER() OVER (PARTITION BY previous.match_id
                        ORDER BY previous.finished_at DESC,previous.id DESC) AS recency
                FROM attempts previous JOIN tasks t ON t.match_id=previous.match_id
                LEFT JOIN metadata boundary ON boundary.key='task_identity_after:'||t.match_id
                WHERE previous.finished_at<? AND previous.envelope_json IS NOT NULL
                    AND previous.id>COALESCE(CAST(boundary.value AS INTEGER),0)
                    AND t.kickoff_utc<? AND t.latest_observation IS NOT NULL
                    AND ? IN (t.home_id,t.away_id))
                SELECT t.match_id,t.league,t.kickoff_utc,a.finished_at AS collected_at,a.id AS observation_id,
                m.period,m.metric,m.value FROM known a JOIN tasks t ON t.match_id=a.match_id
                JOIN measurements m ON m.observation_id=a.id
                WHERE a.recency=1 AND m.team_id=? AND a.classification IN ('ready','partial')
                ORDER BY t.kickoff_utc,t.match_id,m.period,m.metric'''
            return self._quality_projection(self.db.execute(query, (timestamp(observed_before),before,str(team_id),str(team_id))))
        query = '''SELECT t.match_id,t.league,t.kickoff_utc,a.finished_at AS collected_at,a.id AS observation_id,
            m.period,m.metric,m.value FROM measurements m JOIN attempts a ON a.id=m.observation_id
            JOIN tasks t ON t.match_id=a.match_id AND t.latest_observation=a.id
            WHERE m.team_id=? AND t.kickoff_utc<? AND t.availability IN ('ready','partial')'''
        params = [str(team_id),before]
        query += ' ORDER BY t.kickoff_utc,t.match_id,m.period,m.metric'
        return self._quality_projection(self.db.execute(query,params))

    def _quality_projection(self, rows):
        """Check selected observations, including the other team's xG only.

        Measurements from a different observation never complete a triplet.
        The shared pure gate keeps this read projection consistent with future
        collection, including legacy measurements stored before that gate.
        """
        rows = [dict(row) for row in rows]
        observations = sorted({row['observation_id'] for row in rows if row['metric']=='expected_goals'})
        measurements = {}
        for offset in range(0,len(observations),500):
            chunk = observations[offset:offset+500]
            query = '''SELECT observation_id,period,team_id,value FROM measurements
                WHERE metric='expected_goals' AND observation_id IN ('''+','.join('?' for _ in chunk)+')'
            for row in self.db.execute(query,chunk):
                measurements.setdefault(row['observation_id'],{}).setdefault(row['team_id'],{})[row['period']] = row['value']
        invalid = set()
        for observation,teams in measurements.items():
            # Only additivity matters here, so stable team order suffices and
            # avoids consulting mutable fixture metadata a second time.
            if len(teams)>2:
                invalid.add(observation)
                continue
            periods = {period:[values.get(period) for values in teams.values()]+[None]*(2-len(teams))
                       for period in ('full','first','second')}
            if xg_consistency_issues(periods):
                invalid.add(observation)
        result = []
        for row in rows:
            observation = row.pop('observation_id')
            if row['metric']!='expected_goals' or observation not in invalid:
                result.append(row)
        return result
