"""Verdict filters retain exact trends, phase identity and complete exports."""
from copy import deepcopy
import unittest

import club_trends_panel as panel
import test_club_trends_comparison_panel as comparison_fixture


def role_profile(profile_id, *, home=None, away=None):
    return {'profile_id': profile_id,
            'home_percentages': home or {'1st': None, 'Égal': None, '2nd': None},
            'away_percentages': away or {'1st': None, 'Égal': None, '2nd': None}}


class VerdictFilterTests(unittest.TestCase):
    def test_options_distinguish_global_role_phase_and_real_final_result(self):
        self.assertEqual(panel._verdict_options(), ('Tendance dominante', 'Tendance récente'))
        self.assertEqual(panel._verdict_options(fixtures=True),
                         ('Tendance domicile', 'Tendance extérieur', 'Verdict final'))
        compared = ('Tendance domicile avant', 'Tendance domicile après',
                    'Tendance extérieur avant', 'Tendance extérieur après')
        self.assertEqual(panel._verdict_options(comparison=True), compared)
        self.assertEqual(panel._verdict_options(fixtures=True, comparison=True),
                         compared + ('Verdict final',))

    def test_fixture_filters_are_cumulative_keep_ties_and_use_exact_phase(self):
        before = role_profile('99:39', home={'1st': 0, 'Égal': 0, '2nd': 100})
        after = role_profile('99:39', home={'1st': 50, 'Égal': 0, '2nd': 50})
        opponent = role_profile('98:39', away={'1st': 10, 'Égal': 70, '2nd': 20})
        correct = {'match_id': 'yes', 'home_profile': before, 'home_after_profile': after,
                   'away_profile': opponent, 'away_after_profile': opponent, 'final_verdict': '1st'}
        rows = [correct, {**correct, 'match_id': 'wrong-final', 'final_verdict': '2nd'},
                {**correct, 'match_id': 'missing-before', 'home_profile': None},
                {**correct, 'match_id': 'missing-after', 'home_after_profile': None}]
        original = deepcopy(rows)
        comparison = {'after': {'rows': [after, opponent]}}
        criteria = (('Tendance domicile avant', '2nd'), ('Tendance domicile après', '1st'),
                    ('Tendance extérieur avant', 'Égal'), ('Verdict final', '1st'))
        selected = panel._filter_verdicts(rows, criteria, fixtures=True, comparison=comparison)
        self.assertEqual([row['match_id'] for row in selected], ['yes'])
        self.assertIs(selected[0], correct)
        self.assertEqual(rows, original)
        self.assertEqual(panel._filter_verdicts(rows, (('Tendance domicile avant', '1st'),),
                         fixtures=True, comparison=comparison), [])

    def test_missing_and_zero_history_are_not_equal_and_rounding_does_not_make_ties(self):
        missing = role_profile('missing')
        zero = role_profile('zero', home={'1st': 0, 'Égal': 0, '2nd': 0})
        close = role_profile('close', home={'1st': 33.349, 'Égal': 33.300, '2nd': 33.351})
        equal = role_profile('equal', home={'1st': 20, 'Égal': 60, '2nd': 20})
        rows = [missing, zero, close, equal]
        comparison = {'after': {'rows': rows}}
        self.assertEqual([row['profile_id'] for row in panel._filter_verdicts(rows,
                         (('Tendance domicile avant', 'Égal'),), comparison=comparison)], ['equal'])
        self.assertEqual([row['profile_id'] for row in panel._filter_verdicts(rows,
                         (('Tendance domicile avant', '2nd'),), comparison=comparison)], ['close'])
        self.assertEqual(panel._filter_verdicts(rows, (('Tendance domicile avant', '1st'),),
                         comparison=comparison), [])
        self.assertIs(panel._filter_verdicts(rows, (), comparison=comparison), rows)

    def test_global_recent_and_dominant_filters_do_not_conflate_their_denominators(self):
        rows = [{'profile_id': 'tie', 'leaders': ['1st', '2nd'],
                 'recent_counts': {'1st': 0, 'Égal': 2, '2nd': 0}},
                {'profile_id': 'other', 'leaders': ['Égal'],
                 'recent_counts': {'1st': 2, 'Égal': 0, '2nd': 2}},
                {'profile_id': 'empty', 'leaders': [],
                 'recent_counts': {'1st': 0, 'Égal': 0, '2nd': 0}}]
        selected = panel._filter_verdicts(rows,
            (('Tendance dominante', '1st'), ('Tendance récente', 'Égal')))
        self.assertEqual([row['profile_id'] for row in selected], ['tie'])
        selected = panel._filter_verdicts(rows, (('Tendance récente', '2nd'),))
        self.assertEqual([row['profile_id'] for row in selected], ['other'])

    def test_all_club_after_filters_join_by_profile_identity_and_reject_wrong_view(self):
        before = [role_profile('99:39', home={'1st': 100, 'Égal': 0, '2nd': 0}),
                  role_profile('99:40', home={'1st': 100, 'Égal': 0, '2nd': 0})]
        after = [role_profile('99:40', home={'1st': 0, 'Égal': 100, '2nd': 0}),
                 role_profile('99:39', home={'1st': 0, 'Égal': 0, '2nd': 100})]
        chosen = panel._filter_verdicts(before, (('Tendance domicile après', '2nd'),),
                                       comparison={'after': {'rows': after}})
        self.assertEqual([row['profile_id'] for row in chosen], ['99:39'])
        with self.assertRaises(ValueError):
            panel._filter_verdicts(before, (('Verdict final', '1st'),))


class VerdictFilterPanelTests(unittest.TestCase):
    # Share only the data fixture/helpers, without inheriting its test methods.
    setUp = comparison_fixture.ClubComparisonPanelTests.setUp
    mock = comparison_fixture.ClubComparisonPanelTests.mock
    day_view = comparison_fixture.ClubComparisonPanelTests.day_view
    table = comparison_fixture.ClubComparisonPanelTests.table
    export = comparison_fixture.ClubComparisonPanelTests.export
    assert_read_only = comparison_fixture.ClubComparisonPanelTests.assert_read_only

    def choose(self, prefix, columns, values):
        self.at.multiselect(key=prefix + '_columns').set_value(columns).run()
        self.assertFalse(self.at.exception)
        for column, value in values.items():
            self.at.button_group(key=prefix + '_' + column).set_value(value).run()
            self.assertFalse(self.at.exception)

    def test_calendar_and_filters_reset_pagination_and_export_the_selected_criteria(self):
        self.day_view()
        self.at.selectbox(key='club_trends_day_limit').set_value(10).run()
        self.at.selectbox(key='club_trends_day_page').set_value(2).run()
        self.assertEqual(len(self.table()), 6)
        prefix = 'club_trends_verdict_day_compare'
        columns = ['Tendance domicile avant', 'Tendance domicile après',
                   'Tendance extérieur avant', 'Verdict final']
        self.choose(prefix, columns, {'Tendance domicile avant': '2nd',
            'Tendance domicile après': '1st', 'Tendance extérieur avant': '2nd', 'Verdict final': '1st'})
        self.assertEqual(list(self.table()['ID du match API']), ['1001'])
        self.assertEqual(self.at.selectbox(key='club_trends_day_page').value, 1)
        _, exported = self.export()
        self.assertEqual(len(exported), 1)
        metadata = exported[0]['Filtres de verdicts']
        for column, value in (('Tendance domicile avant', '2nd'), ('Tendance domicile après', '1st'),
                              ('Tendance extérieur avant', '2nd'), ('Verdict final', '1st')):
            self.assertIn(f'{column} = {value}', metadata)
        self.assertEqual(metadata.count(' ET '), 3)
        self.assertNotEqual(exported[0]['Filtres et sélection appliqués à'], 'Avant uniquement')
        self.at.button_group(key=prefix + '_Verdict final').set_value('2nd').run()
        self.assertFalse(self.at.exception)
        self.assertTrue(any('Aucun match' in item.value for item in self.at.info))
        self.assertEqual(self.at.multiselect(key=prefix + '_columns').value, columns)
        self.at.button_group(key=prefix + '_Verdict final').set_value('Tous').run()
        self.assertFalse(self.at.exception)
        _, exported = self.export()
        self.assertEqual(len(exported), 12)
        self.assertEqual(self.build.call_count, 1)
        self.assert_read_only()

    def test_club_after_filter_remains_active_when_hidden_and_retains_its_choice(self):
        prefix = 'club_trends_verdict_clubs_compare'
        column = 'Tendance domicile après'
        self.choose(prefix, [column], {column: '1st'})
        self.assertEqual(list(self.table(False)['ID club API']), ['99'])
        self.at.toggle(key='club_trends_compare_show_after').set_value(False).run()
        self.assertFalse(self.at.exception)
        self.assertNotIn(column, self.table(False).columns)
        self.assertEqual(list(self.table(False)['ID club API']), ['99'])
        self.assertTrue(any(column + ' = 1st' in item.value for item in self.at.caption))
        _, exported = self.export()
        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0][column], '1st / 2nd')
        self.assertEqual(exported[0]['Filtres de verdicts'], column + ' = 1st')
        self.assertNotEqual(exported[0]['Filtres et sélection appliqués à'], 'Avant uniquement')
        self.at.multiselect(key=prefix + '_columns').set_value([]).run()
        self.assertFalse(self.at.exception)
        self.assertEqual(len(self.table(False)), 4)
        self.at.multiselect(key=prefix + '_columns').set_value([column]).run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.at.button_group(key=prefix + '_' + column).value, '1st')
        self.assertEqual(list(self.table(False)['ID club API']), ['99'])
        self.day_view()
        self.assertEqual(len(self.table()), 16)
        self.at.button_group(key='club_trends_view').set_value('Tous les clubs').run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.at.button_group(key=prefix + '_' + column).value, '1st')
        self.assertEqual(list(self.table(False)['ID club API']), ['99'])
        self.assertEqual(self.build.call_count, 1)
        self.assert_read_only()


if __name__ == '__main__':
    unittest.main()
