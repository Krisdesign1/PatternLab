"""Identity regressions use synthetic rows only, without data/store access."""
from copy import deepcopy
import unittest

from club_identity import ClubProfiles, identifier
from match_predictor import estimate, goals, profile, VERSION
from test_match_predictor import sample


def identified():
    return [dict(row, provider='api-football', home='Shared name', away='Visitor',
                 home_id='11' if index < 32 else '12', away_id='20')
            for index, row in enumerate(sample())]


class ClubIdentityTests(unittest.TestCase):
    def test_confirmed_equal_display_names_survive_fixture_and_feature_validation(self):
        import api_match_workspace
        import scenario_features
        import prospective_models
        from test_api_match_workspace import raw
        value = raw()
        value['teams']['home']['name'] = value['teams']['away']['name'] = 'Shared display'
        fixture = api_match_workspace.fixture_from_raw(value)
        fixture['provider'] = 'api-football'
        metadata = scenario_features._metadata(fixture, fixture['league'], 'api-football')
        self.assertNotEqual(metadata['home_id'], metadata['away_id'])
        second = dict(fixture, match_id='9999', timestamp='2026-10-25T13:00:00')
        normalized,_,_ = prospective_models._fixtures([fixture,second],'API-Football')
        self.assertEqual(len(normalized),2)

    def test_equal_names_never_combine_known_ids_and_ambiguous_name_has_no_profile(self):
        rows = identified()
        first = profile(rows, 'Shared name', 'home', team_id=11, league='A', provider='API-Football')
        second = profile(rows, 'Shared name', 'home', team_id='12', league='A', provider='api-football')
        self.assertEqual(first['matches'], 32)
        self.assertEqual(second['matches'], 32)
        self.assertEqual(profile(rows, 'Shared name', 'home', league='A')['matches'], 0)
        result = estimate(rows, 'A', 'Shared name', 'Visitor', '2026-02-01',
                          home_id=11, away_id=20, provider='api-football')
        self.assertEqual(result['home_profile'], first)
        self.assertIn('club-ids-v2', VERSION)

    def test_same_id_keeps_history_through_rename_and_roles_remain_separate(self):
        rows = [dict(row, home_id='11', home='Old club', away_id='20', away='Visitor',
                     provider='api-football') for row in sample()]
        for row in rows[32:]:
            row['home'] = 'Recent club'
        result = estimate(rows, 'A', 'Brand new club name', 'Visitor', '2026-02-01',
                          home_id=11, away_id=20, provider='api-football')
        self.assertTrue(result['personalized'])
        self.assertEqual(result['home_profile']['matches'], 64)
        self.assertEqual(profile(rows, 'Brand new club name', 'away', team_id=11, league='A')['matches'], 0)
        # Labels are not used to reject two explicitly distinct club identities.
        self.assertTrue(estimate(rows, 'A', 'Same display', 'Same display', '2026-02-01',
                                 home_id=11, away_id=20)['personalized'])

    def test_sources_competitions_and_numeric_id_spellings_are_scoped(self):
        rows = [dict(row, home_id='0011', away_id='20', provider='api-football') for row in sample()]
        other = [dict(row, provider='other-source', ht='2-0', ft='2-0', value='1st') for row in rows]
        foreign = [dict(row, league='B', match_id=str(1000+i)) for i, row in enumerate(rows)]
        expected = estimate(rows, 'A', 'H0', 'A0', '2026-02-01', home_id=11, away_id=20,
                            provider='api-football')
        actual = estimate(rows+other+foreign, 'A', 'H0', 'A0', '2026-02-01', home_id='11',
                          away_id='020', provider='API-Football')
        self.assertEqual(actual, expected)
        with self.assertRaisesRegex(ValueError, 'source'):
            estimate(rows+other, 'A', 'H0', 'A0', '2026-02-01')
        with self.assertRaisesRegex(ValueError, 'compétition'):
            profile(rows+foreign, 'H0', 'home', team_id=11)

    def test_legacy_exact_names_work_but_ambiguous_legacy_rows_do_not_join_ids(self):
        legacy = [dict(row, home='Shared name', away='Visitor') for row in sample()]
        self.assertEqual(profile(legacy, 'Shared name', 'home', league='A')['matches'], 64)
        # An older wholly ID-less source can still be used by an exact label.
        self.assertTrue(estimate(legacy, 'A', 'Shared name', 'Visitor', '2026-02-01',
                                 home_id=11, away_id=20, provider='api-football')['personalized'])
        known = identified()[:2]
        known[1]['home_id'] = '12'
        known[1]['match_id'] = '999'
        rows = legacy+known
        self.assertEqual(profile(rows, 'Shared name', 'home', team_id=11, league='A')['matches'], 1)
        self.assertEqual(profile(rows, 'Shared name', 'home', team_id=12, league='A')['matches'], 1)
        self.assertEqual(profile(rows, 'Shared name', 'home', league='A')['matches'], 0)

    def test_unambiguous_legacy_aliases_join_only_the_resolved_identifier(self):
        rows = [dict(row, home='Old name', away='Visitor', provider='api-football') for row in sample()]
        for row in rows[16:]:
            row.update(home_id='11', away_id='20')
        rows[-1]['home'] = 'Renamed'
        result = estimate(rows, 'A', 'Renamed', 'Visitor', '2026-02-01', home_id=11, away_id=20)
        self.assertEqual(result['home_profile']['matches'], 64)
        self.assertEqual(profile(rows, 'Old name', 'home', team_id=12, league='A')['matches'], 0)

    def test_future_alias_collision_or_provider_cannot_change_earlier_estimate(self):
        rows = [dict(row, home='Shared name', away='Visitor', provider='api-football') for row in sample()]
        cutoff = '2026-01-12'
        result = estimate(rows, 'A', 'Shared name', 'Visitor', cutoff, home_id=11, away_id=20)
        later = dict(rows[-1], match_id='1001', timestamp='2026-01-12T00:00:00', home_id='12')
        future = dict(later, match_id='1002', timestamp='2027-01-01T00:00:00', provider='other-source')
        self.assertEqual(estimate(rows+[later, future], 'A', 'Shared name', 'Visitor', cutoff,
                                  home_id=11, away_id=20), result)

    def test_incremental_identity_resolution_is_causal_and_never_mutates_rows(self):
        rows = identified()
        saved = deepcopy(rows)
        index = ClubProfiles(provider='api-football')
        for row in rows[:32]:
            index.add(row, goals(row))
        self.assertEqual(index.profile('Shared name', 'home', league='A')['matches'], 32)
        for row in rows[32:]:
            index.add(row, goals(row))
        self.assertEqual(index.profile('Shared name', 'home', league='A')['matches'], 0)
        self.assertEqual(index.profile('New name', 'home', team_id=11, league='A')['matches'], 32)
        self.assertEqual(rows, saved)

    def test_invalid_known_identifier_never_silently_becomes_a_name(self):
        for value in (True, False, 0, -1, 'eleven', 1.5, '１１'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                identifier(value)
        with self.assertRaises(ValueError):
            estimate(sample(), 'A', 'H0', 'A0', '2026-02-01', home_id=11, away_id=11)


if __name__ == '__main__':
    unittest.main()
