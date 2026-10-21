"""Legacy computations cannot overwrite a campaign that finished meanwhile."""
from contextlib import ExitStack
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import catboost_scenarios as ml
import contextual_validation as contextual
import model_campaign as campaign
from test_model_campaign import NativeStub, fake_context
from test_seasonal_validation import sample


class PublicationGuardTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / 'provider'
        self.models = Path(folder.name) / 'models'
        stack = self.enterContext(ExitStack())
        stack.enter_context(patch.object(api, 'DATA_DIR', self.root))
        stack.enter_context(patch.object(ml, 'DATA_DIR', self.models))
        stack.enter_context(patch.object(contextual, 'REPORT_DIR', self.root))
        self.paths = campaign._paths('39')
        self.paths['base'].mkdir(parents=True)
        self.context = fake_context(sample(), '39', 'API-Football', sizes=(4,))

    def assert_legacy_publishers_blocked(self):
        path = contextual._report_path('39')
        path.write_text('previous context')
        report = {'deployment': {}}
        before = copy.deepcopy(report)
        with self.assertRaises(ml.ModelError):
            ml._publish(NativeStub(), report, 'API-Football', '39', None)
        with self.assertRaises(contextual.ContextualError):
            contextual.save_report(self.context)
        self.assertEqual(report, before)
        self.assertEqual(path.read_text(), 'previous context')
        self.assertFalse(self.models.exists())

    def test_finished_or_failed_campaign_and_invalid_manifest_are_fail_closed(self):
        for status in ('building', 'frozen', 'error', 'invalid'):
            with self.subTest(status=status):
                self.paths['manifest'].write_text(json.dumps({'status': status}))
                self.assert_legacy_publishers_blocked()

    def test_first_input_already_protects_artifacts_before_manifest(self):
        self.paths['input'].write_text('{}')
        self.assert_legacy_publishers_blocked()

    def test_campaign_holds_same_lock_as_legacy_publication(self):
        with campaign._lock(self.paths):
            self.assert_legacy_publishers_blocked()
            # Campaign training stages privately while holding that same lock.
            stage = self.root / 'isolated-models'
            ml._publish(NativeStub(), {'deployment': {}}, 'API-Football', '39', stage)
            self.assertTrue(ml._location('API-Football', '39', stage).joinpath('CURRENT.json').exists())

    def test_legacy_model_save_excludes_campaign_creation_until_pointer_is_published(self):
        owner = self
        class ConcurrentProbe(NativeStub):
            def save_model(self, path, format='cbm'):
                with owner.assertRaises(campaign.CampaignError):
                    with campaign._lock(owner.paths):
                        owner.fail('Campaign must not start while a legacy publisher holds the lock')
                super().save_model(path, format)
        ml._publish(ConcurrentProbe(), {'deployment': {}}, 'API-Football', '39', None)
        self.assertTrue(ml._location('API-Football', '39', None).joinpath('CURRENT.json').exists())


if __name__ == '__main__':
    unittest.main()
