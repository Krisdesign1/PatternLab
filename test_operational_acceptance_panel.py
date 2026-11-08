from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import operational_acceptance_panel as panel
from test_operational_acceptance import sample_report


class OperationalAcceptancePanelTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        p=patch.object(panel.acceptance,'collect',side_effect=lambda *a,**k:deepcopy(self.report))
        self.report=sample_report();self.read=p.start();self.addCleanup(p.stop)
        p=patch.object(panel.cloud_storage,'configured',return_value=False);p.start();self.addCleanup(p.stop)
        self.at=AppTest.from_string(f"from operational_acceptance_panel import render\nrender({str(self.root)!r})",default_timeout=15)

    def open_report(self):
        self.at.run().button(key='operational_acceptance_read').click().run();self.assertFalse(self.at.exception)
        return self.at

    def table(self,column):return next(t.value for t in self.at.dataframe if column in t.value.columns)

    def test_open_is_lazy_and_does_not_create_any_storage(self):
        self.at.run();self.assertFalse(self.at.exception);self.read.assert_not_called()
        self.assertFalse(self.at.dataframe);self.assertFalse(list(self.root.iterdir()))

    def test_current_success_keeps_global_reception_open_and_tables_distinct(self):
        self.open_report()
        self.assertFalse(self.at.success);self.assertFalse(self.at.metric)
        self.assertTrue(any('réception reste ouverte' in n.value for n in self.at.info))
        self.assertEqual(len(self.table('Tests réussis lors de cette réception')),2)
        self.assertEqual(self.table('Contrôle').loc[lambda frame:frame['Contrôle']=='Avantage prédictif','État'].iloc[0],
                         'Reste à démontrer')
        self.assertFalse(self.table('Modèle chargé pour ce contrôle').iloc[0]['Modèle chargé pour ce contrôle'])

    def test_filter_is_display_only_and_export_keeps_full_proof(self):
        with patch.object(panel.st,'download_button',wraps=panel.st.download_button) as download:
            self.open_report();self.at.selectbox(key='operational_acceptance_scope').set_value('remaining').run()
        self.assertEqual(self.read.call_count,1)
        self.assertEqual(set(self.table('Contrôle')['Portée']),{'Travaux restants'})
        exported=json.loads(download.call_args.args[1])
        self.assertEqual(len(exported['criteria']),11)
        self.assertFalse(exported['automatic_acceptance']);self.assertFalse(exported['atomic_across_sources'])

    def test_failed_refresh_clears_previous_tables_and_download_without_raw_error(self):
        self.open_report();self.read.side_effect=ValueError('private-dsn')
        self.at.button(key='operational_acceptance_read').click().run()
        self.assertFalse(self.at.exception);self.assertFalse(self.at.dataframe);self.assertFalse(self.at.get('download_button'))
        self.assertTrue(self.at.error);self.assertNotIn('private-dsn',str([n.value for n in self.at.error]))

    def test_partial_sources_are_visible_and_not_green(self):
        self.report['source_failures']=['capture_windows_unavailable'];self.report['protocol_summary']=None
        self.report['evidence']['rows'][0].update(state='invalid',recorded_at=None,tests_passed=None)
        self.open_report()
        self.assertTrue(self.at.warning);self.assertFalse(self.at.success)
        self.assertEqual(self.table('Lot').iloc[0]['État'],'Preuves incohérentes')

    def test_cloud_view_never_reads_local_receipts(self):
        with patch.object(panel.cloud_storage,'configured',return_value=True):self.at.run()
        self.read.assert_not_called();self.assertFalse(self.at.button);self.assertFalse(self.at.exception)


if __name__=='__main__':unittest.main()
