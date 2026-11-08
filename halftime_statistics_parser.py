"""Strict, network-free parser for the local API-Football statistics collector.

The official ``team`` + ``statistics`` list and the live-observed
``statistics_1h`` / ``statistics_2h`` lists are parsed explicitly. The verified
response is fixtures_statistics-58e756770f016523.json in this audit's v1 folder.
An unknown structure never becomes an inferred first/second half. Values are paired in the
caller's home/away ID order, never in provider response order. This module has no
application imports, I/O, credentials, or training side effects.
"""

import math
import re

PARSER_VERSION = 'api-football-halves-v1'


COUNT_METRICS = frozenset((
    "Shots on Goal", "Shots off Goal", "Shots insidebox", "Shots outsidebox",
    "Total Shots", "Blocked Shots", "Fouls", "Corner Kicks", "Offsides",
    "Yellow Cards", "Red Cards", "Goalkeeper Saves", "Total passes", "Passes accurate",
))
PERCENT_METRICS = frozenset(("Ball Possession", "Passes %"))
CONTINUOUS_METRICS = frozenset(("expected_goals",))
TRACKED_METRICS = tuple(sorted(COUNT_METRICS | PERCENT_METRICS | CONTINUOUS_METRICS))
# Exact keys observed in a successful half=true provider response, not heuristics.
PERIOD_FIELDS = {"statistics": "full", "statistics_1h": "first", "statistics_2h": "second"}
_DECIMAL = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)\Z")


def _team_id(value):
    if type(value) is int and value > 0:
        return str(value)
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return str(int(value))
    return None


def _value(metric, value):
    """Return (normalized value, problem); preserve zero and distinguish null."""
    if value is None:
        return None, "null"
    if metric in PERCENT_METRICS:
        if not isinstance(value, str) or not value.endswith("%"):
            return None, "percentage_unit_missing"
        token = value[:-1].strip()
        if not _DECIMAL.fullmatch(token):
            return None, "invalid_percentage"
        number = float(token)
        return (number, None) if 0 <= number <= 100 else (None, "percentage_out_of_range")
    if isinstance(value, bool):
        return None, "boolean"
    if isinstance(value, str):
        token = value.strip()
        if not _DECIMAL.fullmatch(token):
            return None, "invalid_number"
        number = float(token)
    elif type(value) in (int, float):
        try:
            number = float(value)
        except OverflowError:
            return None, "nonfinite"
    else:
        return None, "invalid_number"
    if not math.isfinite(number):
        return None, "nonfinite"
    if number < 0:
        return None, "negative"
    if metric in COUNT_METRICS:
        if not number.is_integer():
            return None, "noninteger_count"
        return int(number), None
    return number, None


def _error_kind(errors):
    # Classify errors but never copy provider messages (potential credential echo).
    text = str(errors).casefold()
    if any(token in text for token in ("rate", "quota", "limit", "request")):
        return "quota_or_rate"
    if any(token in text for token in ("token", "api-key", "api key", "apikey", "authentication")):
        return "authentication"
    if any(token in text for token in ("subscription", "plan", "access", "permission")):
        return "access"
    return "provider_error"


def _empty_period():
    return {"present": False, "team_ids": [], "pairs": {},
            "valid_pair_metrics": [], "metric_issues": {}, "unknown_metrics": []}


def _parse_statistics(payload, expected_team_ids):
    """Inspect one decoded provider payload; no extrapolation of missing halves.

    ``expected_team_ids`` contains exactly the fixture's home and away IDs.
    Invalid values never enter numeric pairs. Duplicate metrics, duplicate teams,
    mismatched teams, malformed blocks and unknown response shapes are explicit.
    """
    expected = [_team_id(value) for value in expected_team_ids]
    if len(expected) != 2 or None in expected or len(set(expected)) != 2:
        raise ValueError("Two distinct positive expected team IDs are required.")
    result = {
        "status": "unknown_shape", "expected_team_ids": expected, "team_ids": [],
        "provider_error_kind": None, "diagnostics": [],
        "response_shape": None, "unknown_entry_keys": [],
        "periods": {period: _empty_period() for period in ("full", "first", "second")},
        "halves_available": False, "complete_half_metrics": [],
        "halves_core_shots_available": False,
    }
    diagnostics = result["diagnostics"]
    if not isinstance(payload, dict):
        result["status"] = "invalid"
        diagnostics.append("payload_not_object")
        return result
    errors = payload.get("errors")
    if errors:
        result["status"] = "api_error"
        result["provider_error_kind"] = _error_kind(errors)
        return result
    if "response" not in payload:
        diagnostics.append("response_missing")
        return result
    response = payload["response"]
    result["response_shape"] = type(response).__name__
    if not isinstance(response, list):
        diagnostics.append("response_not_list")
        return result
    if not response:
        result["status"] = "empty"
        diagnostics.append("empty_response")
        return result

    teams = {}
    structural_invalid = False
    unknown_shape = False
    unknown_keys = set()
    for entry in response:
        if not isinstance(entry, dict):
            structural_invalid = True
            diagnostics.append("team_entry_not_object")
            continue
        unknown_keys.update(str(key) for key in entry if key not in {"team", *PERIOD_FIELDS})
        team = entry.get("team")
        tid = _team_id(team.get("id")) if isinstance(team, dict) else None
        if tid is None:
            structural_invalid = True
            diagnostics.append("invalid_team_id")
            continue
        if tid in teams:
            structural_invalid = True
            diagnostics.append("duplicate_team_id")
            continue
        teams[tid] = entry
        if not any(field in entry for field in PERIOD_FIELDS):
            unknown_shape = True
            diagnostics.append("no_recognized_statistics_block")
    result["team_ids"] = sorted(teams, key=int)
    result["unknown_entry_keys"] = sorted(unknown_keys)
    if set(teams) != set(expected):
        structural_invalid = True
        diagnostics.append("team_ids_mismatch")
    if unknown_keys:
        diagnostics.append("unrecognized_team_fields")

    for field, period in PERIOD_FIELDS.items():
        report = result["periods"][period]
        per_team = {}
        for tid, entry in teams.items():
            if field not in entry:
                continue
            report["present"] = True
            statistics = entry[field]
            if not isinstance(statistics, list):
                structural_invalid = True
                diagnostics.append(f"{period}_statistics_not_list")
                continue
            report["team_ids"].append(tid)
            parsed = {}
            duplicate = set()
            unknown_metrics = set(report["unknown_metrics"])
            for statistic in statistics:
                if not isinstance(statistic, dict) or not isinstance(statistic.get("type"), str):
                    structural_invalid = True
                    diagnostics.append(f"{period}_invalid_statistic_entry")
                    continue
                metric = statistic["type"]
                if metric not in TRACKED_METRICS:
                    unknown_metrics.add(metric)
                    continue
                if metric in parsed:
                    duplicate.add(metric)
                    continue
                if "value" not in statistic:
                    parsed[metric] = (None, "value_missing")
                else:
                    parsed[metric] = _value(metric, statistic["value"])
            for metric in duplicate:
                parsed[metric] = (None, "duplicate_metric")
            report["unknown_metrics"] = sorted(unknown_metrics)
            per_team[tid] = parsed
        report["team_ids"].sort(key=int)
        for metric in TRACKED_METRICS:
            values, problems = [], []
            for tid in expected:
                value, problem = per_team.get(tid, {}).get(metric, (None, "metric_absent"))
                values.append(value)
                problems.append(problem)
            if report["present"]:
                report["metric_issues"][metric] = problems
            # Structural mismatches must never be counted as complete coverage.
            if report["present"] and not structural_invalid and not any(problems):
                report["pairs"][metric] = values
        report["valid_pair_metrics"] = sorted(report["pairs"])
    if structural_invalid:
        result["status"] = "invalid"
        for report in result["periods"].values():
            report["pairs"] = {}
            report["valid_pair_metrics"] = []
    elif unknown_shape:
        result["status"] = "unknown_shape"
    else:
        result["status"] = "ok"
    # Only corresponding metric pairs observed explicitly in both halves count.
    common_halves = (set(result["periods"]["first"]["pairs"])
                     & set(result["periods"]["second"]["pairs"]))
    result["halves_available"] = result["status"] == "ok" and bool(common_halves)
    result["complete_half_metrics"] = sorted(common_halves) if result["status"] == "ok" else []
    result["halves_core_shots_available"] = (
        result["status"] == "ok" and {"Shots on Goal", "Total Shots"} <= common_halves)
    result["diagnostics"] = sorted(set(diagnostics))
    return result


def parse_statistics(payload, expected_team_ids):
    """Parse observed periods and quarantine inconsistent shot measurements.

    The original provider response remains stored separately. No missing half
    is reconstructed, and rounded xG are not subjected to integer identities.
    """
    result = _parse_statistics(payload, expected_team_ids)
    result['parser_version'] = PARSER_VERSION
    result['consistency_issues'] = []
    bad = set()
    periods = result['periods']
    for metric in ('Total Shots', 'Shots on Goal', 'Shots insidebox', 'Shots outsidebox'):
        if all(metric in p['pairs'] for p in periods.values()):
            for side in (0, 1):
                if (periods['full']['pairs'][metric][side]
                        != periods['first']['pairs'][metric][side] + periods['second']['pairs'][metric][side]):
                    result['consistency_issues'].append(dict(metric=metric, side=side, issue='nonadditive_shots'))
                    bad.add(metric)
    for period, values in periods.items():
        pairs = values['pairs']
        if all(k in pairs for k in ('Shots on Goal', 'Total Shots')):
            if any(pairs['Shots on Goal'][i] > pairs['Total Shots'][i] for i in (0, 1)):
                result['consistency_issues'].append(dict(period=period, issue='on_goal_gt_total'))
                bad.update(('Shots on Goal', 'Total Shots'))
        if all(k in pairs for k in ('Shots insidebox', 'Shots outsidebox', 'Total Shots')):
            if any(pairs['Shots insidebox'][i] + pairs['Shots outsidebox'][i] != pairs['Total Shots'][i] for i in (0, 1)):
                result['consistency_issues'].append(dict(period=period, issue='inside_plus_outside_ne_total'))
                bad.update(('Shots insidebox', 'Shots outsidebox', 'Total Shots'))
    for values in periods.values():
        for metric in bad:
            values['pairs'].pop(metric, None)
            if values['present']:
                values['metric_issues'][metric] = ['inconsistent_shots', 'inconsistent_shots']
        values['valid_pair_metrics'] = sorted(values['pairs'])
    common = set(periods['first']['pairs']) & set(periods['second']['pairs'])
    result['complete_half_metrics'] = sorted(common)
    result['halves_available'] = result['status'] == 'ok' and bool(common)
    result['halves_core_shots_available'] = result['status'] == 'ok' and {'Shots on Goal', 'Total Shots'} <= common
    return result
