from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import automation_health as health
import automation_health_panel as panel


class LinuxHealthPanelTests(unittest.TestCase):
    def render_wait(self, minutes):
        start = datetime(2026, 11, 6, 10, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as folder:
            report = dict(started_at=start.isoformat(), status='completed', issues=[])
            with health.monitor(Path(folder), report, clock=lambda: start+timedelta(hours=1),
                                interval_minutes=15, origin='service'):
                pass
            cutoff = start + timedelta(hours=1, minutes=minutes)
            view = health.load(Path(folder), now=cutoff)
        with patch.object(panel.health, 'load', return_value=view), \
             patch.object(panel.cloud_storage, 'configured', return_value=False), \
             patch.object(panel.automation_observation_panel, 'render'), \
             patch.object(panel, 'datetime') as clock:
            clock.now.return_value = cutoff
            app = AppTest.from_string('''
import automation_health_panel
automation_health_panel.render(dict(enabled=True), scheduler=dict(
    backend='systemd', running=True, process_running=False))
''').run(timeout=10)
        self.assertFalse(app.exception)
        return app

    def test_normal_linux_wait_has_no_interruption_warning(self):
        app = self.render_wait(15)
        self.assertFalse(any('écart(s)' in value.value for value in app.warning))
        self.assertTrue(any('après la fin' in value.value for value in app.caption))

    def test_genuine_linux_delay_remains_visible(self):
        app = self.render_wait(30)
        self.assertTrue(any('1 écart(s)' in value.value for value in app.warning))


if __name__ == '__main__':
    unittest.main()
