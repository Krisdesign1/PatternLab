"""The competition browser caches small summaries instead of whole histories."""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from streamlit.testing.v1 import AppTest

import competition_browser as browser
import competition_overview as overview
import scenario_workspace_panel as panel
from test_competition_browser import DAY, NOW, FixedTime, snapshot
from test_competition_readiness import history


class CompetitionOverviewPerformanceTests(unittest.TestCase):
    def setUp(self):
        panel._competition_overview.clear()
        self.addCleanup(panel._competition_overview.clear)
        self.state = snapshot()
        self.state.pop('matches')  # Legacy fixtures-only display remains supported.
        self.rows = history(80) + history(40, league='61') + history(100, league='999')
        self.rows = [dict(row, day=row['timestamp'][:10]) for row in self.rows]
        self.rows.append(dict(self.rows[0], match_id='9000', day=DAY.isoformat(),
                              timestamp=DAY.isoformat()+'T00:00:00', ht=None, ft=None, value='invalid'))
        self.reader = Mock(side_effect=lambda path, **filters: (
            [row for row in self.rows if row['league'] in filters['leagues']], {}))
        self.provider = SimpleNamespace(STORE_PATH=Path('/synthetic/fixtures.json'), load_matches=self.reader)
        self.patch(panel.sources, '_provider', return_value=self.provider)
        self.patch(panel, '_snapshot', return_value=self.state)
        self.patch(panel.sources, 'load_matches', side_effect=AssertionError('No unscoped history read'))
        self.patch(browser, 'datetime', FixedTime)

    def patch(self, target, name, *args, **kwargs):
        context = patch.object(target, name, *args, **kwargs)
        value = context.start()
        self.addCleanup(context.stop)
        return value

    def load(self, *, size=4, revision=1, minute='2025-08-01T10:00', now=NOW):
        return panel._competition_overview(DAY, ('synthetic', revision), 'API-Football', size, minute, now)

    def test_scoped_query_matches_full_history_result_and_keeps_invalid_leagues_visible(self):
        # A bad historical score must retain the original "À vérifier" result.
        self.rows.append(dict(self.rows[0], match_id='8999', value='invalid'))
        before = min(DAY.isoformat(), NOW.date().isoformat())
        expected = overview.build(panel._day_snapshot(self.state, DAY),
                                  [row for row in self.rows if row['day'] < before],
                                  DAY, size=4, now=NOW)
        original = deepcopy(self.rows)
        actual = self.load()
        self.assertEqual(actual, expected)
        self.assertEqual({row['league'] for row in actual}, {'39', '61', '777'})
        self.assertEqual(next(row for row in actual if row['league']=='39')['preparation'], 'À vérifier')
        self.assertEqual(set(self.reader.call_args.kwargs['leagues']), {'39', '61', '777'})
        self.assertEqual(self.rows, original)

    def test_cache_reuses_small_summary_and_invalidates_revision_size_and_clock(self):
        first = self.load()
        first[0]['history_matches'] = -1
        self.assertNotEqual(self.load()[0]['history_matches'], -1)
        self.assertEqual(self.reader.call_count, 1)
        self.load(revision=2)
        self.load(size=3)
        self.load(minute='2025-08-01T10:01', now=NOW+timedelta(minutes=1))
        self.assertEqual(self.reader.call_count, 4)

    def test_empty_calendar_does_not_read_any_history(self):
        self.state.update(competitions={}, fixtures=[])
        self.assertEqual(self.load(), [])
        self.reader.assert_not_called()

    def test_filter_and_pagination_reruns_do_not_read_or_hash_full_history(self):
        script = '''from datetime import date
import competition_browser as browser
import scenario_workspace_panel as panel
snapshot = panel._snapshot(date(2025,8,1), ('synthetic',1), 'API-Football')
def load(size,current):
    return panel._competition_overview(date(2025,8,1), ('synthetic',1), 'API-Football',
        size, current.strftime('%Y-%m-%dT%H:%M'), current)
browser.render(snapshot, (), date(2025,8,1), overview_loader=load)
'''
        with patch.object(browser, '_overview', side_effect=AssertionError('Full-history cache hashing')):
            at = AppTest.from_string(script).run()
            self.assertFalse(at.exception)
            self.assertEqual(self.reader.call_count, 1)
            at.selectbox(key='competition_browser_sort').set_value('name').run()
            at.text_input(key='competition_browser_query').set_value('Ligue').run()
            at.selectbox(key='competition_browser_page_size').set_value(20).run()
            self.assertFalse(at.exception)
            self.assertEqual(self.reader.call_count, 1)


if __name__ == '__main__':
    unittest.main()
