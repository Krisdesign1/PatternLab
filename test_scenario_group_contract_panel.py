from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import scenario_group_contract_panel as panel


def report(*, status='valid', size=2):
    probability = .5**size
    return dict(version='scenario-group-contract-v1', entry_id='a'*64, size=size,
                status=status, input_sha256='b'*64, common_cutoff='2026-11-01',
                decision_at='2026-11-01T18:00:00+00:00', issues=[],
                methods=[dict(name='clubs',version='saved-v1',top1_probability=probability,
                              top3_probability=3*probability,remaining_probability=1-3*probability,
                              assumptions=['Indépendance supposée entre les résultats des matchs.'])])


def table(at, column):
    return next(node.value for node in at.dataframe if column in node.value.columns)


class GroupContractPanelTests(unittest.TestCase):
    def setUp(self):
        self.value = report()
        self.patch = patch.object(panel, 'inspect', return_value=self.value)
        self.inspect = self.patch.start(); self.addCleanup(self.patch.stop)
        self.source = "from scenario_group_contract_panel import render\nrender({'id':'loaded','created_at':'2026-11-01T18:01:00Z'})"
        self.at = AppTest.from_string(self.source, default_timeout=10)

    def test_valid_group_uses_tables_with_absolute_mass_and_dates(self):
        self.at.run(); self.assertFalse(self.at.exception)
        summary = table(self.at, 'État du contrôle').iloc[0]
        self.assertEqual(summary['État du contrôle'], 'Contrôles cohérents')
        self.assertEqual(summary['Décision conservée (Kinshasa)'], '01/11/2026 19:00:00')
        self.assertEqual(summary['Enregistrement déclaré (Kinshasa)'], '01/11/2026 19:01:00')
        method = table(self.at, 'Premier scénario (%)').iloc[0]
        self.assertEqual(method['Premier scénario (%)'], 25.)
        self.assertEqual(method['Trois premiers scénarios (%)'], 75.)
        self.assertEqual(method['Autres scénarios (%)'], 25.)
        self.assertFalse(self.at.success)
        self.assertFalse(self.at.expander)

    def test_legacy_missing_proofs_are_explicit_without_rewriting_probabilities(self):
        self.value.update(status='unverified',decision_at=None)
        self.value['issues'] = [dict(code='binding_unverified',severity='unverified',method='clubs',
                                    message='Les identifiants ordonnés n’ont pas été conservés.')]
        before = deepcopy(self.value)
        self.at.run(); self.assertFalse(self.at.exception)
        self.assertEqual(table(self.at, 'État du contrôle').iloc[0]['État du contrôle'], 'Preuves incomplètes')
        self.assertEqual(table(self.at, 'Décision conservée (Kinshasa)').iloc[0]
                         ['Décision conservée (Kinshasa)'], 'Non conservée')
        self.assertEqual(table(self.at, 'Contrôle de la méthode').iloc[0]
                         ['Contrôle de la méthode'], 'Preuves incomplètes')
        self.assertEqual(table(self.at, 'Motif').iloc[0]['Point contrôlé'], 'binding_unverified')
        self.assertEqual(self.value, before)

    def test_invalid_claim_and_its_method_are_visible_with_no_success_claim(self):
        self.value.update(status='invalid')
        self.value['issues'] = [dict(code='component_cutoff_mismatch',severity='invalid',method='clubs · 39',
                                    message='Les composants n’utilisent pas la même coupure.')]
        self.at.run(); self.assertFalse(self.at.exception)
        self.assertEqual(table(self.at, 'État du contrôle').iloc[0]['État du contrôle'], 'Incohérences détectées')
        self.assertEqual(table(self.at, 'Contrôle de la méthode').iloc[0]
                         ['Contrôle de la méthode'], 'Incohérences détectées')
        self.assertIn('coupure', table(self.at, 'Motif').iloc[0]['Motif'])
        self.assertFalse(self.at.success)

    def test_failed_new_inspection_does_not_keep_previous_tables_or_download(self):
        self.at.run(); self.assertTrue(self.at.dataframe)
        self.inspect.side_effect = ValueError('Contenu incohérent')
        self.at.run(); self.assertFalse(self.at.exception)
        self.assertTrue(self.at.error)
        self.assertFalse(self.at.dataframe)
        self.assertFalse(self.at.get('download_button'))

    def test_unknown_inspector_status_is_not_presented_as_valid(self):
        self.value['status'] = 'unexpected'
        self.at.run(); self.assertFalse(self.at.exception)
        self.assertTrue(self.at.error)
        self.assertFalse(self.at.dataframe)

    def test_twenty_match_probabilities_remain_nonzero_and_absolute(self):
        self.inspect.return_value = report(size=20)
        self.at.run(); self.assertFalse(self.at.exception)
        method = table(self.at, 'Premier scénario (%)').iloc[0]
        self.assertEqual(method['Premier scénario (%)'], .5**20*100)
        self.assertGreater(method['Premier scénario (%)'], 0)
        self.assertAlmostEqual(method['Trois premiers scénarios (%)']+method['Autres scénarios (%)'], 100)
        self.assertLess(method['Trois premiers scénarios (%)'], 1)

    def test_reruns_only_inspect_supplied_forecast_without_io_fit_or_api(self):
        import api_football
        import prediction_journal
        import prospective_models
        with patch.object(api_football.ApiFootballClient, 'get', side_effect=AssertionError('No API')), \
                patch.object(prediction_journal, 'list_entries', side_effect=AssertionError('No journal read')), \
                patch.object(prospective_models, 'build_methods', side_effect=AssertionError('No model construction')):
            self.at.run(); self.at.run()
        self.assertFalse(self.at.exception)
        self.inspect.assert_called_with({'id':'loaded','created_at':'2026-11-01T18:01:00Z'})
        self.assertEqual(self.inspect.call_count, 2)
        self.assertFalse(self.at.button)
        self.assertTrue(any('ni l’heure physique' in row.value for row in self.at.caption))

    def test_export_keeps_group_identity_and_input_fingerprint(self):
        with patch.object(panel.st, 'download_button', wraps=panel.st.download_button) as download:
            self.at.run()
        self.assertFalse(self.at.exception)
        exported = json.loads(download.call_args.args[1])
        self.assertEqual(exported['entry_id'], 'a'*64)
        self.assertEqual(exported['input_sha256'], 'b'*64)
        self.assertEqual(download.call_args.kwargs['file_name'], 'patternlab_controle_groupe_aaaaaaaaaaaa.json')

    def test_distinct_keys_allow_two_loaded_groups_on_one_page(self):
        at = AppTest.from_string("from scenario_group_contract_panel import render\n"
                                "render({'id':'one'},key='one')\nrender({'id':'two'},key='two')",
                                default_timeout=10).run()
        self.assertFalse(at.exception)
        self.assertEqual(len(at.get('download_button')), 2)

    def test_actual_inspector_and_twenty_match_snapshot_render_together(self):
        from scenario_group_contract import inspect
        self.inspect.side_effect = inspect
        at = AppTest.from_string('from scenario_group_contract_panel import render\n'
                                 'from test_scenario_group_contract import entry\n'
                                 'render(entry(size=20))', default_timeout=10).run()
        self.assertFalse(at.exception)
        self.assertEqual(table(at, 'État du contrôle').iloc[0]['État du contrôle'], 'Contrôles cohérents')
        self.assertEqual(table(at, 'Matchs dans le groupe').iloc[0]['Matchs dans le groupe'], 20)
        self.assertLess(table(at, 'Premier scénario (%)').iloc[0]['Premier scénario (%)'], 1)


if __name__ == '__main__':
    unittest.main()
