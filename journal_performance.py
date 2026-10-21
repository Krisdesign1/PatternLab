"""Read-only prospective journal cohorts, progress, and comparable trends."""
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import prediction_journal as journal


ORIGINS = ('all', 'manual', 'automatic')
STATUSES = ('settled', 'pending', 'void', 'unresolved')
LOCAL_ZONE = ZoneInfo('Africa/Kinshasa')


def entries_on_day(entries, day):
    """Keep whole frozen groups whose every kickoff belongs to this local day.

    Creation time is deliberately irrelevant. No partial group is presented as
    if its ordered scenario described fewer matches, and no saved data changes.
    """
    if not isinstance(day, date) or isinstance(day, datetime):
        raise ValueError('La journée doit être une date locale.')
    selected = []
    for entry in entries:
        fixtures = entry.get('fixtures') or []
        try:
            if fixtures and all(journal._stamp(row['timestamp']).astimezone(LOCAL_ZONE).date() == day
                                for row in fixtures):
                selected.append(entry)
        except (journal.JournalError, KeyError, TypeError):
            continue
    return selected


def origin(entry):
    metadata = entry.get('metadata', {})
    return 'automatic' if metadata.get('automation_policy') or metadata.get('selected_engine') == 'automation' else 'manual'


def filter_entries(entries, league, size, selected_origin='all'):
    if selected_origin not in ORIGINS:
        raise ValueError('Origine de prévision inconnue.')
    return [entry for entry in entries if str(entry['league']) == str(league) and entry['size'] == size
            and (selected_origin == 'all' or origin(entry) == selected_origin)]


def observed_count(entry):
    ids = {str(row['match_id']) for row in entry['fixtures']}
    return len({str(row['match_id']) for row in entry.get('observed', []) if str(row['match_id']) in ids})


def counts(entries):
    status = Counter(entry['status'] for entry in entries)
    return dict(total=len(entries), **{name: status[name] for name in STATUSES},
                observed_matches=sum(observed_count(entry) for entry in entries),
                expected_matches=sum(entry['size'] for entry in entries))


def signature(entry, methods):
    return tuple((name, entry['methods'][name]['version'], entry['methods'][name].get('generation', ''))
                 for name in sorted(methods))


def signatures(entries, methods):
    if not methods:
        return []
    return sorted({signature(entry, methods) for entry in entries if set(methods).issubset(entry['methods'])})


def _day(timestamp):
    parsed = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).date().isoformat()


def _trend(entries, methods):
    """Cumulative scores on the same settled entries, by prediction UTC day."""
    by_day = defaultdict(list)
    for entry in entries:
        by_day[_day(entry['created_at'])].append(entry)
    if len(by_day) < 2:
        return []
    output, seen, hits, losses = [], 0, Counter(), Counter()
    for day, current in sorted(by_day.items()):
        seen += len(current)
        for name in sorted(methods):
            hits[name] += sum(entry['evaluations'][name]['top3'] for entry in current)
            losses[name] += sum(entry['evaluations'][name]['loss'] for entry in current)
            output.append(dict(day=day, method=name, tested=seen, top3=hits[name],
                               top3_rate=hits[name]/seen, loss=losses[name]/seen))
    return output


def comparison(entries, methods, selected_signature=None):
    """Keep one league, size and exact version set; every method shares IDs.

    Pending, invalid, and void groups remain visible in cohort counts but never
    enter success rates. Missing methods are counted as exclusions, never as
    unsuccessful predictions. Multiple version sets require an explicit choice.
    """
    entries, methods = list(entries), tuple(methods)
    if len(methods) != len(set(methods)):
        raise ValueError('Une méthode ne peut être comparée deux fois.')
    if len({(entry['league'], entry['size']) for entry in entries}) > 1:
        raise ValueError('La comparaison doit conserver une seule compétition et une seule taille.')
    if len({entry['id'] for entry in entries}) != len(entries):
        raise ValueError('Un groupe ne peut être compté deux fois.')
    complete = [entry for entry in entries if methods and set(methods).issubset(entry['methods'])]
    versions = signatures(complete, methods)
    if selected_signature is None:
        if len(versions) > 1:
            raise ValueError('Choisissez les versions à comparer sur les mêmes groupes.')
        selected_signature = versions[0] if versions else None
    elif selected_signature not in versions:
        raise ValueError('Ces versions ne sont pas présentes dans le journal filtré.')
    cohort = [entry for entry in complete if signature(entry, methods) == selected_signature]
    settled = [entry for entry in cohort if entry['status'] == 'settled'
               and set(methods).issubset(entry.get('evaluations', {}))]
    summary = journal.summarize(settled, required_methods=methods) if settled else []
    ids = {entry['id'] for entry in settled}
    if any(set(row['group_ids']) != ids or row['tested'] != len(settled) for row in summary):
        raise ValueError('Les méthodes doivent être évaluées sur les mêmes groupes.')
    return dict(entries=cohort, settled=settled, counts=counts(cohort), summary=summary,
                signature=selected_signature, signatures=versions,
                missing_methods=len(entries)-len(complete), other_versions=len(complete)-len(cohort),
                excluded_incomplete_evaluations=sum(entry['status'] == 'settled' for entry in cohort)-len(settled),
                trend=_trend(settled, methods) if methods else [])
