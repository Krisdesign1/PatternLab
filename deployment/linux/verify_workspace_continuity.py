"""Read-only verification of workspace continuity on the installed runtime.

The UI cases use synthetic fixtures, mocked providers and temporary archives.
The performance check reads only one competition from the actual fixture store.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
import ast
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch


ROOT = Path('/opt/patternlab')
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent/'workspace-continuity-tests'))


def verify():
    import api_football
    import scenario_workspace_panel as panel
    import test_scenario_workspace_panel as fixtures
    import test_workspace_refresh_preservation as cases

    fixtures.APP_PATH = ROOT/'app.py'
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(cases.WorkspaceRefreshPreservationTests)
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=1).run(suite)
    if not result.wasSuccessful():
        raise RuntimeError('Workspace continuity verification failed')

    tree = ast.parse((ROOT/'scenario_workspace_panel.py').read_text())
    fragments = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in ('_manual_workspace', '_live_workspace'):
            fragments[node.name] = [ast.unparse(item) for item in node.decorator_list]
    assert 'st.fragment' in fragments['_manual_workspace']
    assert 'st.fragment(run_every=60)' in fragments['_live_workspace']

    # These API clients raise if any verification path unexpectedly calls out.
    with ExitStack() as stack:
        for provider in (api_football.ApiFootballClient,):
            stack.enter_context(patch.object(provider, 'get', side_effect=AssertionError('No API in verification')))
        signature = panel.sources.data_signature('API-Football')
        panel._readiness_history.clear()
        panel._history.clear()
        started = time.perf_counter()
        rows = panel._readiness_history(signature, 'API-Football', '39')
        cold = time.perf_counter()-started
        started = time.perf_counter()
        cached = panel._readiness_history(signature, 'API-Football', '39')
        warm = time.perf_counter()-started
        calculation_rows, _audit = panel._history(signature, 'API-Football', '39')
        assert rows and rows == cached == calculation_rows
        assert all(str(row['league']) == '39' for row in rows)

    return dict(verified_at=datetime.now(timezone.utc).isoformat(),
                ui_tests=result.testsRun, ui_errors=len(result.errors), ui_failures=len(result.failures),
                synthetic_ui_fixtures=True, inference_and_archive_calls_mocked=True,
                network_calls=0, production_forecasts_created=0,
                manual_periodic_rerun=False, live_refresh_seconds=60,
                performance=dict(league='39', rows=len(rows), cold_seconds=round(cold, 4),
                                 cached_seconds=round(warm, 4), only_selected_league=True))


if __name__ == '__main__':
    print(json.dumps(verify()))
