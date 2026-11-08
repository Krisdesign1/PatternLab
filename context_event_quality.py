"""Pure, conservative quality audit of an API-Football fixture's event list.

This module has no I/O, provider, application, database or training imports.
``event_process_eligible`` means internally usable under this limited contract;
it never certifies collection completeness or historical availability. A caller
must separately attest coverage, receipt time and participant/lineup membership.
Only normal FT fixtures are admitted. AET and PEN are deliberately excluded.
"""
from collections import Counter
import json
import re


VERSION = 'context-event-quality-v1'
_SCORING = frozenset(('Normal Goal', 'Penalty', 'Own Goal'))
_CARDS = frozenset(('Yellow Card', 'Red Card', 'Second Yellow card', 'Second Yellow Card'))
_VAR_CONFIRMED = frozenset(('Goal confirmed', 'Penalty confirmed', 'Red card confirmed',
                            'Card upgrade confirmed'))


def _identifier(value):
    if (isinstance(value, bool) or not isinstance(value, (int, str))
            or not str(value).isascii() or not str(value).isdigit() or int(value) <= 0):
        return None
    return str(int(value))


def _pair(value):
    if not isinstance(value, dict):
        return None
    pair = [value.get('home'), value.get('away')]
    return pair if all(type(v) is int and v >= 0 for v in pair) else None


def _entity(event, key):
    value = event.get(key)
    return value if isinstance(value, dict) else {}


def audit_events(raw_fixture):
    """Return a finite JSON report, without modifying or repairing its input.

    Availability distinguishes absent/null/empty/present/malformed. Counted
    goals use the supplied event team as the credited team, including Own Goal;
    no player-to-team inference or arbitrary own-goal inversion is performed.
    Missed Penalty is a non-scoring event. Unknown goal details, ambiguous VAR,
    duplicates and invalid chronology are reported rather than corrected.

    Time keys are (half, elapsed, extra): 45+X precedes 46, and 90+X remains H2.
    Bare elapsed>90 is ambiguous and never reinterpreted as added regulation
    time. Simultaneous events keep provider order; minute resolution does not
    establish their actual within-minute order. Player identity is separate
    from fixture-team identity; no lineup membership is inferred here.
    """
    report = dict(version=VERSION, availability='malformed', event_count=None,
                  type_counts={}, issue_codes=[], annotations=[], fixture_id=None,
                  team_ids=dict(home=None, away=None), status=None,
                  score_halftime=None, score_fulltime=None,
                  counted_goal_totals_h1=None, counted_goal_totals_90=None,
                  goal_compatible=False, event_process_eligible=False,
                  collection_completeness_proven=False,
                  historical_availability_proven=False,
                  chronology_order=[], own_goal_count=0, var_count=0,
                  missing_player_id_count=0, duplicate_count=0)
    issues = set()
    goal_safe = True
    process_safe = True

    def issue(code, *, goal=False, process=True):
        nonlocal goal_safe, process_safe
        issues.add(code)
        if goal:
            goal_safe = False
        if process:
            process_safe = False

    if not isinstance(raw_fixture, dict):
        report['issue_codes'] = ['FIXTURE_NOT_OBJECT']
        return report
    if raw_fixture.get('provider') not in (None, 'api-football', 'API-Football'):
        issue('PROVIDER_NOT_API_FOOTBALL', goal=True)
    if 'league' in raw_fixture:
        league = raw_fixture['league']
        if not isinstance(league, dict) or _identifier(league.get('id')) is None:
            issue('LEAGUE_ID_INVALID', goal=True)
    fixture = raw_fixture.get('fixture')
    fixture = fixture if isinstance(fixture, dict) else {}
    report['fixture_id'] = _identifier(fixture.get('id'))
    if report['fixture_id'] is None:
        issue('FIXTURE_ID_INVALID', goal=True)
    status = fixture.get('status')
    short = status.get('short') if isinstance(status, dict) else None
    report['status'] = short.strip().upper() if isinstance(short, str) else None
    if report['status'] != 'FT':
        issue('STATUS_NOT_NORMAL_FT', goal=True)
    quality = raw_fixture.get('_patternlab_import')
    if isinstance(quality, dict) and quality.get('quarantined') is True:
        issue('IMPORT_QUARANTINED', goal=True)
    elif isinstance(quality, dict) and 'quarantined' in quality and type(quality['quarantined']) is not bool:
        issue('IMPORT_ANNOTATION_MALFORMED', goal=True)
    elif quality is not None and not isinstance(quality, dict):
        issue('IMPORT_ANNOTATION_MALFORMED', goal=True)

    teams = raw_fixture.get('teams')
    teams = teams if isinstance(teams, dict) else {}
    for side in ('home', 'away'):
        team = teams.get(side)
        report['team_ids'][side] = _identifier(team.get('id')) if isinstance(team, dict) else None
    ids = [report['team_ids'][side] for side in ('home', 'away')]
    valid_teams = None not in ids and ids[0] != ids[1]
    if not valid_teams:
        issue('FIXTURE_TEAM_IDS_INVALID', goal=True)
    score = raw_fixture.get('score')
    score = score if isinstance(score, dict) else {}
    ht, ft = _pair(score.get('halftime')), _pair(score.get('fulltime'))
    report['score_halftime'], report['score_fulltime'] = ht, ft
    if ht is None or ft is None:
        issue('REGULATION_SCORE_INVALID', goal=True)
    elif any(h > f for h, f in zip(ht, ft)):
        issue('HALFTIME_EXCEEDS_FULLTIME', goal=True)
    if report['status'] == 'FT' and 'goals' in raw_fixture:
        goals = _pair(raw_fixture['goals'])
        if goals is None:
            issue('FINAL_GOALS_FIELD_INVALID', goal=True)
        elif ft is not None and goals != ft:
            issue('FINAL_GOALS_FIELD_MISMATCH', goal=True)
    for name in ('extratime', 'penalty'):
        value = score.get(name)
        # Non-null AET/shootout scores contradict the normal-FT-only contract.
        if value is not None and (not isinstance(value, dict) or not {'home', 'away'} <= set(value)):
            issue('NON_REGULATION_SCORE_MALFORMED', goal=True)
        elif isinstance(value, dict) and any(value.get(side) is not None for side in ('home', 'away')):
            issue('NON_REGULATION_SCORE_PRESENT', goal=True)

    if 'events' not in raw_fixture:
        report['availability'] = 'absent'
        issue('EVENTS_ABSENT', goal=True)
        rows = None
    elif raw_fixture['events'] is None:
        report['availability'] = 'null'
        issue('EVENTS_NULL', goal=True)
        rows = None
    elif not isinstance(raw_fixture['events'], list):
        issue('EVENTS_NOT_LIST', goal=True)
        rows = None
    else:
        rows = raw_fixture['events']
        report['availability'] = 'present' if rows else 'empty'
        report['event_count'] = len(rows)
        if not rows:
            issue('EMPTY_EVENTS_NOT_COMPLETENESS_PROOF')

    if rows is None:
        report['issue_codes'] = sorted(issues)
        return report
    first_goals, all_goals = [0, 0], [0, 0]
    counts, seen, time_keys = Counter(), set(), []
    previous_key = None
    for index, event in enumerate(rows):
        annotation = dict(index=index, type=None, detail=None, team_id=None,
                          player_id=None, assist_id=None, phase=None, elapsed=None,
                          extra=None, time_key=None, counted_goal=False, issue_codes=[])
        event_issues = set()

        def event_issue(code, *, goal=False, process=True):
            event_issues.add(code)
            issue(code, goal=goal, process=process)

        if not isinstance(event, dict):
            event_issue('EVENT_NOT_OBJECT', goal=True)
            annotation['issue_codes'] = sorted(event_issues)
            report['annotations'].append(annotation)
            continue
        try:
            signature = json.dumps(event, sort_keys=True, separators=(',', ':'), allow_nan=False)
        except (TypeError, ValueError):
            event_issue('EVENT_NOT_JSON', goal=True)
        else:
            if signature in seen:
                report['duplicate_count'] += 1
                event_issue('DUPLICATE_EVENT', goal=True)
            seen.add(signature)
        raw_type, raw_detail = event.get('type'), event.get('detail')
        typ = raw_type if isinstance(raw_type, str) else None
        detail = raw_detail if isinstance(raw_detail, str) else None
        annotation['type'], annotation['detail'] = typ, detail
        counts[typ if isinstance(typ, str) else '__invalid__'] += 1
        is_goal = typ == 'Goal'
        team_id = _identifier(_entity(event, 'team').get('id'))
        player_id = _identifier(_entity(event, 'player').get('id'))
        assist_id = _identifier(_entity(event, 'assist').get('id'))
        annotation.update(team_id=team_id, player_id=player_id, assist_id=assist_id)
        if team_id is None:
            event_issue('EVENT_TEAM_ID_INVALID', goal=is_goal)
        elif valid_teams and team_id not in ids:
            event_issue('EVENT_TEAM_NOT_IN_FIXTURE', goal=is_goal)
        if player_id is None:
            report['missing_player_id_count'] += 1
            event_issue('PLAYER_ID_UNKNOWN')
        if typ == 'subst' and assist_id is None:
            event_issue('SUBSTITUTION_PARTICIPANT_UNKNOWN')
        if typ == 'subst' and player_id is not None and player_id == assist_id:
            event_issue('SUBSTITUTION_IDENTICAL_PARTICIPANTS')

        time = event.get('time')
        time = time if isinstance(time, dict) else {}
        elapsed, extra = time.get('elapsed'), time.get('extra')
        annotation['elapsed'] = elapsed if type(elapsed) is int else None
        annotation['extra'] = extra if type(extra) is int else None
        valid_time = (type(elapsed) is int and 0 <= elapsed <= 90
                      and (extra is None or type(extra) is int and extra >= 0))
        if not valid_time:
            event_issue('EVENT_TIME_INVALID_OR_NONREGULATION', goal=is_goal)
        elif extra and elapsed not in (45, 90):
            valid_time = False
            event_issue('ADDED_TIME_BASE_AMBIGUOUS', goal=is_goal)
        if valid_time:
            phase = 'H1' if elapsed <= 45 else 'H2'
            key = (1 if phase == 'H1' else 2, elapsed, extra or 0)
            annotation.update(phase=phase, time_key=list(key))
            time_keys.append((key, index))
            if previous_key is not None and key < previous_key:
                event_issue('EVENTS_OUT_OF_ORDER')
            elif key == previous_key:
                event_issue('WITHIN_MINUTE_ORDER_UNRESOLVED', process=False)
            previous_key = key
        comments = event.get('comments')
        shootout = isinstance(comments, str) and 'shootout' in re.sub(r'[^a-z]', '', comments.casefold())
        if shootout:
            event_issue('SHOOTOUT_EVENT', goal=is_goal)
        if is_goal:
            if detail == 'Own Goal':
                report['own_goal_count'] += 1
                event_issue('OWN_GOAL_TEAM_AS_CREDITED', process=False)
            if detail == 'Missed Penalty':
                event_issue('MISSED_PENALTY_NOT_SCORED', process=False)
            elif detail not in _SCORING:
                event_issue('UNKNOWN_GOAL_DETAIL', goal=True)
            if (detail in _SCORING and valid_time and valid_teams and team_id in ids and not shootout):
                side = ids.index(team_id)
                all_goals[side] += 1
                if elapsed <= 45:
                    first_goals[side] += 1
                annotation['counted_goal'] = True
        elif typ == 'Card':
            if detail not in _CARDS:
                event_issue('UNKNOWN_CARD_DETAIL')
            event_issue('CARD_RECIPIENT_MEMBERSHIP_UNVERIFIED', process=False)
        elif typ == 'subst':
            if not isinstance(detail, str) or re.fullmatch(r'Substitution [1-9][0-9]*', detail) is None:
                event_issue('UNKNOWN_SUBSTITUTION_DETAIL')
            event_issue('SUBSTITUTION_DIRECTION_UNVERIFIED', process=False)
        elif typ == 'Var':
            report['var_count'] += 1
            if detail not in _VAR_CONFIRMED:
                event_issue('VAR_DECISION_AMBIGUOUS')
            else:
                event_issue('VAR_CONFIRMED_NOT_ADDITIONAL_GOAL', process=False)
        else:
            event_issue('UNKNOWN_EVENT_TYPE', goal=True)
        annotation['issue_codes'] = sorted(event_issues)
        report['annotations'].append(annotation)

    report['type_counts'] = dict(sorted(counts.items()))
    report['chronology_order'] = [index for _, index in sorted(time_keys)]
    if valid_teams:
        report['counted_goal_totals_h1'] = dict(zip(('home', 'away'), first_goals))
        report['counted_goal_totals_90'] = dict(zip(('home', 'away'), all_goals))
    if ht is not None and first_goals != ht:
        issue('HALFTIME_GOAL_TOTAL_MISMATCH', goal=True)
    if ft is not None and all_goals != ft:
        issue('REGULATION_GOAL_TOTAL_MISMATCH', goal=True)
    report['goal_compatible'] = goal_safe
    report['event_process_eligible'] = bool(rows) and goal_safe and process_safe
    report['issue_codes'] = sorted(issues)
    return report
