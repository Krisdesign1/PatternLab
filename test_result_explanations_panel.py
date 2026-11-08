"""Shared explanation tables are lazy, local-only and do not settle results."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest
import api_football as api
import prediction_journal as journal
from test_result_explanations import entry, raw


def overview():
    from datetime import datetime, timezone
    import prediction_journal as journal
    from result_explanations_panel import render_pending_overview
    render_pending_overview(journal.list_entries, key='causes', now=datetime(2026,10,27,18,tzinfo=timezone.utc))


class ExplanationPanelTests(unittest.TestCase):
    def setUp(self):
        self.entries = [entry(), entry(id='second')]
        self.original = deepcopy(self.entries)
        self.read = self.patch(journal, 'list_entries', return_value=self.entries)
        self.local = self.patch(api, 'load_fixtures', return_value=[raw('PST')])
        self.network = self.patch(api.ApiFootballClient, 'get', side_effect=AssertionError('No API'))
        self.reconcile = self.patch(journal, 'reconcile', side_effect=AssertionError('No reconciliation'))

    def patch(self, obj, name, **kwargs):
        mocker = patch.object(obj, name, **kwargs)
        result = mocker.start(); self.addCleanup(mocker.stop)
        return result

    def test_closed_overview_does_not_load_journal_or_fixtures(self):
        at = AppTest.from_function(overview).run()
        self.assertFalse(at.exception)
        self.read.assert_not_called(); self.local.assert_not_called()

    def test_open_overview_identifies_match_and_groups_without_changing_results(self):
        at = AppTest.from_function(overview)
        at.session_state['causes'] = True
        at.run()
        self.assertFalse(at.exception)
        table = at.dataframe[0].value
        self.assertEqual(table['État du match'].tolist(), ['Reporté'])
        self.assertEqual(table['Match'].tolist(), ['A — B'])
        self.assertEqual(table['Groupes concernés'].tolist(), [2])
        self.assertEqual(self.entries, self.original)
        self.local.assert_called_once()
        self.network.assert_not_called(); self.reconcile.assert_not_called()

    def test_unavailable_journal_is_explained_without_fabricating_a_rate(self):
        self.read.side_effect = journal.JournalError('Unavailable')
        at = AppTest.from_function(overview)
        at.session_state['causes'] = True
        at.run()
        self.assertFalse(at.exception)
        self.assertIn('indisponible', at.warning[0].value)
        self.assertEqual(len(at.dataframe), 0)


if __name__ == '__main__':
    unittest.main()
