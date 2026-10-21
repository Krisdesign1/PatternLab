"""One bounded local model job after an API cycle; frozen leagues never refit."""
from datetime import timedelta
import fcntl

import api_football as api
import automation_worker as automation
import model_campaign as campaign


def run_cycle(*, root=None, config=None, now=None, dry_run=False):
    root = automation._root(root)
    config = automation._config(config if config is not None else automation.load_config(root))
    clock = (lambda: automation._time(now)) if now is not None else automation._time
    report = dict(status='idle', started_at=clock().isoformat(), prepared=0, issues=[], jobs=[])
    if not dry_run and (not config['enabled'] or not config['models_enabled'] or config['model_jobs_per_cycle'] == 0):
        return dict(report, status='disabled', finished_at=clock().isoformat())
    if not dry_run:
        root.mkdir(parents=True, exist_ok=True)
    lock = None
    locked = False
    try:
        if not dry_run:
            lock = (root/'model_campaign_worker.lock').open('a')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except BlockingIOError:
                return dict(report, status='busy', finished_at=clock().isoformat())
        rows, audit = api.load_matches(root/'fixtures.json')
        raw = api.load_fixtures(root/'fixtures.json')
        jobs = campaign.plan(rows, raw=raw, sizes=config['sizes'], root=root, now=clock())
        report.update(competitions=len(jobs), ready=sum(j['status']=='ready' for j in jobs),
                      frozen=sum(j['status']=='frozen' for j in jobs), excluded_scores=len(audit['issues']))
        if dry_run:
            return dict(report, status='dry_run', jobs=jobs, finished_at=clock().isoformat())
        state = automation._read(root/'model_campaign_worker_state.json', {})
        attempts = state.setdefault('attempts', {})
        selected = next((job for job in jobs if job['status']=='ready' and
                         attempts.get(job['league'], 0) <= clock().timestamp()-86400), None)
        if selected:
            report.update(status='building', league=selected['league'])
            report['jobs'] = [selected]
            automation._write(root/'model_campaign_status.json', report)
            attempts[selected['league']] = clock().timestamp()
            automation._write(root/'model_campaign_worker_state.json', state)
            result = campaign.run_one(rows, selected['league'], sizes=config['sizes'], root=root, now=now)
            report.update(status='completed', prepared=int(result['status']=='frozen'))
            report['jobs'] = [dict(league=result['league'], status=result['status'],
                                   components=result.get('components', {}),
                                   comparisons=result.get('comparisons', {}).get('status'))]
    except (campaign.CampaignError, api.ApiFootballError, ValueError, OSError, KeyError, TypeError):
        report.update(status='error')
        report['issues'].append('Préparation interrompue ; le premier instantané est conservé. '
                                'Consultez le manifeste de la compétition. Nouvelle tentative au prochain jour.')
    finally:
        if lock is not None:
            if locked:
                report['finished_at'] = clock().isoformat()
                automation._write(root/'model_campaign_status.json', report)
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
    report['finished_at'] = clock().isoformat()
    return report
