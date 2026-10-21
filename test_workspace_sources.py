"""The single-provider policy must reject every retired source."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import api_football
import workspace_sources as sources


class WorkspaceSourceTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path=Path(directory.name)/'settings.json'
        p=patch.object(sources,'PREFERENCE_PATH',self.path)
        p.start(); self.addCleanup(p.stop)

    def test_default_and_persisted_source_are_api_only_without_network(self):
        with patch.object(api_football.ApiFootballClient,'get') as network:
            self.assertEqual(sources.SOURCES,('API-Football',))
            self.assertEqual(sources.selected_source(),'API-Football')
            sources.save_source('API-Football')
            self.assertEqual(json.loads(self.path.read_text())['source'],'API-Football')
            self.assertEqual(sources.selected_source(),'API-Football')
            network.assert_not_called()

    def test_retired_sources_cannot_load_save_or_reactivate(self):
        for source in ('football-data.org','Forebet','Unknown'):
            for operation in (sources.save_source,sources.load_matches,sources.data_signature,sources.default_league):
                with self.subTest(source=source,operation=operation.__name__), self.assertRaises(sources.WorkspaceSourceError):
                    operation(source)
            self.path.write_text(json.dumps(dict(version=1,source=source)))
            with self.assertRaises(sources.WorkspaceSourceError): sources.selected_source()

    def test_invalid_configuration_is_rejected(self):
        for raw in ('broken','[]','{"source":"API-Football"}'):
            self.path.write_text(raw)
            with self.assertRaises(sources.WorkspaceSourceError): sources.selected_source()

    def test_failed_write_preserves_previous_policy(self):
        sources.save_source('API-Football'); before=self.path.read_bytes()
        with patch.object(sources.os,'replace',side_effect=OSError('failure')):
            with self.assertRaises(sources.WorkspaceSourceError): sources.save_source('API-Football')
        self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(list(self.path.parent.glob('.workspace-*')),[])
