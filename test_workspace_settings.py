"""API-only navigation clears stale sessions and cannot regenerate old data."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from streamlit.testing.v1 import AppTest
import api_football
import api_football_panel
import database
import scenario_workspace_panel
import workspace_sources


class WorkspaceSettingsTests(unittest.TestCase):
    def test_single_provider_pages_cannot_access_legacy_storage(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with patch.object(workspace_sources,'PREFERENCE_PATH',root/'settings.json'), \
                 patch.object(database,'DB_PATH',root/'coupons.db'), \
                 patch.object(database,'init_db',side_effect=AssertionError('SQLite must remain retired')), \
                 patch.object(database,'load_coupons',side_effect=AssertionError('No manual data')), \
                 patch.object(api_football_panel,'render_api_football') as render, \
                 patch.object(scenario_workspace_panel,'_snapshot',return_value={'competitions':{},'fixtures':[],'day_count':0,'usable_count':0,'updated_at':None}), \
                 patch.object(api_football.ApiFootballClient,'get',side_effect=AssertionError('No automatic API call')) as network:
                at=AppTest.from_file('app.py',default_timeout=20)
                at.session_state['workspace_forecast']={'source':'football-data.org'}
                at.session_state['current_coupon']=['1st','2nd']
                at.run()
                self.assertFalse(at.exception)
                self.assertNotIn('workspace_forecast',at.session_state)
                self.assertNotIn('current_coupon',at.session_state)
                at.switch_page('ui_pages/settings.py').run()
                self.assertFalse(at.exception)
                render.assert_called_once()
                self.assertFalse(at.selectbox)
                self.assertFalse(at.file_uploader)
                self.assertFalse(at.get('page_link'))
                self.assertTrue(any('source unique' in c.value for c in at.caption))
                with self.assertRaises(ValueError): at.switch_page('legacy_workspace.py')
                self.assertFalse((root/'coupons.db').exists())
                network.assert_not_called()
