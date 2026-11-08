from copy import deepcopy
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import causal_feature_panel as panel
from causal_feature_workspace import digest
from test_prematch_feature_snapshot import build


def prepared():
    snapshot=build()
    return dict(version='test',id='cohort',entries=[dict(snapshot_id=snapshot['id'])],exclusions=[]),{snapshot['id']:snapshot}


class CausalFeaturePanelTests(unittest.TestCase):
    def setUp(self):
        patcher=patch.object(panel.preparation,'prepare_cohort',return_value=prepared())
        self.prepare=patcher.start();self.addCleanup(patcher.stop)
        self.at=AppTest.from_string('from causal_feature_panel import render\nrender()',default_timeout=10).run()

    def submit(self):
        self.at.text_input(key='causal_feature_match_id').set_value('999')
        self.at.text_input(key='causal_feature_decision').set_value('2026-02-15T10:00:00Z')
        return self.at.button(key='causal_feature_prepare').click().run()

    def test_opening_and_rerunning_are_read_only_until_submit(self):
        self.assertFalse(self.at.exception);self.prepare.assert_not_called()
        self.at.run();self.prepare.assert_not_called()
        self.submit()
        self.assertFalse(self.at.exception);self.prepare.assert_called_once()
        self.assertEqual(self.at.dataframe[1].value.shape[0],len(build()['features']))
        self.at.run();self.prepare.assert_called_once()

    def test_error_or_exclusion_clears_previous_success(self):
        self.submit();self.assertTrue(self.at.dataframe)
        self.prepare.side_effect=ValueError('Instant avec fuseau obligatoire')
        self.submit()
        self.assertFalse(self.at.exception)
        self.assertTrue(self.at.error);self.assertFalse(self.at.dataframe)
        self.prepare.side_effect=None
        self.prepare.return_value=(dict(exclusions=[dict(match_id='999',decision_at='2026-02-15T10:00:00Z',
            reason='target_availability_unproven',detail='Aucune affiche antérieure')]),{})
        self.submit()
        self.assertFalse(self.at.exception)
        self.assertEqual(len(self.at.dataframe[0].value),1)


if __name__=='__main__':unittest.main()
