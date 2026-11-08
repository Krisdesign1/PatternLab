"""Read-only operating reception, with historical and current evidence separate.

No overall readiness score or automatic acceptance is inferred. The installed
candidate is checked as bytes/metadata, without CatBoost loading or inference.
Each source has its own snapshot; this is not a cross-database transaction.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

import acceptance_evidence as evidence
import active_candidate_integrity as candidate
import causal_confirmation as confirmation
import scheduler_deadlines as deadlines
import scheduler_operations as operations


VERSION = 'operational-acceptance-v1'
ZONE = ZoneInfo('Africa/Kinshasa')


def _digest(value):
    return sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _criterion(identity,dimension,state,observation,action):
    return dict(id=identity,dimension=dimension,state=state,observation=observation,next_action=action)


def build(archived,windows,op,artifact,selected, *, now,interval_minutes):
    """Pure reception from supplied observations; no results are evaluated here."""
    rows=[]
    for receipt in archived['rows']:
        verified=receipt['state']=='verified'
        text=(f"{receipt['tests_passed']} tests réussis dans le contrôle archivé du {receipt['recorded_at'][:10]}."
              if verified else 'Preuve archivée absente ou incohérente ; aucun résultat de test retenu.')
        rows.append(_criterion(receipt['id'],'archived','verified' if verified else 'unavailable',text,
            'Conserver ce reçu et ses empreintes ; les tests ne sont pas relancés par cette lecture.' if verified else
            'Retrouver le reçu et ses fichiers archivés, puis relancer le contrôle.'))
    pid=selected.get('id') if selected else None
    summary=next((p for p in windows.get('protocols',[]) if p['id']==pid),None)
    days=[d for d in windows.get('days',[]) if d['protocol_id']==pid]
    linked=bool(summary and summary['candidate_id']==selected['candidate_id']) if selected else False
    compatible=bool(linked and summary.get('implementation_compatible') is True)
    candidate_ok=compatible and artifact.get('status')=='valid'
    rows.append(_criterion('candidate','current','verified' if candidate_ok else 'attention',
        'Code, versions et fichiers du candidat correspondent au protocole retenu.' if candidate_ok
        else 'Le protocole, sa compatibilité ou les fichiers du candidat ne sont pas confirmés.',
        'Garder ces versions figées ; ce contrôle ne charge pas le modèle et ne teste pas une prédiction.' if candidate_ok else
        'Examiner la compatibilité et les fichiers avant toute nouvelle capture.'))
    registration=bool(linked and days and all(d['registration_ready'] is True for d in days))
    rows.append(_criterion('registration','current','verified' if registration else 'attention',
        'Accusé local d’inscription conservé avant la première décision.' if registration else
        'Inscription avant décision non attestée pour la période retenue.',
        'Conserver cet accusé ; ne jamais reconstituer une ancienne heure d’inscription.'))
    due=[d for d in days if now>=datetime.fromisoformat(d['deadline_at'])]
    if not days:
        followup_state='unavailable';followup='Aucune période future inscrite n’est lisible.'
    elif not compatible:
        followup_state='attention';followup='La période retenue n’est pas confirmée compatible ; elle reste consultable sans nouvelle capture annoncée.'
    elif not due and now<datetime.fromisoformat(days[0]['decision_at']):
        followup_state='not_due';followup=f"Première décision le {days[0]['decision_at']} ; aucune fenêtre échue."
    elif any(d['state'] in ('missed','closed_unverified') or d['missing_known_targets'] for d in due):
        followup_state='attention';followup='Une fenêtre échue présente une preuve incomplète ou des captures absentes.'
    else:
        followup_state='observing';followup='Fenêtres observées en cours de suivi ; aucun bilan prédictif calculé ici.'
    rows.append(_criterion('followup','prospective',followup_state,followup,
        'Consulter les captures et les semaines dans Confirmation avant match ; conserver les critères inscrits.'))
    latest=op.get('latest_operations',{})
    local_day=now.astimezone(ZONE).date().isoformat()
    fresh=(op.get('state')=='available' and not op.get('truncated') and all(
        latest.get(phase,{}).get('state')=='recent' and latest[phase].get('day')==local_day
        for phase in ('calendar','results')))
    rows.append(_criterion('freshness','current','observed' if fresh else 'attention',
        'Calendrier et résultats récemment observés pour la journée locale.' if fresh else
        'La fraîcheur du calendrier et des résultats du jour n’est pas entièrement confirmée.',
        f'Consulter Délais, fraîcheur et reprises. Le repère est de {2*interval_minutes} minutes, sans garantie de délai.'))
    cycles=[r for r in op.get('cycles',[]) if r['origin']=='service']
    service=max(cycles,key=lambda r:datetime.fromisoformat(r['started_at'])) if cycles else None
    pulse=bool(service and service['status']=='running' and service['signal_state']=='recent')
    rows.append(_criterion('process','current','observed' if pulse else 'unknown',
        'Signal récent du processus de service observé ; sa fin reste à confirmer.' if pulse else
        'Aucun processus de service avec signal récent n’est confirmé par ce journal.',
        'Un signal ne prouve pas la progression des données. Vérifier aussi le planificateur dans Automatisation.'))
    for identity,observation,action in (
        ('continuous','Le fonctionnement continu sur plusieurs journées n’est pas établi par ce relevé.',
         'Comparer des observations datées sur plusieurs journées, avec les absences et reprises conservées.'),
        ('off_machine','Une restauration locale archivée ne prouve pas une sauvegarde hors machine.',
         'Prévoir une copie hors machine et conserver son empreinte séparément ; aucun transfert n’est lancé ici.'),
        ('full_recovery','Le lancement du service après restauration complète, avec environnement et accès privés, reste à qualifier.',
         'Préparer cet exercice dans un environnement séparé, sans remplacer les données actives.'),
        ('prediction','Aucun avantage prédictif durable n’est démontré dans cette vue.',
         'Laisser avancer le suivi inscrit et comparer les méthodes sur les mêmes matchs, selon les règles figées.'),
    ):
        rows.append(_criterion(identity,'remaining','not_demonstrated',observation,action))
    return dict(criteria=rows,selected_protocol_id=pid,
                protocol_summary=summary,selected_days=days,latest_service_cycle=service,
                overall_decision='reception_open',automatic_acceptance=False,
                predictive_improvement_demonstrated=False,continuous_availability_proven=False)


def collect(project_root=None, *, now=None):
    project=Path(project_root or Path(__file__).resolve().parent).resolve()
    root=project/'data/api_football'
    current=now or datetime.now(timezone.utc)
    if not isinstance(current,datetime) or current.tzinfo is None:
        raise ValueError('Le contrôle doit préciser un instant avec fuseau.')
    current=current.astimezone(timezone.utc)
    archived=evidence.load(project)
    failures=[]
    try:
        import automation_worker
        interval=automation_worker.load_config(root)['interval_minutes']
    except (ValueError,OSError,KeyError,TypeError):
        interval=15;failures.append('configuration_unavailable_default_monitoring_interval')
    try:
        windows=deadlines.load(root=root,now=current)
    except (ValueError,OSError,sqlite3.Error):
        windows=dict(protocols=[],days=[],source_sha256=None);failures.append('capture_windows_unavailable')
    op=operations.load(root,now=current,days=7,interval_minutes=interval)
    try:
        protocols=[p for p in confirmation.protocols(root) if 'followup' in p
                   and datetime.fromisoformat(p['registered_at'])<=current]
        selected=max(protocols,key=lambda p:(datetime.fromisoformat(p['registered_at']),p['id'])) if protocols else None
    except (ValueError,OSError,sqlite3.Error,KeyError,TypeError):
        selected=None;failures.append('protocol_metadata_unavailable')
    artifact=candidate.inspect(root,selected) if selected else dict(status='missing',issues=['protocol_missing'],source_sha256=None)
    reception=build(archived,windows,op,artifact,selected,now=current,interval_minutes=interval)
    if failures:
        reception['criteria'].insert(0,_criterion('read_failures','current','unavailable',
            'Certaines sources locales ne sont pas lisibles ; leur absence ne confirme aucun succès.',
            'Examiner les journaux locaux et relancer le relevé.'))
    report=dict(version=VERSION,as_of=current.isoformat(),interval_minutes=interval,
                evidence=archived,candidate=artifact,operations=op,windows=windows,
                source_failures=failures,**reception,
                atomic_across_sources=False,api_requested=False,fit_requested=False,tests_rerun=False,
                backup_reverified=False,model_loaded=False,
                limitations=[
                    'Les reçus archivés sont vérifiés contre leur code archivé ; ils ne qualifient pas tous les fichiers installés aujourd’hui.',
                    'Les empreintes locales ne sont pas des signatures et les tests ne sont pas relancés.',
                    'La présence du processus, la fraîcheur des données et la qualité prédictive sont trois observations distinctes.',
                    'Les sources sont lues séparément ; aucun instantané global inter-bases ni contenu déjà chargé dans le processus n’est attesté.',
                    'La sauvegarde complète n’est pas relue ; l’intégrité du candidat concerne ses fichiers, sans chargement ni inférence.',
                ])
    report['implementation_sha256']={name:sha256((Path(__file__).parent/name).read_bytes()).hexdigest() for name in
        ('operational_acceptance.py','acceptance_evidence.py','active_candidate_integrity.py','scheduler_deadlines.py','scheduler_operations.py')}
    report['report_sha256']=_digest(report)
    return report
