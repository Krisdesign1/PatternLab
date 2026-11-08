"""Local, pre-registered shadow comparisons; never change the serving model.

Studies pin both native artifacts and evaluation rules. Score-free forecasts are
write-once; result revisions use the existing journal's identity and score rules.
Only confirmed future fixtures on today's Kinshasa calendar may be recorded.
"""
from collections import Counter
from datetime import datetime, timezone
import copy
import hashlib
import json
from math import fsum
from math import log
from pathlib import Path
import shutil
import sqlite3
from zoneinfo import ZoneInfo

import api_football as api
import api_match_workspace as workspace
import catboost_scenarios as ml
import cloud_storage
import contextual_scenarios as contextual
import journal_protocol
import journal_uncertainty
import model_reassessment as review
import prediction_journal as journal
import prospective_models as models
from league_patterns import repeated_scenarios
from match_predictor import VERSION as CLUB_VERSION
from scenario_predictor import fit_sequences

VERSION = 'candidate-shadow-v1'
METHODS = ('candidate', 'incumbent', 'clubs', 'frequency')
TIMEZONE = 'Africa/Kinshasa'


def _root(root=None):
    return Path(root if root is not None else api.DATA_DIR).resolve()


def database_path(root=None):
    return _root(root)/'candidate_tracking.sqlite3'


def _write_allowed():
    cloud_storage._assert_writable()
    if cloud_storage.configured():
        raise ValueError('Le suivi des candidats exige le mode local.')


def _clock(clock=None):
    value = clock() if clock else datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('L’horloge du suivi doit préciser son fuseau horaire.')
    return value.astimezone(timezone.utc)


def _connect(root=None, create=False):
    path = database_path(root)
    if not create:
        if not path.exists():
            return None
        con = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=15)
        con.row_factory = sqlite3.Row
        return con
    con = journal._connect(path, create=True)
    con.executescript('''
        CREATE TABLE IF NOT EXISTS studies(id TEXT PRIMARY KEY, payload TEXT NOT NULL, digest TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS anchors(study_id TEXT NOT NULL, day TEXT NOT NULL, payload TEXT NOT NULL,
            digest TEXT NOT NULL, PRIMARY KEY(study_id,day));
        CREATE TRIGGER IF NOT EXISTS studies_no_update BEFORE UPDATE ON studies
            BEGIN SELECT RAISE(ABORT,'Study rules are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS studies_no_delete BEFORE DELETE ON studies
            BEGIN SELECT RAISE(ABORT,'Study rules are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS anchors_no_update BEFORE UPDATE ON anchors
            BEGIN SELECT RAISE(ABORT,'Calendar anchors are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS anchors_no_delete BEFORE DELETE ON anchors
            BEGIN SELECT RAISE(ABORT,'Calendar anchors are immutable'); END;
    ''')
    return con


def _implementation():
    directory = Path(__file__).resolve().parent
    names = ('catboost_scenarios.py', 'prospective_models.py', 'match_predictor.py',
             'scenario_features.py', 'scenario_predictor.py', 'league_patterns.py',
             'candidate_tracking.py', 'contextual_scenarios.py', 'club_identity.py')
    return {name: hashlib.sha256((directory/name).read_bytes()).hexdigest() for name in names}


def studies(root=None):
    con = _connect(root)
    if con is None:
        return []
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='studies'").fetchone():
            return []
        output = []
        for row in con.execute('SELECT * FROM studies ORDER BY rowid'):
            value = json.loads(row['payload'])
            if review._digest(value) != row['digest'] or value.get('id') != row['id'] or value.get('version') != VERSION:
                raise ValueError('Les règles d’un suivi de candidat sont altérées.')
            journal_protocol.validate_protocol(value['evidence_protocol'])
            output.append(value)
        return output
    except sqlite3.Error:
        raise ValueError('La base locale du suivi des candidats est illisible.') from None
    finally:
        con.close()


def _pin(report, source_dir, destination, league):
    base = ml._location('API-Football', league, source_dir)
    generation = report['deployment']['generation']
    native, raw_report = base/generation/'model.cbm', base/generation/'report.json'
    if hashlib.sha256(native.read_bytes()).hexdigest() != report['deployment']['model_sha256']:
        raise ValueError('Un modèle à figer est absent ou altéré.')
    target = ml._location('API-Football', league, destination)
    target.mkdir(parents=True, exist_ok=True)
    if not (target/generation).exists():
        shutil.copytree(base/generation, target/generation)
    review._write(target/'CURRENT.json', dict(generation=generation,
        report_sha256=hashlib.sha256(raw_report.read_bytes()).hexdigest()))
    if (ml.load_report('API-Football', league, destination) != report
            or hashlib.sha256((target/generation/'model.cbm').read_bytes()).hexdigest() != report['deployment']['model_sha256']):
        raise ValueError('La copie figée du modèle est incomplète ou altérée.')


def register(league, run_id, root=None, *, now=None):
    """Explicit enrollment pins exact versions before any future prediction."""
    _write_allowed()
    root = _root(root)
    if now is not None and root == Path(api.DATA_DIR).resolve():
        raise ValueError('L’enregistrement réel utilise uniquement l’horloge UTC.')
    current = _clock((lambda: now) if now is not None else None)
    item = review.load_run(league, run_id, root)
    if item['status'] != 'completed' or item['report']['policy'].get('evaluation_procedure') != 'native':
        raise ValueError('Préparez d’abord un candidat avec la procédure native alignée.')
    if current < journal._stamp(item['frozen_at']):
        raise ValueError('Le suivi ne peut pas précéder la création du candidat.')
    if item.get('completed_at') and current < journal._stamp(item['completed_at']):
        raise ValueError('Le suivi ne peut pas précéder la fin de préparation du candidat.')
    base, production = review._paths(league, root)
    incumbent = ml.load_report('API-Football', league, production)
    if incumbent is None or incumbent['deployment']['generation'] != item['incumbent_generation']:
        raise ValueError('Le modèle de référence a changé depuis la préparation du candidat.')
    study_id = review._digest(dict(version=VERSION, league=str(league), run_id=run_id,
        candidate=item['candidate_generation'], incumbent=item['incumbent_generation'], implementation=_implementation()))
    with review._lock(base):
        existing = next((s for s in studies(root) if s['id'] == study_id), None)
        if existing:
            return existing
        protocol = dict(copy.deepcopy(journal_protocol.CRITERIA), registered_at=current.isoformat())
        protocol['fingerprint'] = journal_protocol._fingerprint(protocol)
        study = dict(version=VERSION, id=study_id, league=str(league), run_id=run_id,
            registered_at=current.isoformat(), sizes=item['sizes'], timezone=TIMEZONE,
            candidate_generation=item['candidate_generation'], incumbent_generation=item['incumbent_generation'],
            clubs_version=CLUB_VERSION, frequency_version='league-frequency-v1', implementation=_implementation(),
            evidence_protocol=protocol, primary='paired whole-group logloss', secondary='top3',
            comparisons=[['candidate','clubs'],['candidate','incumbent'],['incumbent','clubs'],['frequency','clubs']],
            calibration='Exact saved joint marginals, per size; Brier/logloss and classwise ten-bin reliability.',
            population='All confirmed calendar fixtures for this league today; chronological blocks per size anchored once per day. No probability threshold. Groups crossing the registration or current kickoff cannot be backfilled.',
            selection='All registered candidates are tracked; no automatic historical winner selection.',
            replacement='Descriptive only. No automatic promotion, threshold change or retrospective forecast.',
            engine_scope='Each size separate; shared fixture IDs and UTC weeks form dependency blocks.',
            incomplete='Pending/unresolved results stay outside mature comparisons; void is reported separately.')
        folder = root/'candidate_studies'/study_id
        _pin(item['report']['native'], review._run_path(league, run_id, root)/'candidate_models', folder/'candidate', league)
        _pin(incumbent, production, folder/'incumbent', league)
        if ml.load_report('API-Football',league,production) != incumbent:
            raise ValueError('Le modèle en service a changé pendant l’activation du suivi.')
        con = _connect(root, create=True)
        try:
            encoded = review._json(study)
            con.execute('INSERT INTO studies VALUES(?,?,?)', (study_id, encoded, review._digest(study)))
            con.commit()
        finally:
            con.close()
    return study


def _baselines(rows, fixtures):
    league, before = fixtures[0]['league'], fixtures[0]['timestamp'][:10]
    history = ml._rows(rows, league, 'API-Football', before)
    blocks = repeated_scenarios(history, len(fixtures)).get(league, dict(blocks=[], matches=history))
    result, context = contextual._prepare(blocks, fixtures)
    if not result['eligible']:
        raise ValueError(result.get('diagnostic') or 'Historique insuffisant pour les références.')
    provenance = dict(source='API-Football', league=league, training_before=before,
        training_groups=context['fit'][1], training_matches=len(history), history_fingerprint=ml._fingerprint(history))
    return dict(clubs=models._method(CLUB_VERSION, models._distribution('independent', len(fixtures),
            distributions=context['distributions']), provenance),
        frequency=models._method('league-frequency-v1', models._distribution('frequency', len(fixtures),
            fit=context['fit']), provenance))


def _methods(rows, fixtures, study, root):
    if study['implementation'] != _implementation():
        raise ValueError('Le code des moteurs a changé ; ce suivi exige une nouvelle version explicite.')
    folder = root/'candidate_studies'/study['id']
    league, before = study['league'], fixtures[0]['timestamp'][:10]
    methods = _baselines(rows, fixtures)
    for name in ('candidate', 'incumbent'):
        method = models._catboost(rows, fixtures, 'API-Football', league, before, None, None,
                                 directory=folder/name)
        if method['generation'] != study[name+'_generation']:
            raise ValueError('La génération d’un modèle suivi a changé.')
        methods[name] = method
    return methods


def _anchors(study, raw, day, root):
    con = _connect(root, create=True)
    try:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT payload,digest FROM anchors WHERE study_id=? AND day=?', (study['id'], day)).fetchone()
        if row is None:
            fixtures = []
            for item in raw:
                try:
                    fixture = workspace.fixture_from_raw(item)
                    if fixture['league'] == study['league'] and journal._stamp(fixture['timestamp']).astimezone(ZoneInfo(TIMEZONE)).date().isoformat() == day:
                        fixtures.append(fixture)
                except (ValueError, TypeError, KeyError):
                    continue
            fixtures.sort(key=lambda r: (r['timestamp'], int(r['match_id'])))
            value = [dict(size=size, fixtures=fixtures[i:i+size]) for size in study['sizes']
                     for i in range(0, len(fixtures)-size+1, size)]
            # Empty early calendar is not a durable plan; wait for actual data.
            if fixtures:
                con.execute('INSERT INTO anchors VALUES(?,?,?,?)', (study['id'], day, review._json(value), review._digest(value)))
        else:
            value = json.loads(row['payload'])
            if review._digest(value) != row['digest']:
                raise ValueError('Les groupes du calendrier suivi sont altérés.')
        con.commit()
        return value
    finally:
        con.close()


def _record(study, fixtures, methods, raw, root, clock):
    fixtures = journal._fixtures(fixtures)
    current = _clock(clock)
    if current < journal._stamp(study['registered_at']):
        raise ValueError('Le suivi ne peut pas précéder son protocole.')
    journal._check_time(fixtures, current, True)
    checks = {str(item['fixture']['id']): item for item in raw}
    for fixture in fixtures:
        check = workspace.fixture_from_raw(checks[fixture['match_id']])
        if api._status_short(checks[fixture['match_id']]) != 'NS' or any(str(check[k]) != str(fixture[k]) for k in ('match_id','league','home_id','away_id','timestamp')):
            raise ValueError('API-Football ne confirme plus les affiches suivies.')
    if set(methods) != set(METHODS):
        raise ValueError('Les quatre méthodes doivent être présentes sur le même groupe.')
    for name, method in methods.items():
        models.validate_method(method, len(fixtures))
        if (method['training_before'] != fixtures[0]['timestamp'][:10]
                or method.get('source') != 'API-Football' or method.get('league') != study['league']):
            raise ValueError('Coupure historique du suivi invalide.')
        if name in ('candidate','incumbent') and method.get('generation') != study[name+'_generation']:
            raise ValueError('Version de modèle suivie incompatible.')
    key = review._digest([study['id'], journal._group_key(fixtures)])
    payload = dict(version=VERSION, source='API-Football', league=study['league'], size=len(fixtures),
        fixtures=fixtures, methods=methods, metadata=dict(study_id=study['id'], run_id=study['run_id']))
    encoded = review._json(payload)
    con = _connect(root, create=True)
    try:
        con.execute('BEGIN IMMEDIATE')
        commit_time = _clock(clock)
        journal._check_time(fixtures, commit_time, True)
        cursor = con.execute('INSERT OR IGNORE INTO predictions VALUES(?,?,?,?)',
            (key, commit_time.isoformat(), encoded, hashlib.sha256(encoded.encode()).hexdigest()))
        journal._check_time(fixtures, _clock(clock), True)
        con.commit()
        return bool(cursor.rowcount)
    finally:
        con.close()


def run_cycle(root=None, *, clock=None, refresh=None, check_time=None):
    """Called by the existing local scheduler, with its provider/quota gate.

    No training occurs. Fresh status confirmation follows all inference, and
    the real clock is checked again in the forecast's committing transaction.
    """
    _write_allowed()
    root = _root(root)
    registered = studies(root)
    report = dict(status='idle', studies=len(registered), created=0, existing=0, unavailable=0, issues=[])
    if not registered:
        return report
    current = _clock(clock)
    day = current.astimezone(ZoneInfo(TIMEZONE)).date().isoformat()
    rows, _ = api.load_matches(root/'fixtures.json')
    raw = api.load_fixtures(root/'fixtures.json')
    existing = {entry['id'] for entry in journal.list_entries(database_path(root))}
    report['status'] = 'completed'
    for study in registered:
        for group in _anchors(study, raw, day, root):
            if check_time:
                check_time()
            fixtures = group['fixtures']
            key = review._digest([study['id'], journal._group_key(fixtures)])
            if key in existing:
                report['existing'] += 1
                continue
            try:
                journal._check_time(fixtures, _clock(clock), True)
                if min(journal._stamp(f['timestamp']) for f in fixtures) <= journal._stamp(study['registered_at']):
                    continue
                methods = _methods(rows, fixtures, study, root)
                if refresh is None:
                    raise ValueError('Une confirmation API récente est nécessaire avant enregistrement.')
                fresh = refresh(ids='-'.join(f['match_id'] for f in fixtures), timezone='UTC')
                if {str(f['fixture']['id']) for f in fresh} != {f['match_id'] for f in fixtures}:
                    raise ValueError('Le relevé API du suivi est incomplet.')
                api.save_fixtures(fresh, root/'fixtures.json')
                created = _record(study, fixtures, methods, fresh, root, clock)
                report['created'] += int(created)
                existing.add(key)
            except (ValueError, OSError, KeyError, TypeError) as error:
                # Propagate the scheduler's pause/provider errors to its gate.
                from automation_worker import _Pause
                if isinstance(error, _Pause):
                    raise
                report['unavailable'] += 1
                message = str(error) if isinstance(error, ValueError) else 'Suivi de candidat indisponible.'
                if message not in report['issues']:
                    report['issues'].append(message[:300])
    return report


def reconcile(root=None, *, now=None):
    return journal.reconcile(path=database_path(root), raw_fixtures=api.load_fixtures(_root(root)/'fixtures.json'), now=now)


def summary(study_id, root=None, *, now=None):
    """Read immutable snapshots; same-size paired descriptive evidence only."""
    study = next((s for s in studies(root) if s['id'] == study_id), None)
    if study is None:
        raise ValueError('Suivi de candidat inconnu.')
    entries = [e for e in journal.list_entries(database_path(root)) if e['metadata']['study_id'] == study_id]
    output = []
    aliases = dict(candidate='catboost', incumbent='combined', clubs='clubs', frequency='frequency')
    for size in study['sizes']:
        group = [e for e in entries if e['size'] == size]
        mapped = [dict(e, methods={aliases[k]:v for k,v in e['methods'].items()},
                       evaluations={aliases[k]:v for k,v in e['evaluations'].items()}) for e in group]
        evidence = journal_uncertainty.evaluate(mapped, aliases.values(), study['evidence_protocol'], now=now)
        incumbent_pair=[dict(e,methods=dict(clubs=e['methods']['incumbent'],catboost=e['methods']['candidate']),
            evaluations={alias:e['evaluations'][name] for name,alias in (('incumbent','clubs'),('candidate','catboost'))
                         if name in e['evaluations']}) for e in group]
        candidate_vs_incumbent=journal_uncertainty.evaluate(incumbent_pair,('clubs','catboost'),
            study['evidence_protocol'],now=now)['comparisons'][0]
        candidate_vs_incumbent['reason']=candidate_vs_incumbent['reason'].replace('clubs seuls','CatBoost courant figé')
        metrics = []
        settled = [e for e in group if e['status'] == 'settled']
        for name in METHODS:
            scores = [e['evaluations'][name] for e in settled]
            metrics.append(dict(method=name, groups=len(scores),
                loss=fsum(s['loss'] for s in scores)/len(scores) if scores else None,
                top1=sum(s['top1'] for s in scores), top3=sum(s['top3'] for s in scores)))
        output.append(dict(size=size, counts=dict(Counter(e['status'] for e in group)), metrics=metrics,
            evidence=evidence, candidate_vs_incumbent=candidate_vs_incumbent, calibration=_calibration(group),
            unique_matches=len({f['match_id'] for e in group for f in e['fixtures']})))
    return dict(study=study, sizes=output, entries=entries, total=len(entries),
        unique_matches=len({f['match_id'] for e in entries for f in e['fixtures']}), promoted=False)


def _calibration(entries):
    """Earliest common forecast per fixture and size, before outcome filtering."""
    from saved_model_comparison import marginals
    from league_model import VALUES
    first = {}
    for entry in sorted(entries, key=lambda e:(e['created_at'],e['id'])):
        for index,fixture in enumerate(entry['fixtures']):
            first.setdefault(fixture['match_id'],(entry,index))
    usable = [(e,index) for e,index in first.values() if e['status']=='settled']
    cached = {}
    output = {}
    for name in METHODS:
        records = []
        for entry,index in usable:
            key=(entry['id'],name)
            if key not in cached: cached[key]=marginals(entry['methods'][name])
            records.append((entry['actual'][index],cached[key][index]))
        n=len(records)
        reliability={}
        for label in VALUES:
            bins=[[] for _ in range(10)]
            for actual,p in records: bins[min(9,int(p[label]*10))].append((p[label],actual==label))
            reliability[label]=[dict(lower=i/10,upper=(i+1)/10,count=len(bucket),
                mean_probability=fsum(p for p,_ in bucket)/len(bucket) if bucket else None,
                observed_rate=sum(v for _,v in bucket)/len(bucket) if bucket else None)
                for i,bucket in enumerate(bins)]
        output[name]=dict(matches=n,logloss=fsum(-log(max(p[a],1e-300)) for a,p in records)/n if n else None,
            brier=fsum(fsum((p[v]-int(a==v))**2 for v in VALUES) for a,p in records)/n if n else None,
            reliability=reliability)
    return output
