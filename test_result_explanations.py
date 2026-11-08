"""Status explanations never settle or rewrite immutable forecasts."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import result_explanations as reasons


NOW = datetime(2026, 10, 27, 18, tzinfo=timezone.utc)


def fixture():
    return dict(match_id='1', league='39', home_id='10', away_id='20', home='A', away='B',
                timestamp='2026-10-24T12:00:00', provider='api-football')


def entry(**values):
    return dict(dict(id='saved', status='unresolved', fixtures=[fixture()], observed=[], issues=[],
                     created_at='2026-10-24T09:00:00Z', checked_at='2026-10-25T09:00:00Z'), **values)


def raw(short='FT', ht=None, ft=None):
    return dict(fixture=dict(id=1, date='2026-10-24T12:00:00Z', status=dict(short=short)),
                league=dict(id=39), teams=dict(home=dict(id=10), away=dict(id=20)),
                score=dict(halftime=dict(home=0, away=0) if ht is None else ht,
                           fulltime=dict(home=1, away=0) if ft is None else ft))


class ExplanationTests(unittest.TestCase):
    def result(self, observation, saved=None):
        return reasons.describe_entry(saved or entry(), raw_by_id={'1': observation}, now=NOW)

    def test_distinct_provider_states(self):
        expected = {'PST':'Reporté', 'CANC':'Annulé', 'WO':'Forfait', 'AWD':'Décision administrative',
                    'ABD':'Abandonné', 'SUSP':'Suspendu', 'INT':'Interrompu'}
        for code, label in expected.items():
            with self.subTest(code=code):
                result = self.result(raw(code))
                self.assertEqual(result['label'], label)
                self.assertEqual(result['matches'][0]['raw_status'], code)

    def test_24_oct_missing_halftime_does_not_invent_result(self):
        saved = entry(issues=['Match 1 : Score HT/FT absent ou invalide'])
        before = deepcopy(saved)
        result = self.result(raw(ht=dict(home=None, away=None), ft=dict(home=4, away=1)), saved)
        self.assertEqual(result['label'], 'Score à la pause manquant')
        self.assertEqual(saved, before)
        self.assertNotIn('actual', result)

    def test_regulation_score_required_for_penalty_match(self):
        result = self.result(raw('PEN', ft=dict(home=None, away=None)))
        self.assertEqual(result['label'], 'Score à 90 minutes manquant')

    def test_both_scores_absent_and_incoherent_scores(self):
        self.assertEqual(self.result(raw(ht={}, ft={}))['label'], 'Scores manquants')
        self.assertEqual(self.result(raw(ht=dict(home=2, away=0), ft=dict(home=1, away=0)))['label'], 'Scores incohérents')

    def test_valid_local_result_requires_reconciliation(self):
        saved = entry()
        result = self.result(raw(), saved)
        self.assertEqual(result['label'], 'Résultat à rapprocher')
        self.assertEqual(saved['status'], 'unresolved')
        self.assertNotIn('actual', saved)

    def test_complete_final_keeps_strict_protocol_schedule_review(self):
        saved = dict(status='unresolved', fixture=fixture(),
                     captured_at='2026-10-24T08:25:00Z',
                     issues=['Horaire modifié depuis la capture ; résultat à examiner.'])
        observation = raw(); observation['fixture']['date'] = '2026-10-24T10:00:00Z'
        original = deepcopy((saved, observation))
        result = self.result(observation, saved)
        match = result['matches'][0]
        self.assertEqual((match['state'], match['label']), ('schedule_changed', 'Horaire à examiner'))
        self.assertIn(saved['issues'][0], match['reason'])
        self.assertIn('règles de cette prévision', match['reason'])
        self.assertNotIn('perdu', match['reason'].casefold())
        self.assertEqual((saved, observation), original)
        self.assertNotIn('actual', saved)

    def test_complete_final_names_capture_after_effective_kickoff(self):
        saved = entry(created_at='2026-10-24T10:25:00Z',
                      issues=['Match 1 : Prévision ou résultat incompatible avec le nouvel horaire.'])
        observation = raw(); observation['fixture']['date'] = '2026-10-24T10:00:00Z'
        original = deepcopy(saved)
        result = self.result(observation, saved)
        match = result['matches'][0]
        self.assertEqual((match['state'], match['label']),
                         ('noncausal_forecast', 'Prévision hors période avant match'))
        self.assertIn('ne précède pas les deux horaires', match['reason'])
        self.assertNotIn('perdu', match['reason'].casefold())
        self.assertEqual(saved, original)

    def test_fresh_complete_final_can_supply_previous_missing_confirmation(self):
        for issue in ('Horaire modifié : résultat final confirmé nécessaire.',
                      'Match 1 : Score HT/FT absent ou invalide'):
            with self.subTest(issue=issue):
                saved = entry(issues=[issue])
                observation = raw(); observation['fixture']['date'] = '2026-10-24T10:00:00Z'
                result = self.result(observation, saved)
                self.assertEqual(result['matches'][0]['state'], 'awaiting_reconciliation')
                self.assertEqual(result['label'], 'Résultat à rapprocher')

    def test_complete_final_preserves_explicit_identity_and_protocol_rejections(self):
        for issue, state, label in (
                ('Identité du match modifiée.', 'identity_review', 'Identité à vérifier'),
                ('La prévision ne correspond pas au protocole figé.', 'protocol_review', 'Protocole à vérifier'),
                ('Relevés API contradictoires.', 'conflicting_observations', 'Relevés contradictoires')):
            with self.subTest(issue=issue):
                saved = entry(issues=[issue]); original = deepcopy(saved)
                result = self.result(raw(), saved)['matches'][0]
                self.assertEqual((result['state'], result['label']), (state, label))
                self.assertEqual(result['reason'], issue)
                self.assertEqual(saved, original)

    def test_complete_final_with_changed_provider_identity_is_not_awaiting_reconciliation(self):
        observation = raw(); observation['teams']['home']['id'] = 99
        result = self.result(observation, entry(issues=['Identité du match modifiée.']))
        self.assertEqual(result['matches'][0]['state'], 'identity_mismatch')
        self.assertEqual(result['label'], 'Identité à vérifier')

    def test_other_fixture_blocker_does_not_hide_fresh_complete_final(self):
        second = dict(fixture(), match_id='2', home_id='30', away_id='40')
        saved = entry(fixtures=[fixture(), second],
                      issues=['Match 1 : Prévision ou résultat incompatible avec le nouvel horaire.'])
        first_raw = raw(); first_raw['fixture']['date'] = '2026-10-24T10:00:00Z'
        second_raw = raw(); second_raw['fixture']['id'] = 2
        second_raw['teams'] = dict(home=dict(id=30), away=dict(id=40))
        result = reasons.describe_entry(saved, raw_by_id={'1': first_raw, '2': second_raw}, now=NOW)
        matches = {row['match_id']: row for row in result['matches']}
        self.assertEqual(matches['1']['state'], 'schedule_changed')
        self.assertEqual(matches['2']['state'], 'awaiting_reconciliation')
        self.assertNotIn('Match 1', matches['2']['reason'])

    def test_saved_settled_observation_stays_authority_despite_old_schedule_issue(self):
        saved = entry(status='settled', observed=[dict(match_id='1', ht='0-0', ft='1-0', value='2nd')],
                      issues=['Horaire modifié depuis la capture ; résultat à examiner.'])
        observation = raw(); observation['fixture']['date'] = '2026-10-24T10:00:00Z'
        original = deepcopy(saved)
        result = self.result(observation, saved)
        self.assertEqual(result['label'], 'Évalué')
        self.assertEqual(result['blocking'], [])
        self.assertEqual(saved, original)

    def test_stale_live_status_is_not_presented_as_still_live(self):
        result = self.result(raw('2H'))
        self.assertEqual(result['label'], 'Résultat final non reçu')
        self.assertEqual(result['matches'][0]['raw_status'], '2H')
        self.assertNotIn('postponed', result['matches'][0]['state'])

    def test_future_live_and_unconfirmed_statuses(self):
        value = raw('NS'); value['fixture']['date'] = '2026-10-28T12:00:00Z'
        self.assertEqual(self.result(value)['label'], 'Reprogrammé au 28/10 à 13:00')
        value = raw('HT'); value['fixture']['date'] = '2026-10-27T17:00:00Z'
        same_schedule = entry(fixtures=[dict(fixture(), timestamp='2026-10-27T17:00:00Z')])
        self.assertEqual(self.result(value, same_schedule)['label'], 'Mi-temps')
        self.assertEqual(self.result(raw('TBD'))['label'], 'Horaire non confirmé')

    def test_changed_schedule_names_new_local_date_without_rewriting_forecast(self):
        saved = entry(issues=['Horaire modifié : résultat final confirmé nécessaire.'])
        before = deepcopy(saved)
        observation = raw('NS'); observation['fixture']['date'] = '2026-10-28T17:30:00Z'
        result = self.result(observation, saved)
        self.assertEqual(result['label'], 'Reprogrammé au 28/10 à 18:30')
        self.assertIn('24/10/2026 à 13:00 → 28/10/2026 à 18:30', result['blocking'][0]['reason'])
        self.assertEqual(saved, before)

    def test_old_schedule_and_identity_issues_are_named_without_current_observation(self):
        cases = [('Horaire modifié : résultat final confirmé nécessaire.', 'Horaire modifié'),
                 ('Les clubs, la compétition ou l’horaire ont changé.', 'Affiche modifiée'),
                 ('Relevés API contradictoires.', 'Relevés contradictoires'),
                 ('Match 1 absent du relevé local.', 'Match absent du relevé')]
        for message, label in cases:
            with self.subTest(label=label):
                self.assertEqual(reasons.describe_entry(entry(issues=[message]), now=NOW)['label'], label)

    def test_source_and_identity_mismatch_fail_closed(self):
        for field in ('league', 'home', 'away', 'provider'):
            value = raw('PST')
            if field == 'league': value['league']['id'] = 40
            elif field == 'provider': value['provider'] = 'other'
            else: value['teams'][field]['id'] = 99
            self.assertEqual(self.result(value)['label'], 'Identité à vérifier')

    def test_saved_evaluated_observation_remains_authoritative(self):
        saved = entry(status='settled', observed=[dict(match_id='1', ht='0-0', ft='1-0', value='2nd')])
        result = self.result(raw('PST'), saved)
        self.assertEqual(result['label'], 'Évalué')
        self.assertEqual(result['blocking'], [])

    def test_inconsistent_saved_category_or_chronology_is_not_called_evaluated(self):
        for changes in (dict(value='1st'), dict(value='2nd')):
            saved = entry(status='settled', observed=[dict(match_id='1', ht='0-0', ft='1-0', **changes)])
            if changes['value'] == '2nd':
                saved['checked_at'] = '2026-10-24T11:00:00Z'
            self.assertNotEqual(reasons.describe_entry(saved, now=NOW)['label'], 'Évalué')

    def test_provider_result_before_future_kickoff_is_not_accepted(self):
        observation = raw(); observation['fixture']['date'] = '2026-10-28T12:00:00Z'
        self.assertEqual(self.result(observation)['label'], 'Résultat à vérifier')

    def test_import_quarantine_is_visible_without_rewriting_saved_verdict(self):
        from fixture_import_policy import QUALITY_KEY, VERSION
        saved = entry(status='settled', observed=[dict(match_id='1', ht='0-0', ft='1-0', value='2nd')])
        before = deepcopy(saved)
        observation = raw()
        observation[QUALITY_KEY] = dict(version=VERSION, quarantined=True, reason='incomplete_final')
        result = self.result(observation, saved)
        self.assertEqual(result['label'], 'Correction en quarantaine')
        self.assertIn('verdicts déjà enregistrés', result['blocking'][0]['reason'])
        self.assertEqual(saved, before)

    def test_other_match_issue_not_attributed_to_single_legacy_row(self):
        saved = entry(issues=['Match 22 reporté ou interrompu.'])
        result = reasons.describe_entry(saved, now=NOW)
        self.assertEqual(result['label'], 'Résultat non reçu')
        self.assertNotIn('22', result['summary'])

    def test_old_events_use_conservative_subtype_without_current_raw(self):
        result = reasons.describe_entry(entry(issues=['Match 1 reporté ou interrompu.']), now=NOW)
        self.assertEqual(result['label'], 'Reporté ou interrompu')
        self.assertIsNone(result['matches'][0]['raw_status'])

    def test_archive_single_fixture_supported(self):
        value = dict(status='unresolved', fixture=fixture(), ht=None, ft=None, issues=[])
        self.assertEqual(self.result(raw('PST'), value)['label'], 'Reporté')
        value.update(status='settled', ht='0-0', ft='1-0')
        self.assertEqual(self.result(raw('PST'), value)['label'], 'Évalué')

    def test_boolean_and_negative_scores_are_not_accepted(self):
        for bad in (True, -1):
            self.assertEqual(self.result(raw(ht=dict(home=bad, away=0)))['label'], 'Score à la pause manquant')

    def test_annotation_batches_read_only_ids_and_keeps_input(self):
        import api_football as api
        saved = entry(); duplicate = entry(id='other'); original = deepcopy(saved)
        with tempfile.TemporaryDirectory() as folder, patch.object(api, 'load_fixtures', return_value=[raw('PST')]) as loader:
            result = reasons.annotate_entries([saved, duplicate], root=folder, now=NOW)
            loader.assert_called_once_with(Path(folder)/'fixtures.json', ids=['1'])
        self.assertEqual(result[0]['result_explanation']['label'], 'Reporté')
        self.assertEqual(saved, original)

    def test_annotation_respects_default_store_path_and_handles_unavailable(self):
        import api_football as api
        with patch.object(api, 'STORE_PATH', Path('/tmp/isolated-status-fixtures.json')):
            with patch.object(api, 'load_fixtures', side_effect=api.ApiFootballError('Unavailable')) as loader:
                result = reasons.annotate_entries([entry()], now=NOW)
                loader.assert_called_once_with(api.STORE_PATH, ids=['1'])
        self.assertTrue(result[0]['result_explanation']['local_unavailable'])

    def test_empty_skips_read_and_settled_keeps_observation_after_local_quality_check(self):
        with patch('api_football.load_fixtures', return_value=[]) as loader:
            self.assertEqual(reasons.annotate_entries([], now=NOW), [])
            loader.assert_not_called()
            value = entry(status='settled', observed=[dict(match_id='1', ht='0-0', ft='1-0')])
            self.assertEqual(reasons.annotate_entries([value], now=NOW)[0]['result_explanation']['blocking'], [])
            self.assertEqual(loader.call_count, 1)

    def test_partial_group_keeps_saved_result_and_only_blocker_explained(self):
        second = dict(fixture(), match_id='2', home_id='30', away_id='40', home='C', away='D')
        saved = entry(fixtures=[fixture(), second], observed=[dict(match_id='1', ht='0-0', ft='1-0')])
        second_raw = raw('WO'); second_raw['fixture']['id'] = 2
        second_raw['teams'] = dict(home=dict(id=30), away=dict(id=40))
        result = reasons.describe_entry(saved, raw_by_id={'2':second_raw}, now=NOW)
        self.assertEqual([row['match_id'] for row in result['blocking']], ['2'])
        self.assertEqual(result['label'], 'Forfait')


if __name__ == '__main__':
    unittest.main()
