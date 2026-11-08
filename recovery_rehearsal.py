"""Explicit recovery rehearsal against isolated copies, never the live workspace.

This is a standalone maintenance operation, not a page-load function. It uses
installed readers, never executes code from a recovery point, and never starts
a service, imports scores, reconciles results or fits a model.
"""
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
from unittest.mock import patch

import local_recovery as recovery


VERSION = 'local-recovery-rehearsal-v1'
STORES = {
    'fixtures':'data/api_football/fixtures.sqlite3',
    'forecast_journal':'data/api_football/predictions.sqlite3',
    'causal_confirmation':'data/api_football/causal_confirmation.sqlite3',
    'probability_archive':'data/api_football/high_probability_archive.sqlite3',
    'scenario_intents':'data/api_football/probability_scenarios.sqlite3',
    'manual_bets':'data/api_football/manual_bets.sqlite3',
}
REQUIRED = ('fixtures','forecast_journal','causal_confirmation')


class RehearsalError(ValueError):
    pass


def _domain(component, root):
    path = root/STORES[component]
    if component=='fixtures':
        import fixture_store as store
        counts = store.statistics(path)
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as con:
            ids = [row[0] for row in con.execute('SELECT match_id FROM fixtures ORDER BY ordinal LIMIT 3')]
        sample = store.raw_fixtures(path,ids=ids) if ids else []
        if len(sample)!=len(ids):
            raise RehearsalError('Projection restaurée incohérente.')
        return dict(items=counts['stored_count'],usable_results=counts['usable_count'],sample_items=len(sample))
    if component=='forecast_journal':
        import prediction_journal
        rows = prediction_journal.list_entries(path)
        return dict(items=len(rows),result_revisions=sum(len(r['result_history']) for r in rows))
    if component=='causal_confirmation':
        import causal_confirmation_store as store
        data = path.parent
        protocols, heads = store.protocols(data), store.heads(data)
        return dict(items=len(heads),protocols=len(protocols),
                    receipts=sum(r['ack'] is not None for r in heads))
    if component=='probability_archive':
        import high_probability_archive
        return dict(items=len(high_probability_archive.list_entries(path.parent)))
    if component=='scenario_intents':
        import probability_scenario_store
        return dict(items=len(probability_scenario_store.list_intents(path)))
    if component=='manual_bets':
        import manual_bet_store
        return dict(items=len(manual_bet_store.list_bets(path)))
    raise RehearsalError('Lecteur de restauration inconnu.')


def rehearse(folder):
    """Verify all listed files/DBs, then exercise six explicit app readers.

    Required stores absent from the inventory leave an incomplete rehearsal.
    Domain failures remain failures; they cannot become empty successful stores.
    Separate databases have separate backup instants. This is not a service,
    authentication, full feature reconstruction or model inference test.
    """
    folder = Path(folder).resolve()
    manifest_path = folder/'manifest.json'
    manifest_sha = recovery.fingerprint(manifest_path)
    verification = recovery.verify_recovery_point(folder)
    if verification.get('known_private_paths_excluded') is not True:
        raise RehearsalError('Le point contient des chemins privés connus ; restauration applicative refusée.')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    items = {item['path']:item for item in recovery._manifest_items(manifest)}
    if recovery.fingerprint(manifest_path)!=manifest_sha:
        raise RehearsalError('Le manifeste a changé pendant le contrôle.')
    checks = []
    import api_football as api
    with tempfile.TemporaryDirectory(prefix='application-restore-',dir=folder) as temporary, ExitStack() as guards:
        isolated = Path(temporary)
        # Guards apply only inside this standalone tool process, never the app.
        guards.enter_context(patch.object(api,'load_key',side_effect=RehearsalError('Accès aux identifiants interdit.')))
        guards.enter_context(patch.object(api.ApiFootballClient,'get',side_effect=RehearsalError('Appel API interdit.')))
        for component, relative in STORES.items():
            item = items.get(relative)
            if item is None:
                checks.append(dict(component=component,state='missing',required=component in REQUIRED))
                continue
            source, target = folder/'files'/relative, isolated/relative
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(source,target)
            if recovery.fingerprint(target)!=item['sha256']:
                raise RehearsalError('La copie à examiner ne correspond pas au point vérifié.')
        for component, relative in STORES.items():
            if relative not in items:
                continue
            try:
                result = _domain(component,isolated)
                checks.append(dict(component=component,state='passed',required=component in REQUIRED,**result))
            except (ValueError,OSError,sqlite3.Error,KeyError,TypeError,RuntimeError):
                checks.append(dict(component=component,state='failed',required=component in REQUIRED,
                                   reason='restored_application_read_failed'))
    if recovery.fingerprint(manifest_path)!=manifest_sha:
        raise RehearsalError('Le manifeste a changé pendant la restauration isolée.')
    failed = any(r['state']=='failed' for r in checks)
    missing = any(r['required'] and r['state']=='missing' for r in checks)
    return dict(version=VERSION,checked_at=datetime.now(timezone.utc).isoformat(),
                state='failed' if failed else 'incomplete' if missing else 'passed',
                recovery_manifest_sha256=manifest_sha,verification=verification,domain_checks=checks,
                installed_reader_sha256={name:recovery.fingerprint(Path(__file__).parent/name) for name in
                    ('recovery_rehearsal.py','local_recovery.py','fixture_store.py','prediction_journal.py',
                     'causal_confirmation_store.py','high_probability_archive.py','probability_scenario_store.py','manual_bet_store.py')},
                production_restored=False,api_calls_allowed=False,credential_access_allowed=False,
                model_fit_requested=False,atomic_across_databases=False,off_machine_backup=False,
                limitations=[
                    'Contrôle des fichiers listés et de six lecteurs installés, sans exécuter le code sauvegardé.',
                    'Les données d’authentification privées sont exclues par chemin connu et restent à restaurer séparément.',
                    'Le manifeste n’est pas signé ; son empreinte doit être conservée séparément pour établir son origine.',
                    'Ce contrôle ne reconstruit pas toutes les variables, ne lance pas les modèles et ne prouve pas un service H24.',
                    'Les bases sont cohérentes séparément ; les références entre bases ne sont pas validées globalement.',
                ])
