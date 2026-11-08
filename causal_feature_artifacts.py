"""Exclusive, self-contained local feature bundles, separate from active models."""
import json
import os
from pathlib import Path
import re

from causal_feature_workspace import VERSION,digest
from fixture_temporal import utc_instant


def _write(path,value):
    with path.open('x',encoding='utf-8') as stream:
        json.dump(value,stream,ensure_ascii=False,sort_keys=True,separators=(',', ':'),allow_nan=False)
        stream.flush();os.fsync(stream.fileno())


def _identity(value):
    if not isinstance(value,str) or re.fullmatch('[a-f0-9]{64}',value) is None:
        raise ValueError('Identifiant d’instantané invalide.')
    return value


def _seal(value):
    if not isinstance(value,dict) or value.get('id')!=digest({key:item for key,item in value.items() if key!='id'}):
        raise ValueError('Le contenu scellé a été modifié.')


def validate_cohort(manifest,snapshots):
    from prematch_feature_snapshot import validate
    _seal(manifest)
    if (manifest.get('version')!=VERSION or manifest.get('kind')!='feature_inputs_only'
            or manifest.get('temporal_quality')!='retrospective_conditional'
            or manifest.get('unique_fixtures')!=manifest.get('included')
            or manifest.get('automatic_model_training') is not False
            or manifest.get('active_model_changed') is not False
            or manifest.get('prospective_confirmation') is not False
            or any(type(manifest.get(key)) is not int for key in ('requested','included','excluded','unique_fixtures'))
            or manifest.get('requested')!=len(manifest['requests'])
            or manifest.get('included')!=len(manifest['entries'])
            or manifest.get('excluded')!=len(manifest['exclusions'])
            or manifest['included']+manifest['excluded']!=manifest['requested']):
        raise ValueError('Les compteurs de cohorte sont incohérents.')
    wanted={row['match_id']:row for row in manifest['requests']}
    if len(wanted)!=len(manifest['requests']):
        raise ValueError('La cohorte contient plusieurs demandes pour la même rencontre.')
    seen=set();snapshot_ids=set()
    for row in [*manifest['entries'],*manifest['exclusions']]:
        mid=row['match_id']
        if mid in seen or mid not in wanted or row['decision_at']!=wanted[mid]['decision_at']:
            raise ValueError('Les décisions de cohorte ne correspondent pas aux demandes.')
        seen.add(mid)
    for row in manifest['entries']:
        snapshot_id=_identity(row['snapshot_id']);snapshot_ids.add(snapshot_id)
        snapshot=validate(snapshots[snapshot_id])
        if ('target_observation' not in snapshot or snapshot['id']!=snapshot_id
                or snapshot['fixture']['match_id']!=row['match_id']
                or snapshot['decision_at']!=row['decision_at']):
            raise ValueError('Un instantané ne correspond pas à sa décision.')
    if snapshot_ids!=set(snapshots) or seen!=set(wanted):
        raise ValueError('Des instantanés ou des demandes sont manquants ou surnuméraires.')


def validate_outcomes(outcomes,manifest,snapshots):
    from halftime_domain import outcome
    _seal(outcomes)
    if (outcomes.get('version')!=VERSION or outcomes.get('cohort_id')!=manifest['id']
            or outcomes.get('kind')!='labelled_feature_examples'
            or outcomes.get('temporal_quality')!='retrospective_conditional'
            or outcomes.get('automatic_model_training') is not False
            or outcomes.get('active_model_changed') is not False
            or outcomes.get('prospective_confirmation') is not False):
        raise ValueError('Le jeu étiqueté ne correspond pas à la cohorte figée.')
    expected={row['snapshot_id'] for row in manifest['entries']}
    seen=set()
    for row in [*outcomes['examples'],*outcomes['exclusions']]:
        sid=row['snapshot_id']
        if sid not in expected or sid in seen:
            raise ValueError('Une rencontre est absente ou comptée deux fois dans les résultats.')
        seen.add(sid)
        if row in outcomes['exclusions'] and row['match_id']!=snapshots[sid]['fixture']['match_id']:
            raise ValueError('Une exclusion ne correspond pas à sa rencontre.')
    if seen!=expected or outcomes['evaluated']!=len(outcomes['examples']) or outcomes['not_evaluable']!=len(outcomes['exclusions']):
        raise ValueError('Les dénominateurs du jeu étiqueté sont incohérents.')
    for row in outcomes['examples']:
        snapshot=snapshots[row['snapshot_id']]
        result=row['outcome_row'];reference=row['outcome_source']
        if (row['features']!=snapshot['features'] or row['match']!=snapshot['fixture']
                or row['decision_at']!=snapshot['decision_at']
                or row['label']!=outcome(row['ht'],row['ft'])
                or result.get('provider')!='api-football'
                or result['value']!=row['label'] or result['ht']!=row['ht'] or result['ft']!=row['ft']
                or any(str(result[key])!=str(snapshot['fixture'][key])
                       for key in ('match_id','league','home_id','away_id','timestamp'))
                or reference['match_id']!=snapshot['fixture']['match_id']
                or reference['time_kind']!='local_import_recorded'
                or reference['normalized_sha256']!=digest(result)
                or not utc_instant(snapshot['decision_at'])<=utc_instant(reference['observed_at_utc'])<utc_instant(outcomes['outcome_at'])
                or utc_instant(reference['observed_at_utc'])<utc_instant(snapshot['fixture']['timestamp']+'+00:00')):
            raise ValueError('Les variables ou les labels du jeu étiqueté ont été modifiés.')
        _identity(reference['observation_id'])


def write_bundle(directory,manifest,snapshots,outcomes=None):
    directory=Path(directory)
    validate_cohort(manifest,snapshots)
    if outcomes is not None:
        validate_outcomes(outcomes,manifest,snapshots)
    directory.mkdir(parents=True,exist_ok=False)
    (directory/'snapshots').mkdir()
    files={}
    _write(directory/'cohort.json',manifest);files['cohort.json']=digest(manifest)
    for sid,snapshot in sorted(snapshots.items()):
        name='snapshots/'+_identity(sid)+'.json'
        _write(directory/name,snapshot);files[name]=digest(snapshot)
    if outcomes is not None:
        _write(directory/'outcomes.json',outcomes);files['outcomes.json']=digest(outcomes)
    package=dict(version='causal-feature-bundle-v1',state='complete',cohort_id=manifest['id'],files=files)
    package['id']=digest(package)
    _write(directory/'COMPLETE.json',package)  # Written last: incomplete bundles are never loaded.
    return package


def load_bundle(directory):
    directory=Path(directory)
    package=json.loads((directory/'COMPLETE.json').read_text(encoding='utf-8'))
    _seal(package)
    if package.get('version')!='causal-feature-bundle-v1' or package.get('state')!='complete':
        raise ValueError('Le paquet d’instantanés est incomplet ou incompatible.')
    files={}
    for name,expected in package['files'].items():
        path=directory/name
        if (name not in ('cohort.json','outcomes.json') and re.fullmatch(r'snapshots/[a-f0-9]{64}\.json',name) is None
                or path.is_symlink() or not path.resolve().is_relative_to(directory.resolve())):
            raise ValueError('Chemin de paquet invalide.')
        value=json.loads(path.read_text(encoding='utf-8'))
        if digest(value)!=expected:
            raise ValueError('Un fichier du paquet est altéré.')
        files[name]=value
    manifest=files['cohort.json']
    snapshots={Path(name).stem:value for name,value in files.items() if name.startswith('snapshots/')}
    outcomes=files.get('outcomes.json')
    validate_cohort(manifest,snapshots)
    if outcomes is not None:
        validate_outcomes(outcomes,manifest,snapshots)
    if package['cohort_id']!=manifest['id']:
        raise ValueError('Le paquet ne correspond pas au manifeste.')
    return dict(package=package,manifest=manifest,snapshots=snapshots,outcomes=outcomes)
