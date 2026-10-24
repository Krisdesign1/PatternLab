from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import automation_health as health
import automation_health_panel as panel


NOW = datetime(2026, 10, 24, 10, tzinfo=timezone.utc)


class AutomationHealthPanelTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.view = dict(rows=[], total=0, first_started_at=None, truncated=False)
        self.patch(panel.cloud_storage, 'configured', return_value=False)
        self.patch(panel.api, 'STORE_PATH', self.root/'fixtures.json')
        self.read = self.patch(health, 'load', side_effect=lambda *args, **kwargs: deepcopy(self.view))
        self.network = self.patch(panel.api.ApiFootballClient, 'get', side_effect=AssertionError('No API'))
        stub = patch('automation_worker.run_cycle', side_effect=AssertionError('Read only'))
        self.runner = stub.start(); self.addCleanup(stub.stop)

    def patch(self, target, name, *args, **kwargs):
        stub = patch.object(target, name, *args, **kwargs)
        mock = stub.start(); self.addCleanup(stub.stop)
        return mock

    def open(self):
        return AppTest.from_string("from automation_health_panel import render\n"
            "render({'enabled':True}, scheduler={'running':True})", default_timeout=15).run()

    def test_empty_journal_distinguishes_loaded_scheduler_from_real_observation(self):
        at = self.open()
        self.assertFalse(at.exception)
        self.assertTrue(any('Aucun passage observé' in node.value for node in at.info))
        self.assertTrue(any('aucun passage du service' in node.value for node in at.warning))
        self.assertFalse(at.metric)
        self.assertEqual(list(self.root.iterdir()), [])
        self.runner.assert_not_called(); self.network.assert_not_called()

    def test_pauses_errors_pending_and_manual_origins_remain_distinct(self):
        rows = []
        for index, status in enumerate(('completed', 'paused', 'error', 'running')):
            rows.append(dict(id=str(index), started_at=(NOW-timedelta(minutes=15*index)).isoformat(),
                             finished_at=None if status == 'running' else NOW.isoformat(),
                             duration_seconds=None if status == 'running' else 2,
                             status=status, origin='manual', interval_minutes=15,
                             reasons=['provider_quota'] if status == 'paused' else [], counters={'requests': 2}))
        self.view = dict(rows=rows, total=4, first_started_at=rows[-1]['started_at'], truncated=False)
        at = self.open()
        self.assertFalse(at.exception)
        self.assertEqual(next(node.value for node in at.metric if node.label == 'Terminés'), '1')
        self.assertTrue(any('sans fin confirmée' in node.value for node in at.warning))
        self.assertTrue(any('ne confirment pas encore' in node.value for node in at.info))
        table = next(node.value for node in at.dataframe if 'Origine' in node.value.columns)
        self.assertEqual(set(table['État']), {'Terminé', 'En pause', 'Erreur', 'Fin non confirmée'})
        self.assertEqual(set(table['Origine']), {'Bouton manuel'})
        at.segmented_control(key='automation_health_days').set_value(1).run()
        self.assertFalse(at.exception)
        self.assertEqual(self.read.call_args.kwargs['days'], 1)
        self.runner.assert_not_called(); self.network.assert_not_called()

    def test_invalid_journal_does_not_break_automation_screen(self):
        self.read.side_effect = health.HealthError('Journal invalide')
        at = self.open()
        self.assertFalse(at.exception)
        self.assertTrue(any('Journal invalide' in node.value for node in at.warning))
        self.assertFalse(at.dataframe)

    def test_recent_signal_distinguishes_a_live_cycle_from_an_unknown_end(self):
        row = dict(id='live', started_at=NOW.isoformat(), finished_at=None, duration_seconds=None,
                   status='running', origin='service', interval_minutes=15, reasons=[], counters={'requests':12},
                   last_seen_at=NOW.isoformat(), last_progress_at=NOW.isoformat(),
                   phase='history', signal_state='recent', elapsed_seconds=120)
        self.view = dict(rows=[row],total=1,first_started_at=NOW.isoformat(),truncated=False)
        at = self.open()
        self.assertFalse(at.exception)
        self.assertTrue(any('en cours avec un signal récent' in node.value for node in at.info))
        self.assertFalse(any('sans fin confirmée' in node.value for node in at.warning))
        table = next(node.value for node in at.dataframe if 'Origine' in node.value.columns)
        self.assertEqual(table.iloc[0]['État'], 'En cours · signal récent')
        self.assertEqual(table.iloc[0]['Phase'], 'Historique')
        self.assertEqual(table.iloc[0]['Durée (s)'], 120)
        self.runner.assert_not_called(); self.network.assert_not_called()


if __name__ == '__main__':
    unittest.main()
