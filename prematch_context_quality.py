"""Pure structural checks for optional pre-match provider observations.

No I/O is performed. ``observed_at`` must be supplied from the receipt of the
specific endpoint response, never inferred from the fixture's metadata or the
time at which this audit runs. Caller-supplied times are not independently
authenticated. Structural completeness does not certify provider coverage.
"""
from datetime import datetime, timezone


def _identifier(value):
    if (isinstance(value, bool) or not isinstance(value, (str, int))
            or not str(value).isascii() or not str(value).isdecimal()
            or int(value) <= 0):
        return None
    return str(int(value))


def _instant(value):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
        if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
            return None
        return stamp.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _timing(raw, observed_at, decision_at):
    fixture = raw.get('fixture') if isinstance(raw, dict) else None
    kickoff = _instant(fixture.get('date')) if isinstance(fixture, dict) else None
    observed, decision = _instant(observed_at), _instant(decision_at)
    issues = []
    for name, value, supplied in (('observed_at', observed, observed_at),
                                  ('decision_at', decision, decision_at),
                                  ('kickoff', kickoff, fixture.get('date') if isinstance(fixture, dict) else None)):
        if value is None:
            issues.append(name + ('_unknown' if supplied is None else '_invalid_or_naive'))
    if observed is not None and decision is not None and observed >= decision:
        issues.append('observation_not_before_decision')
    if observed is not None and kickoff is not None and observed >= kickoff:
        issues.append('observation_not_before_kickoff')
    if decision is not None and kickoff is not None and decision >= kickoff:
        issues.append('decision_not_before_kickoff')
    return dict(observed_at=observed.isoformat() if observed else None,
                decision_at=decision.isoformat() if decision else None,
                kickoff=kickoff.isoformat() if kickoff else None,
                usable=not issues, issues=issues,
                provenance='caller_supplied_observation_time',
                independently_authenticated=False)


def _teams(raw):
    teams = raw.get('teams') if isinstance(raw, dict) else None
    ids = []
    if isinstance(teams, dict):
        for side in ('home', 'away'):
            team = teams.get(side)
            ids.append(_identifier(team.get('id')) if isinstance(team, dict) else None)
    valid = len(ids) == 2 and None not in ids and len(set(ids)) == 2
    return ids if valid else [], [] if valid else ['fixture_team_identity_invalid']


def _fixture_issues(raw):
    fixture = raw.get('fixture') if isinstance(raw, dict) else None
    issues = []
    if isinstance(raw, dict) and raw.get('provider') not in (None, 'api-football', 'API-Football'):
        issues.append('foreign_provider')
    if not isinstance(fixture, dict) or _identifier(fixture.get('id')) is None:
        issues.append('fixture_identity_invalid')
    if isinstance(raw, dict) and 'league' in raw:
        league = raw['league']
        if not isinstance(league, dict) or _identifier(league.get('id')) is None:
            issues.append('league_identity_invalid')
    annotation = raw.get('_patternlab_import') if isinstance(raw, dict) else None
    if isinstance(annotation, dict) and annotation.get('quarantined') is True:
        issues.append('fixture_quarantined')
    elif isinstance(annotation, dict) and 'quarantined' in annotation and type(annotation['quarantined']) is not bool:
        issues.append('fixture_quality_annotation_malformed')
    elif annotation is not None and not isinstance(annotation, dict):
        issues.append('fixture_quality_annotation_malformed')
    return issues


def _prematch_state(raw):
    """A future date cannot make played events or scores pre-match again."""
    fixture = raw.get('fixture') if isinstance(raw, dict) else None
    status = fixture.get('status') if isinstance(fixture, dict) else None
    short = status.get('short') if isinstance(status, dict) else None
    short = short.strip().upper() if isinstance(short, str) else None
    issues = []
    if short != 'NS':
        issues.append('target_status_not_NS' if short else 'target_status_unknown')
    if isinstance(raw, dict):
        events = raw.get('events')
        if isinstance(events, list):
            if events:
                issues.append('target_events_already_present')
        elif events is not None:
            issues.append('target_events_malformed')
        goals = raw.get('goals')
        if isinstance(goals, dict):
            if any(goals.get(side) is not None for side in ('home', 'away')):
                issues.append('target_goals_already_present')
        elif goals is not None:
            issues.append('target_goals_malformed')
        scores = raw.get('score')
        if isinstance(scores, dict):
            for period in ('halftime', 'fulltime', 'extratime', 'penalty'):
                pair = scores.get(period)
                if isinstance(pair, dict):
                    if any(pair.get(side) is not None for side in ('home', 'away')):
                        issues.append('target_scores_already_present')
                elif pair is not None:
                    issues.append('target_scores_malformed')
        elif scores is not None:
            issues.append('target_scores_malformed')
    return dict(status=short, issues=sorted(set(issues)), usable=not issues)


def _availability(raw, key, *, allow_response=False):
    if isinstance(raw, list) and allow_response:
        value = raw
    elif isinstance(raw, dict):
        if key in raw:
            value = raw[key]
        elif allow_response and 'response' in raw:
            value = raw['response']
        else:
            return 'absent', None
    else:
        return 'malformed', None
    if value is None:
        return 'null', None
    if not isinstance(value, list):
        return 'malformed', None
    return ('present' if value else 'empty'), value


def _players(value, prefix):
    if not isinstance(value, list):
        return [], [prefix + '_not_array']
    ids, issues = [], []
    for entry in value:
        player = entry.get('player') if isinstance(entry, dict) else None
        identity = _identifier(player.get('id')) if isinstance(player, dict) else None
        if identity is None:
            issues.append(prefix + '_player_identity_invalid')
        else:
            ids.append(identity)
    if len(set(ids)) != len(ids):
        issues.append(prefix + '_duplicate_player')
    return ids, issues


def audit_lineups(raw_fixture, *, observed_at=None, decision_at=None):
    """Check the two announced starting elevens, bench and coach separately.

    A usable starting-XI observation requires exact fixture-team identities,
    eleven distinct identified starters per side, and valid explicit receipt
    timing. Bench or coach defects remain visible without invalidating a valid
    starting XI. No missing coach is inferred from today's club metadata.
    """
    availability, lineups = _availability(raw_fixture, 'lineups')
    timing = _timing(raw_fixture, observed_at, decision_at)
    state = _prematch_state(raw_fixture)
    fixture_ids, identity_issues = _teams(raw_fixture)
    identity_issues.extend(_fixture_issues(raw_fixture))
    start_issues, bench_issues, coach_issues, unknown = [], [], [], []
    team_ids, all_starters, coaches, team_reports = [], [], [], []
    if availability != 'present':
        unknown.append('lineups_not_structurally_available')
        if availability == 'malformed':
            identity_issues.append('lineups_not_array')
    else:
        if len(lineups) != 2:
            identity_issues.append('lineup_team_count_not_two')
        for index, lineup in enumerate(lineups):
            if not isinstance(lineup, dict):
                identity_issues.append('lineup_entry_not_object')
                continue
            team = lineup.get('team')
            tid = _identifier(team.get('id')) if isinstance(team, dict) else None
            team_ids.append(tid)
            if tid is None or tid not in fixture_ids:
                identity_issues.append('lineup_team_not_fixture_team')
            starters, xi_issues = _players(lineup.get('startXI'), 'starting_xi')
            if not isinstance(lineup.get('startXI'), list) or len(lineup['startXI']) != 11:
                xi_issues.append('starting_xi_count_not_eleven')
            all_starters.extend(starters)
            bench = lineup.get('substitutes')
            if bench is None:
                bench_ids, bi = [], []
                unknown.append('bench_unknown')
            else:
                bench_ids, bi = _players(bench, 'bench')
            if set(starters) & set(bench_ids):
                bi.append('bench_overlaps_starting_xi')
            coach = lineup.get('coach')
            cid = _identifier(coach.get('id')) if isinstance(coach, dict) else None
            if cid is None:
                unknown.append('coach_unknown')
                if coach is not None and not isinstance(coach, dict):
                    coach_issues.append('coach_not_object')
            else:
                coaches.append(cid)
            start_issues.extend(xi_issues)
            bench_issues.extend(bi)
            team_reports.append(dict(index=index, team_id=tid, starting_player_ids=starters,
                                     bench_player_ids=bench_ids, coach_id=cid,
                                     starting_xi_issues=sorted(set(xi_issues)),
                                     bench_issues=sorted(set(bi))))
        if len(team_ids) != len(set(team_ids)):
            identity_issues.append('duplicate_lineup_team')
        if set(team_ids) != set(fixture_ids):
            identity_issues.append('lineup_teams_do_not_match_fixture')
        if len(all_starters) != len(set(all_starters)):
            start_issues.append('starting_player_shared_between_teams')
        if len(coaches) != len(set(coaches)):
            coach_issues.append('coach_shared_between_teams')
    complete = availability == 'present' and not identity_issues and not start_issues
    issues = sorted(set(identity_issues + start_issues + bench_issues + coach_issues
                        + timing['issues'] + state['issues']))
    return dict(availability=availability, issues=issues,
                complete_starting_elevens=bool(complete),
                coach_ids=sorted(set(coaches), key=int), coach_count=len(set(coaches)),
                coaches_usable_before_decision=bool(complete and timing['usable'] and state['usable']
                    and len(coaches) == 2 and len(set(coaches)) == 2 and not coach_issues),
                usable_before_decision=bool(complete and timing['usable'] and state['usable']),
                timing=timing, prematch_state=state, prematch_issues=state['issues'],
                identity_issues=sorted(set(identity_issues)), starting_xi_issues=sorted(set(start_issues)),
                bench_issues=sorted(set(bench_issues)), coach_issues=sorted(set(coach_issues)),
                teams=team_reports, unknown=sorted(set(unknown)),
                provider_completeness_proven=False)


def audit_injuries(raw_fixture, *, observed_at=None, decision_at=None):
    """Check stored injury rows without interpreting empty as no absences.

    Accept a fixture's ``injuries`` field, a provider ``response`` envelope, or
    the response list itself. Standalone responses lack fixture/timing context
    and consequently cannot be certified usable before a decision by this API.
    Even valid present rows are an observed list, never a completeness claim.
    """
    availability, rows = _availability(raw_fixture, 'injuries', allow_response=True)
    timing = _timing(raw_fixture, observed_at, decision_at)
    state = _prematch_state(raw_fixture)
    fixture_ids, identity_issues = _teams(raw_fixture)
    fixture = raw_fixture.get('fixture') if isinstance(raw_fixture, dict) else None
    fid = _identifier(fixture.get('id')) if isinstance(fixture, dict) else None
    issues, players, reported_types = identity_issues + _fixture_issues(raw_fixture), [], []
    unknown = ['absence_coverage_unknown']
    if fid is None:
        issues.append('fixture_identity_unknown')
    if availability in ('absent', 'null', 'empty'):
        unknown.append('missing_or_empty_does_not_attest_no_injuries')
    elif availability == 'malformed':
        issues.append('injuries_not_array')
    else:
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                issues.append('injury_entry_not_object')
                continue
            team, player, source_fixture = row.get('team'), row.get('player'), row.get('fixture')
            tid = _identifier(team.get('id')) if isinstance(team, dict) else None
            pid = _identifier(player.get('id')) if isinstance(player, dict) else None
            source_id = _identifier(source_fixture.get('id')) if isinstance(source_fixture, dict) else None
            if tid is None or tid not in fixture_ids:
                issues.append('injury_team_not_fixture_team')
            if pid is None:
                issues.append('injury_player_identity_invalid')
            injury_type = player.get('type') if isinstance(player, dict) else None
            injury_reason = player.get('reason') if isinstance(player, dict) else None
            if not isinstance(injury_type, str) or not injury_type.strip():
                unknown.append('injury_type_unknown')
            else:
                reported_types.append(injury_type.strip())
            if not isinstance(injury_reason, str) or not injury_reason.strip():
                unknown.append('injury_reason_unknown')
            if source_id is None:
                unknown.append('injury_fixture_binding_unknown')
            elif source_id != fid:
                issues.append('injury_fixture_mismatch')
            key = tid, pid
            if key in seen:
                issues.append('duplicate_injury_player')
            seen.add(key)
            if pid is not None:
                players.append(pid)
    structural = availability == 'present' and not issues and 'injury_fixture_binding_unknown' not in unknown
    described = 'injury_type_unknown' not in unknown and 'injury_reason_unknown' not in unknown
    return dict(availability=availability, issues=sorted(set(issues + timing['issues'] + state['issues'])),
                observed_injury_rows=len(rows) if isinstance(rows, list) else None,
                player_ids=sorted(set(players), key=int),
                reported_types=sorted(set(reported_types)),
                presence_usable_before_decision=bool(structural and timing['usable'] and state['usable']),
                usable_before_decision=bool(structural and described and timing['usable'] and state['usable']),
                timing=timing, prematch_state=state, prematch_issues=state['issues'],
                unknown=sorted(set(unknown)), no_injuries_proven=False,
                provider_completeness_proven=False)
