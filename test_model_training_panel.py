"""UI makes training explicit and keeps the reserved-test report separate."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import database
import model_training_panel as panel


class ModelTrainingPanelTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.rows = [dict(league='39', competition='Premier League', country='England') for _ in range(200)]
        self.report = {'source': 'API-Football', 'league': '39',
                       'split': {name: {'matches': count, 'start': '2022-01-01', 'end': '2026-10-17',
                                        'groups': count // 4}
                                 for name, count in [('train', 120), ('validation', 40), ('test', 40)]},
                       'selection': {'alpha': 0, 'params': {'depth': 3}},
                       'validation': {name: dict(tested=10, top1=1, top3=2, loss=4.0)
                                      for name in panel.METHODS},
                       'test': {name: dict(tested=10, top1=0, top3=1, loss=4.1)
                                for name in panel.METHODS},
                       'deployment': {'rows': 200, 'trained_through': '2026-10-17', 'generation': 'test'}}
        self.patch(database, 'DB_PATH', Path(self.directory.name) / 'coupons.db')
        self.patch(panel.sources, 'selected_source', return_value='API-Football')
        self.patch(panel, '_rows', side_effect=lambda *args: (self.rows, {}))
        self.patch(panel.model_campaign_panel, 'render')
        self.campaign = self.patch(panel.campaign, 'load_manifest', return_value=None)
        self.loader = self.patch(panel.ml, 'load_report', return_value=None)
        self.trainer = self.patch(panel.ml, 'train', return_value=self.report)
        self.at = AppTest.from_file(str(Path(__file__).parent / 'app.py'), default_timeout=15).run()
        self.at.switch_page('ui_pages/models.py').run()
        self.at.segmented_control(key='model_validation_view').set_value('deployment').run()
        self.assertFalse(self.at.exception)

    def patch(self, target, name, *args, **kwargs):
        patcher = patch.object(target, name, *args, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def test_training_is_explicit_and_report_can_be_reopened(self):
        self.trainer.assert_not_called()
        self.loader.return_value = self.report
        self.at.button(key='model_train').click().run()
        self.assertFalse(self.at.exception)
        self.trainer.assert_called_once_with(self.rows, '39', 'API-Football')
        self.assertEqual(len(self.at.dataframe[0].value), 4)
        self.at.run()
        self.assertEqual(self.trainer.call_count, 1)

    def test_training_error_preserves_page(self):
        self.trainer.side_effect = panel.ml.ModelError('Historique insuffisant pour séparer les jours')
        self.at.button(key='model_train').click().run()
        self.assertFalse(self.at.exception)
        self.assertTrue(any('Historique insuffisant' in value.value for value in self.at.error))

    def test_short_competition_cannot_start_training(self):
        self.rows[:] = self.rows[:10]
        self.at.run()
        self.assertTrue(self.at.button(key='model_train').disabled)
        self.trainer.assert_not_called()

    def test_campaign_frozen_model_cannot_be_manually_retrained(self):
        self.campaign.return_value = dict(status='frozen')
        self.at.run()
        self.assertFalse(self.at.exception)
        self.assertTrue(self.at.button(key='model_train').disabled)
        self.assertTrue(any('première campagne est déjà verrouillée' in value.value for value in self.at.info))
        self.trainer.assert_not_called()


if __name__ == '__main__':
    unittest.main()
