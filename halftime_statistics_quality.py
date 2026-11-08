"""Pure quality gate for future statistics, preserving the frozen v1 parser.

The provider's xG are separate FT/H1/H2 measurements rounded to two decimals.
If their underlying values are additive, each rounding error is at most 0.005,
so the observed FT - H1 - H2 residual is at most 0.015 in absolute value. A
two-cent residual cannot be explained by that rounding. We conservatively
include the boundary because the provider's tie-breaking rule is unspecified.

An inconsistent triplet does not identify which period is wrong. Quarantine
only the xG pairs for all three periods, with no subtraction, replacement, or
mutation of the supplied raw response. Other valid metrics remain available.
Frozen research continues to import halftime_statistics_parser directly.
"""

from decimal import Decimal, localcontext

from halftime_statistics_parser import parse_statistics as _parse_statistics


PARSER_VERSION = "api-football-halves-quality-v2"
XG_ROUNDING_TOLERANCE = Decimal("0.015")
_XG = "expected_goals"


def xg_consistency_issues(period_pairs):
    """Report nonadditive xG using normalized pairs, without changing them.

    ``period_pairs`` maps full/first/second to [home, away]. A missing period,
    a None pair, or a None side is absent, never zero. Each side is checked
    only if its triplet is present. Other values must be finite, nonnegative
    int/float measurements already validated by the parser or statistics store.
    """
    pairs = {period: period_pairs.get(period)
             for period in ("full", "first", "second")}
    for pair in pairs.values():
        if pair is None:
            continue
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError("xG periods require a home/away pair or None.")
        for value in pair:
            if value is not None and (type(value) not in (int, float)
                                      or not Decimal(str(value)).is_finite()
                                      or value < 0):
                raise ValueError("xG values must be finite nonnegative numbers or None.")
    issues = []
    # Decimal(str()) avoids binary floating-point noise at the decimal bound.
    with localcontext() as context:
        context.prec = 1024
        for side in (0, 1):
            if any(pair is None or pair[side] is None for pair in pairs.values()):
                continue
            values = {period: pairs[period][side] for period in pairs}
            residual = (Decimal(str(values["full"]))
                        - Decimal(str(values["first"]))
                        - Decimal(str(values["second"])))
            if abs(residual) > XG_ROUNDING_TOLERANCE:
                issues.append(dict(metric=_XG, side=side, issue="nonadditive_xg",
                                   **values,
                                   rounding_tolerance=float(XG_ROUNDING_TOLERANCE)))
    return issues


def parse_statistics(payload, expected_team_ids):
    """Return the v1 output contract with an additional xG consistency gate.

    Only explicit, valid home/away pairs in all three periods permit the
    inter-period check. Missing or invalid observations remain missing; valid
    zeros are measurements. A failure on either side removes the atomic xG
    pairs for both teams and reports the offending side and observed values.
    """
    result = _parse_statistics(payload, expected_team_ids)
    result["parser_version"] = PARSER_VERSION
    periods = result["periods"]
    issues = xg_consistency_issues({period: report["pairs"].get(_XG)
                                    for period, report in periods.items()})
    if not issues:
        return result

    result["consistency_issues"].extend(issues)
    for period in periods.values():
        period["pairs"].pop(_XG, None)
        period["valid_pair_metrics"] = sorted(period["pairs"])
        period["metric_issues"][_XG] = ["inconsistent_xg", "inconsistent_xg"]
    common = set(periods["first"]["pairs"]) & set(periods["second"]["pairs"])
    result["complete_half_metrics"] = sorted(common)
    result["halves_available"] = result["status"] == "ok" and bool(common)
    result["halves_core_shots_available"] = (
        result["status"] == "ok" and {"Shots on Goal", "Total Shots"} <= common)
    return result
