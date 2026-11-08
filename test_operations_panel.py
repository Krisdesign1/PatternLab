"""Read-only server views and Linux training boundary, with no real services."""
import copy
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import automation_panel as automation
import operations_panel as panel


SCHEDULER = dict(available=True, supported=True, backend='systemd', enabled=True,
                 running=True, process_running=False, interval_seconds=900,
                 observed_at='2026-11-05T08:00:00+00:00',
                 timer={'next_elapse_at': '2026-11-05T08:15:00+00:00'},
                 worker={'started_at': '2026-11-05T07:00:00+00:00', 'result': 'exit-code'})
BACKUP = dict(available=True, state='verified', observed_at='2026-11-05T08:00:00+00:00',
              observation_stale=False, backup_overdue=False,
              last_backup=dict(verified_at='2026-11-05T06:55:00+00:00', files_verified=5397,
                               databases_restored=14, payload_bytes=3229719534),
              scheduler=SCHEDULER, capacity={'free_bytes': 5297291264},
              last_attempt={'state': 'verified', 'reason_code': 'none'})


class OperationsPanelTests(unittest.TestCase):
    def test_successful_backup_distinguishes_restore_test_export_and_off_machine_copy(self):
        with patch.object(panel.operations_status, 'backup_status', return_value=BACKUP):
            at = AppTest.from_string('from operations_panel import render_backup_status\nrender_backup_status()', default_timeout=15).run()
        self.assertFalse(at.exception)
        self.assertEqual(next(node.value for node in at.metric if node.label == 'Bases restaurées pour contrôle'), '14')
        self.assertTrue(any('restauration des bases' in node.value for node in at.success))
        rows = at.dataframe[0].value
        self.assertIn('Non configurée', rows.loc[rows['Contrôle'] == 'Copie hors serveur', 'État'].iloc[0])
        self.assertFalse(at.button)
        self.assertFalse(at.download_button)

    def test_failed_attempt_keeps_last_verified_point_but_does_not_claim_new_success(self):
        value = copy.deepcopy(BACKUP)
        value.update(state='deferred', observation_stale=True, backup_overdue=True)
        value['last_attempt'] = dict(state='deferred', reason_code='insufficient_space')
        value['scheduler'] = dict(available=False)
        with patch.object(panel.operations_status, 'backup_status', return_value=value):
            at = AppTest.from_string('from operations_panel import render_backup_status\nrender_backup_status()', default_timeout=15).run()
        self.assertFalse(at.exception)
        self.assertTrue(any('Espace libre insuffisant' in node.value for node in at.warning))
        self.assertTrue(any('pas confirmée active' in node.value for node in at.warning))
        self.assertTrue(any('relevé est ancien' in node.value for node in at.warning))
        self.assertTrue(any('dépasse le délai' in node.value for node in at.warning))
        self.assertEqual(len(at.success), 1)  # Clearly labelled LAST point only.

    def test_missing_receipt_never_displays_a_successful_backup(self):
        with patch.object(panel.operations_status, 'backup_status', return_value=dict(available=False)):
            at = AppTest.from_string('from operations_panel import render_backup_status\nrender_backup_status()', default_timeout=15).run()
        self.assertFalse(at.exception)
        self.assertFalse(at.success)
        self.assertFalse(at.metric)
        self.assertTrue(any('export de matchs' in node.value for node in at.caption))

    def test_linux_service_state_and_cycle_failure_remain_distinct(self):
        with patch.object(panel, 'linux_scheduler', return_value=SCHEDULER):
            at = AppTest.from_string('from operations_panel import render_scheduler, linux_scheduler\n'
                                     'render_scheduler(linux_scheduler())', default_timeout=15).run()
        self.assertFalse(at.exception)
        self.assertTrue(any('Planification active' in node.value for node in at.success))
        self.assertTrue(any('erreur ou une interruption' in node.value for node in at.warning))
        self.assertTrue(any('15 minutes après la fin' in node.value for node in at.caption))
        self.assertTrue(any('09 h 15' in node.value for node in at.caption))
        self.assertFalse(any('macOS' in node.value for node in at.info))

    def test_linux_browser_cannot_run_or_enable_training_even_for_admin(self):
        config = copy.deepcopy(automation.worker.DEFAULT_CONFIG)
        config.update(enabled=True, models_enabled=True, model_jobs_per_cycle=1)
        with patch.object(automation, '_managed_linux', return_value=True), \
                patch.object(panel, 'linux_scheduler', return_value=SCHEDULER), \
                patch.object(automation.cloud_storage, 'configured', return_value=False), \
                patch.object(automation.admin_access, 'can_admin', return_value=True), \
                patch.object(automation.worker, 'load_config', return_value=config), \
                patch.object(automation.worker, 'load_status', return_value={}), \
                patch.object(automation, '_coverage', return_value=[]) as coverage, \
                patch.object(automation.automation_health_panel, 'render'), \
                patch.object(automation.scheduler_reception_panel, 'render'), \
                patch.object(automation.worker, 'run_cycle', side_effect=AssertionError('No worker')) as run, \
                patch.object(automation.worker, 'save_config', side_effect=AssertionError('No save')) as save:
            at = AppTest.from_string('from automation_panel import render_automation\nrender_automation()', default_timeout=15).run()
            self.assertFalse(at.exception)
            self.assertTrue(at.button(key='automation_run').disabled)
            self.assertFalse(at.toggle)
            self.assertIn('"models_enabled": false', coverage.call_args.args[1])
            self.assertTrue(config['models_enabled'])  # Stored recipe preserved.
            run.assert_not_called()
            save.assert_not_called()

    def test_memory_kill_is_explained_separately_from_active_schedule(self):
        value = copy.deepcopy(SCHEDULER)
        value['worker']['result'] = 'oom-kill'
        with patch.object(panel, 'linux_scheduler', return_value=value):
            at = AppTest.from_string('from operations_panel import render_scheduler, linux_scheduler\n'
                                     'render_scheduler(linux_scheduler())', default_timeout=15).run()
        self.assertFalse(at.exception)
        self.assertTrue(any('Planification active' in item.value for item in at.success))
        self.assertTrue(any('manque de mémoire' in item.value for item in at.warning))


if __name__ == '__main__':
    unittest.main()
