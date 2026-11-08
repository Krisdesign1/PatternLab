"""Import review UI is lazy and local; only an explicit owner choice mutates."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import admin_access
import api_football as api
import fixture_import_policy as policy


def render():
    from fixture_import_panel import render_import_review
    render_import_review()


def raw_fixture():
    return dict(fixture=dict(id=123, date='2026-10-24T12:00:00Z', status=dict(short='FT')),
                league=dict(id=39, name='Test league', country='Test country', season=2026),
                teams=dict(home=dict(id=10, name='Home'), away=dict(id=20, name='Away')),
                score=dict(halftime=dict(home=1, away=0), fulltime=dict(home=1, away=1)))


class FixtureImportPanelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'fixtures.json'
        self.patch(api, 'STORE_PATH', self.path)
        self.prior = raw_fixture()
        self.received = deepcopy(self.prior)
        self.received['teams']['home']['id'] = 11
        self.make_conflict()
        self.quarantines = self.patch(api, 'import_quarantines',
            side_effect=lambda path: deepcopy(self.current))
        self.observations = self.patch(api, 'load_import_observations',
            side_effect=lambda path, ids: deepcopy(self.events))
        self.review = self.patch(api, 'review_import', side_effect=self.apply_review)
        self.owner = self.patch(admin_access, 'can_admin', return_value=True)
        self.require = self.patch(admin_access, 'require_admin', return_value=None)
        self.forbidden = [self.patch(api, name, side_effect=AssertionError('No provider read or automatic write'))
                          for name in ('load_key', 'load_fixtures', 'load_matches', 'save_fixtures')]
        self.forbidden.append(self.patch(api.ApiFootballClient, 'get', side_effect=AssertionError('No API')))

    def patch(self, target, name, *args, **kwargs):
        patcher = patch.object(target, name, *args, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def make_conflict(self):
        selected, decision, reason = policy.decide(self.prior, self.received, api.normalize_fixture)
        self.assertEqual(decision, 'quarantined')
        self.current = [selected]
        self.events = [policy.observation(self.prior, self.received, selected, decision, reason,
                                         '2026-10-27T20:00:00+00:00')]

    def apply_review(self, *args, **kwargs):
        self.current = []
        return dict(id='reviewed')

    def open(self):
        at = AppTest.from_function(render, default_timeout=10)
        at.session_state['import_quality_open'] = True
        at.run()
        self.assertFalse(at.exception)
        return at

    def set_reason(self, at, value='Provider correction checked'):
        at.text_input(key=f'import_quality_reason_{self.events[-1]["id"]}').set_value(value).run()
        self.assertFalse(at.exception)
        return at

    def assert_no_side_effect(self, *, review=False):
        for call in self.forbidden:
            call.assert_not_called()
        if not review:
            self.review.assert_not_called()
            self.require.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_closed_expander_never_reads_storage_credentials_or_api(self):
        at = AppTest.from_function(render).run()
        self.assertFalse(at.exception)
        self.quarantines.assert_not_called()
        self.observations.assert_not_called()
        self.assert_no_side_effect()

    def test_opening_exposes_receipts_and_versions_without_choosing_a_version(self):
        before, events = deepcopy(self.current), deepcopy(self.events)
        at = self.open()
        self.quarantines.assert_called_once_with(self.path)
        self.observations.assert_called_once_with(self.path, ids=['123'])
        self.assertEqual(at.selectbox(key='import_quality_match').value, '123')
        self.assertEqual(len(at.dataframe), 2)
        self.assertEqual(at.dataframe[0].value['Décision d’import'].tolist(), ['quarantined'])
        self.assertEqual(at.dataframe[1].value['Version'].tolist(),
                         ['Dernière réponse reçue', 'Version conservée'])
        self.assertEqual(self.current, before)
        self.assertEqual(self.events, events)
        self.assertTrue(at.button(key='import_quality_choose_received').disabled)
        self.assertTrue(at.button(key='import_quality_choose_prior').disabled)
        self.assert_no_side_effect()

    def test_non_owner_cannot_choose_either_version_even_with_reason(self):
        self.owner.return_value = False
        at = self.set_reason(self.open())
        self.assertTrue(at.button(key='import_quality_choose_received').disabled)
        self.assertTrue(at.button(key='import_quality_choose_prior').disabled)
        self.assert_no_side_effect()

    def test_missing_halftime_response_cannot_be_selected_but_complete_prior_can(self):
        self.received = deepcopy(self.prior)
        self.received['score']['halftime'] = dict(home=None, away=None)
        self.make_conflict()
        at = self.set_reason(self.open())
        self.assertTrue(at.button(key='import_quality_choose_received').disabled)
        self.assertFalse(at.button(key='import_quality_choose_prior').disabled)
        self.assertEqual(at.dataframe[1].value['Pause'].tolist(), ['—', '1-0'])
        self.assert_no_side_effect()

    def test_malformed_received_score_shapes_do_not_crash_review(self):
        for score in ('invalid score object', dict(halftime='1-0', fulltime=dict(home=1, away=1))):
            with self.subTest(score=score):
                self.received = deepcopy(self.prior)
                self.received['score'] = score
                self.make_conflict()
                at = self.set_reason(self.open())
                self.assertTrue(at.button(key='import_quality_choose_received').disabled)
                self.assertFalse(at.button(key='import_quality_choose_prior').disabled)
                self.assertEqual(at.dataframe[1].value['Pause'].tolist(), ['—', '1-0'])
                self.assert_no_side_effect()

    def test_malformed_received_team_shapes_do_not_crash_review(self):
        for teams in ('invalid teams object', dict(home='invalid home', away=dict(id=20, name='Away'))):
            with self.subTest(teams=teams):
                self.received = deepcopy(self.prior)
                self.received['teams'] = teams
                self.make_conflict()
                at = self.set_reason(self.open())
                self.assertTrue(at.button(key='import_quality_choose_received').disabled)
                self.assertFalse(at.button(key='import_quality_choose_prior').disabled)
                self.assert_no_side_effect()

    def test_received_choice_passes_exact_reason_observation_and_owner_boundary(self):
        observation = self.events[-1]['id']
        reason = '  Received identities independently checked  '
        at = self.set_reason(self.open(), reason)
        at.button(key='import_quality_choose_received').click().run()
        self.assertFalse(at.exception)
        self.review.assert_called_once_with('123', path=self.path, choice='received',
                                            expected_observation=observation, reason=reason)
        self.require.assert_called_once_with()
        self.assertTrue(any('Aucune correction' in info.value for info in at.info))
        self.assert_no_side_effect(review=True)

    def test_prior_choice_passes_expected_observation_without_automatic_refetch(self):
        observation = self.events[-1]['id']
        at = self.set_reason(self.open(), 'Keep confirmed prior result')
        at.button(key='import_quality_choose_prior').click().run()
        self.assertFalse(at.exception)
        self.review.assert_called_once_with('123', path=self.path, choice='prior',
                                            expected_observation=observation,
                                            reason='Keep confirmed prior result')
        self.require.assert_called_once_with()
        self.assert_no_side_effect(review=True)

    def test_concurrent_observation_or_review_failure_is_visible_and_retains_conflict(self):
        self.review.side_effect = api.ApiFootballError('Compare and swap failed')
        before, events = deepcopy(self.current), deepcopy(self.events)
        at = self.set_reason(self.open())
        at.button(key='import_quality_choose_received').click().run()
        self.assertFalse(at.exception)
        self.assertTrue(any('nouvelle réponse est arrivée' in value.value for value in at.warning))
        self.assertEqual(self.current, before)
        self.assertEqual(self.events, events)
        self.assert_no_side_effect(review=True)

    def test_missing_observation_disables_review_without_inventing_history(self):
        self.events = []
        at = self.open()
        self.assertFalse(at.button)
        self.assertTrue(any('n’est pas conservée' in value.value for value in at.warning))
        self.assert_no_side_effect()

    def test_storage_read_failures_are_visible_and_do_not_review(self):
        self.quarantines.side_effect = api.ApiFootballError('Unavailable')
        at = self.open()
        self.assertTrue(any('ne peuvent pas être consultées' in value.value for value in at.warning))
        self.observations.assert_not_called()
        self.assert_no_side_effect()

    def test_observation_read_failure_keeps_review_unavailable(self):
        self.observations.side_effect = api.ApiFootballError('Unavailable')
        at = self.open()
        self.assertFalse(at.button)
        self.assertTrue(any('aucune revue n’est possible' in value.value for value in at.warning))
        self.assert_no_side_effect()

    def test_empty_quarantine_does_not_load_unrelated_observations(self):
        self.current = []
        at = self.open()
        self.assertTrue(any('Aucune correction' in value.value for value in at.info))
        self.observations.assert_not_called()
        self.assert_no_side_effect()


if __name__ == '__main__':
    unittest.main()
