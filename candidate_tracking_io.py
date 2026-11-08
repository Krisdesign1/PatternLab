"""Versioned, scoped I/O for the unchanged candidate-shadow protocol.

The original module owns studies, anchors, inference, forecasts and settlement
rules. This explicit adapter narrows only its local reads. Its source hash must
be included in the collector execution receipt; existing study hashes are never
rewritten to conceal an execution change.
"""
from datetime import date
from zoneinfo import ZoneInfo

import candidate_tracking as original


VERSION = 'candidate-shadow-scoped-io-v1'


def run_cycle(root=None, *, clock=None, refresh=None, check_time=None):
    """Run original study decisions with only their leagues and calendar day."""
    original._write_allowed()
    root = original._root(root)
    registered = original.studies(root)
    report = dict(status='idle', studies=len(registered), created=0, existing=0,
                  unavailable=0, issues=[])
    if not registered:
        return report
    current = original._clock(clock)
    day = current.astimezone(ZoneInfo(original.TIMEZONE)).date().isoformat()
    leagues = tuple(sorted({study['league'] for study in registered}, key=int))
    rows, _ = original.api.load_matches(root/'fixtures.json', leagues=leagues)
    start, end = original.workspace._bounds(date.fromisoformat(day), original.TIMEZONE)
    raw = original.api.load_fixtures(root/'fixtures.json', start=start, end=end,
                                     leagues=leagues)
    existing = {entry['id'] for entry in original.journal.list_entries(original.database_path(root))}
    report['status'] = 'completed'
    for study in registered:
        # Saved anchors remain authoritative, even if later calendar updates
        # move a fixture outside today's read window.
        for group in original._anchors(study, raw, day, root):
            if check_time:
                check_time()
            fixtures = group['fixtures']
            key = original.review._digest([study['id'], original.journal._group_key(fixtures)])
            if key in existing:
                report['existing'] += 1
                continue
            try:
                original.journal._check_time(fixtures, original._clock(clock), True)
                if min(original.journal._stamp(f['timestamp']) for f in fixtures) <= original.journal._stamp(study['registered_at']):
                    continue
                methods = original._methods(rows, fixtures, study, root)
                if refresh is None:
                    raise ValueError('Une confirmation API récente est nécessaire avant enregistrement.')
                fresh = refresh(ids='-'.join(f['match_id'] for f in fixtures), timezone='UTC')
                if {str(f['fixture']['id']) for f in fresh} != {f['match_id'] for f in fixtures}:
                    raise ValueError('Le relevé API du suivi est incomplet.')
                original.api.save_fixtures(fresh, root/'fixtures.json')
                created = original._record(study, fixtures, methods, fresh, root, clock)
                report['created'] += int(created)
                existing.add(key)
            except (ValueError, OSError, KeyError, TypeError) as error:
                from automation_worker import _Pause
                if isinstance(error, _Pause):
                    raise
                report['unavailable'] += 1
                message = str(error) if isinstance(error, ValueError) else 'Suivi de candidat indisponible.'
                if message not in report['issues']:
                    report['issues'].append(message[:300])
    return report


def reconcile(root=None, *, now=None):
    """The same journal settlement, loading only its saved fixture identities."""
    return original.journal.reconcile(path=original.database_path(root),
        fixtures_path=original._root(root)/'fixtures.json', now=now)
