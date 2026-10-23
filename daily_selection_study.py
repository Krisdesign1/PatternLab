"""Immutable, offline comparison of daily selection rules.

The source snapshot and protocol precede every fit. The selected rule is saved
before the replay evaluates the test period. Production models and forecasts
are never replaced by this experiment.
"""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version as package_version
import json
from pathlib import Path
import platform
import re
from zoneinfo import ZoneInfo

import api_football as api
import daily_evaluation_store as storage

VERSION = 'daily-selection-study-store-v1'
RUNS_NAME = 'selection_study_runs'
ZONE = ZoneInfo('Africa/Kinshasa')
CODE_FILES = (
    'daily_selection_study.py', 'daily_selection_replay.py', 'daily_selection_statistics.py',
    'daily_analysis_evaluation.py', 'daily_analysis_engine.py', 'daily_evaluation_store.py',
    'match_predictor.py', 'scenario_predictor.py', 'league_patterns.py', 'league_model.py',
    'coupon_sizes.py', 'catboost_scenarios.py', 'scenario_features.py',
    'context_neighbors.py', 'contextual_scenarios.py', 'seasonal_validation.py', 'api_football.py', 'api_match_workspace.py',
)


class StudyError(ValueError):
    """The frozen experiment is absent, invalid or interrupted."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return sha256(_json(value).encode()).hexdigest()


def _read(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        raise StudyError(f'Le fichier figé {path.name} est absent ou illisible.') from None


def _write(path, value, *, immutable=False):
    try:
        storage._write(path, value, immutable=immutable)
    except storage.EvaluationStoreError as error:
        raise StudyError(str(error)) from None


def _protocol(today, end):
    import daily_selection_replay as replay
    import daily_selection_statistics as statistics
    return dict(version=VERSION, source='API-Football', timezone=ZONE.key,
        today=today.isoformat(), end_exclusive=end.isoformat(),
        periods={
            'train': dict(start='2022-01-01', end_exclusive='2025-01-01'),
            'calibration': dict(start='2025-01-01', end_exclusive='2025-07-01'),
            'selection': dict(start='2025-07-01', end_exclusive='2026-01-01'),
            'test': dict(start='2026-01-01', end_exclusive=end.isoformat())},
        predictor=deepcopy(replay.PROTOCOL), selection=deepcopy(statistics.PROTOCOL),
        exploratory=True, prior_history_explored=True, production_changed=False,
        interpretation='historical_reserved_test_within_this_experiment_not_new_blind_evidence')


def _signature():
    root = Path(__file__).resolve().parent
    try:
        files = {name: sha256((root/name).read_bytes()).hexdigest() for name in CODE_FILES}
        runtime = dict(python=platform.python_version(), catboost=package_version('catboost'))
    except (OSError, ValueError, PackageNotFoundError) as error:
        raise StudyError('Les dépendances ou les modules du comparatif sont incomplets.') from error
    return _hash(dict(files=files, runtime=runtime)), files, runtime


def _complete(directory):
    manifest, completed = _read(directory/'manifest.json'), _read(directory/'completed.json')
    identity = {key:manifest.get(key) for key in ('version','input_fingerprint','source_signature','protocol')}
    if (manifest.get('version') != VERSION or manifest.get('run_id') != directory.name
            or _hash(identity) != directory.name or completed.get('version') != VERSION
            or completed.get('run_id') != directory.name
            or _hash(dict(files=manifest.get('code_files'), runtime=manifest.get('runtime'))) != manifest.get('source_signature')):
        raise StudyError('L’identité du comparatif figé est invalide.')
    try:
        for name, digest in completed.get('files', {}).items():
            if name not in ('input.json','rules.json','report.json','review.md'):
                raise StudyError('Un fichier inconnu figure dans le manifeste du comparatif.')
            if sha256((directory/name).read_bytes()).hexdigest() != digest:
                raise StudyError(f'Le fichier figé {name} a changé.')
        if set(completed.get('files', {})) != {'input.json','rules.json','report.json','review.md'}:
            raise StudyError('Le comparatif ne contient pas tous ses fichiers figés.')
        if completed['files']['input.json'] != manifest['input_fingerprint']:
            raise StudyError('Les données figées ne correspondent plus au protocole.')
    except OSError:
        raise StudyError('Un fichier du comparatif figé est manquant.') from None
    report, rules = _read(directory/'report.json'), _read(directory/'rules.json')
    if (report.get('run_id') != directory.name or rules.get('run_id') != directory.name
            or report.get('input_fingerprint') != manifest['input_fingerprint']
            or report.get('source_signature') != manifest['source_signature']
            or report.get('rules_sha256') != completed['files']['rules.json']):
        raise StudyError('Le rapport et les règles ne correspondent plus au comparatif.')
    return report


def _review(report):
    rows = report.get('statistics', {}).get('by_size', [])
    selected = [dict(cohort=row.get('cohort'),size=row.get('size'),status=row.get('status'),
                     family=row.get('family'),rule=row.get('rule'),test=row.get('test')) for row in rows]
    return ('# Comparatif des règles de sélection quotidiennes\n\n'
        f"Données figées : {report['created_at']}. Source : API-Football.\n\n"
        'Apprentissage : 2022–2024 ; calibration : premier semestre 2025 ; '
        'choix des règles : second semestre 2025 ; test historique réservé : 2026 jusqu’à la fin exclusive indiquée.\n\n'
        '**Tous ces historiques ont déjà été explorés. Ce test rétrospectif ne constitue pas une nouvelle confirmation aveugle.** '
        'Les calendriers sont reconstruits depuis leur état final. Les règles sont figées avant les calculs du test de cette expérience. '
        'Les paramètres de production et les prévisions du journal restent inchangés.\n\n'
        'La confiance calibrée concerne l’événement « scénario exact parmi les trois choix ». '
        'Elle ne remplace pas les probabilités de la distribution des scénarios. '
        'Les tailles et les périmètres mono/multi-compétitions restent séparés.\n\n'
        f"Empreinte des données : `{report['input_fingerprint']}`.\n\n"
        '```json\n'+json.dumps(dict(inventory=report.get('inventory'),results=selected,
                                   warnings=report.get('warnings',[])),ensure_ascii=False,indent=2,allow_nan=False)+'\n```\n')


def run_study(*, root=None, today=None, end=None, progress=None):
    """Run or reuse one frozen experiment, without touching live predictions."""
    import daily_selection_replay as replay
    import daily_selection_statistics as statistics
    try:
        today, _, end = storage._dates(today, '2026-01-01', end)
        root = Path(api.DATA_DIR if root is None else root)
        snapshot, source_digest = storage._snapshot(root/'fixtures.json')
        frozen_input = dict(version=1, source='API-Football', fixtures=snapshot['fixtures'])
        encoded = _json(frozen_input)
        signature, code_files, runtime = _signature()
        protocol = _protocol(today, end)
        identity = dict(version=VERSION,input_fingerprint=sha256(encoded.encode()).hexdigest(),
                        source_signature=signature,protocol=protocol)
        run_id = _hash(identity)
        runs = root/RUNS_NAME
        with storage._lock(runs):
            directory = runs/run_id
            directory.mkdir(exist_ok=True)
            if (directory/'manifest.json').exists():
                manifest = _read(directory/'manifest.json')
                if any(manifest.get(key) != value for key,value in identity.items()):
                    raise StudyError('Le protocole de cette exécution ne correspond plus à ses données.')
            else:
                manifest = dict(identity,run_id=run_id,created_at=datetime.now(timezone.utc).isoformat(),
                    code_files=code_files,runtime=runtime,source_store_sha256=source_digest,
                    source_updated_at=snapshot.get('updated_at'),raw_fixtures=len(snapshot['fixtures']))
                _write(directory/'input.json', encoded, immutable=True)
                _write(directory/'manifest.json', manifest, immutable=True)
            if (directory/'input.json').read_text(encoding='utf-8') != encoded:
                raise StudyError('Les données de cette exécution ont changé.')
            if (directory/'completed.json').exists():
                report = _complete(directory)
                _write(runs/'latest.json',dict(version=VERSION,run_id=run_id))
                return report
            frozen = None
            def announce(*args, **kwargs):
                event = dict(status='running',updated_at=datetime.now(timezone.utc).isoformat(),
                             args=list(args),kwargs=kwargs)
                _write(directory/'progress.json',event)
                if progress is not None:
                    progress(*args,**kwargs)
            def before_test(records):
                nonlocal frozen
                if frozen is not None:
                    raise StudyError('La phase de sélection ne peut pas être exécutée deux fois.')
                if any(row.get('phase') == 'test' for row in records):
                    raise StudyError('Les règles doivent être choisies avant de consulter le test.')
                frozen = statistics.fit_rules(records, protocol=deepcopy(protocol['selection']))
                envelope = dict(version=VERSION,run_id=run_id,frozen_at=manifest['created_at'],
                                chosen_before_test=True,rules=frozen)
                _write(directory/'rules.json',envelope,immutable=True)
                announce('rules_frozen',dict(groups_before_test=len(records),path=str(directory/'rules.json')))
            announce('snapshot_frozen',dict(run_id=run_id,fixtures=manifest['raw_fixtures']))
            predictor = deepcopy(protocol['predictor'])
            predictor['end'] = end.isoformat()
            result = replay.replay(json.loads(encoded)['fixtures'],today=today,protocol=predictor,
                                   progress=announce,before_test=before_test)
            if frozen is None:
                raise StudyError('Le rejeu n’a pas figé de règles avant son test.')
            summary = statistics.evaluate_rules(result['records'], frozen, protocol=deepcopy(protocol['selection']))
            report = dict(result, statistics=summary,run_id=run_id,created_at=manifest['created_at'],
                protocol=protocol,inputs=protocol,input_fingerprint=identity['input_fingerprint'],
                source_signature=signature,rules_sha256=sha256((directory/'rules.json').read_bytes()).hexdigest(),
                report_path=str(directory/'report.json'))
            _write(directory/'report.json',report,immutable=True)
            _write(directory/'review.md',_review(report),immutable=True)
            marker = dict(version=VERSION,run_id=run_id,completed_at=datetime.now(timezone.utc).isoformat(),
                files={name:sha256((directory/name).read_bytes()).hexdigest()
                       for name in ('input.json','rules.json','report.json','review.md')})
            _write(directory/'completed.json',marker,immutable=True)
            _write(directory/'progress.json',dict(status='completed',updated_at=marker['completed_at']))
            _write(runs/'latest.json',dict(version=VERSION,run_id=run_id))
            return _complete(directory)
    except storage.EvaluationStoreError as error:
        raise StudyError(str(error)) from None


def load_latest(root=None):
    """Read the last verified result; never fit, forecast or create files."""
    runs = Path(api.DATA_DIR if root is None else root)/RUNS_NAME
    if not (runs/'latest.json').exists():
        return None
    pointer = _read(runs/'latest.json')
    if (pointer.get('version') != VERSION or not isinstance(pointer.get('run_id'),str)
            or not re.fullmatch('[0-9a-f]{64}',pointer['run_id'])):
        raise StudyError('La référence du comparatif de sélection est invalide.')
    return _complete(runs/pointer['run_id'])
