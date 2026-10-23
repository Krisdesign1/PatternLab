"""First saved, pre-match marginals must survive overlapping group selection."""
from copy import deepcopy
import unittest

import probability_reliability as reliability


def fixtures():
    return [dict(match_id=str(index + 1), league='39', home_id=str(index * 2 + 10),
                 away_id=str(index * 2 + 11), home=f'Home {index}', away=f'Away {index}',
                 timestamp=f'2026-10-20T{12 + index:02d}:00:00', provider='api-football')
            for index in range(2)]


def entry(*, identity='first', created='2026-10-20T10:00:00Z', status='settled',
          probabilities=None, version='half-goals-v1'):
    probabilities = probabilities or [dict(zip(('1st', 'Égal', '2nd'), (.2, .3, .5))),
                                      dict(zip(('1st', 'Égal', '2nd'), (.6, .3, .1)))]
    chosen = fixtures()
    return dict(id=identity, source='API-Football', created_at=created, fixtures=chosen, status=status,
                checked_at='2026-10-20T16:00:00Z', observed=[
                    dict(match_id='1', ht='0-0', ft='1-0', value='2nd'),
                    dict(match_id='2', ht='2-0', ft='2-0', value='1st')],
                methods={'clubs': dict(version=version, source='API-Football', league='39',
                    training_before='2026-10-20', distribution=dict(schema=reliability.SCHEMA,
                        type='independent', size=2, distributions=deepcopy(probabilities)))})


def mixed(original):
    result = deepcopy(original)
    result['fixtures'][1]['league'] = '61'
    method = result['methods']['clubs']
    components = []
    for index, fixture in enumerate(result['fixtures']):
        component = deepcopy(method)
        component['league'] = fixture['league']
        component['distribution']['size'] = 1
        component['distribution']['distributions'] = [component['distribution']['distributions'][index]]
        components.append(dict(indices=[index], league=fixture['league'], method=component))
    result['methods']['clubs'] = dict(version='prospective-mixed-blocks-v1', source='API-Football',
        league='mixed', provider='api-football', assumes_block_independence=True,
        training_before='2026-10-20', fixtures=deepcopy(result['fixtures']),
        distribution=dict(schema=reliability.SCHEMA, type='block_product', size=2, components=components))
    return result


class ProbabilityReliabilityTests(unittest.TestCase):
    def result(self, entries):
        return reliability.build(entries)['versions'][0]

    def test_each_category_uses_all_matches_and_one_versus_rest(self):
        result = self.result([entry()])
        for label in ('1st', 'Égal', '2nd'):
            self.assertEqual(sum(row['n'] for row in result['results'][label]['bins']), 2)
        bins = result['results']['2nd']['bins']
        self.assertEqual(bins[5]['n'], 1)
        self.assertEqual(bins[5]['hits'], 1)
        self.assertEqual(bins[5]['announced'], .5)
        self.assertEqual(bins[5]['observed_rate'], 1)
        self.assertEqual(bins[5]['gap_points'], 50)
        self.assertIsNone(bins[0]['observed_rate'])
        self.assertEqual(result['results']['Égal']['most_likely_n'], 0)

    def test_one_hundred_overlapping_groups_do_not_multiply_matches(self):
        entries = [entry(identity=f'group-{index}') for index in range(100)]
        report = reliability.build(entries)
        self.assertEqual(report['versions'][0]['evaluated'], 2)
        self.assertEqual(report['duplicates_removed'], 198)

    def test_earliest_pending_forecast_is_never_replaced_by_later_settled(self):
        first = entry(status='pending')
        first['observed'] = []
        later = entry(identity='later', created='2026-10-20T11:00:00Z')
        result = self.result([later, first])
        self.assertEqual(result['evaluated'], 0)
        self.assertEqual(result['pending'], 2)
        self.assertEqual({row['entry_id'] for row in result['records']}, {'first'})

    def test_first_forecast_is_chosen_per_match_using_creation_time_not_input_order(self):
        first = entry(identity='first', created='2026-10-20T09:00:00+00:00')
        second = entry(identity='second', created='2026-10-20T10:00:00Z')
        second['fixtures'][0]['match_id'] = '3'
        second['observed'][0]['match_id'] = '3'
        result = self.result([second, first])
        self.assertEqual(result['evaluated'], 3)
        self.assertEqual({row['match_id']: row['entry_id'] for row in result['records']},
                         {'1': 'first', '2': 'first', '3': 'second'})

    def test_tied_creation_time_uses_stable_entry_id_even_if_later_has_scores(self):
        first = entry(identity='a', status='pending')
        first['observed'] = []
        later = entry(identity='z')
        self.assertEqual(self.result([later, first])['pending'], 2)

    def test_pre_kickoff_guard_is_per_match_and_accepts_offset_equivalence(self):
        report = reliability.build([entry(created='2026-10-20T13:00:00+01:00')])
        self.assertEqual(report['snapshot_exclusions']['not_before_kickoff'], 1)
        self.assertEqual(report['versions'][0]['forecasts'], 1)
        self.assertEqual(report['versions'][0]['records'][0]['match_id'], '2')
        report = reliability.build([entry(created='2026-10-20T14:00:00Z')])
        self.assertEqual(report['versions'], [])

    def test_mono_and_mixed_use_real_component_versions_not_container_version(self):
        original = entry()
        other = mixed(entry(identity='mixed', created='2026-10-20T11:00:00Z'))
        report = reliability.build([other, original])
        self.assertEqual(len(report['versions']), 1)
        self.assertEqual(report['versions'][0]['key'], 'half-goals-v1')
        self.assertEqual(report['versions'][0]['evaluated'], 2)
        self.assertEqual(report['duplicates_removed'], 2)

    def test_mixed_component_versions_remain_separate(self):
        other = mixed(entry())
        other['methods']['clubs']['distribution']['components'][1]['method']['version'] = 'half-goals-v2'
        versions = reliability.build([other])['versions']
        self.assertEqual([row['key'] for row in versions], ['half-goals-v1', 'half-goals-v2'])
        self.assertEqual([row['evaluated'] for row in versions], [1, 1])

    def test_different_generations_are_not_pooled(self):
        first = entry()
        second = entry(identity='other')
        second['methods']['clubs']['generation'] = 'new-artifact'
        self.assertEqual(len(reliability.build([first, second])['versions']), 2)

    def test_corrected_context_and_old_distributions_are_excluded(self):
        for kind in ('corrected', 'context_mixture', 'frequency'):
            item = entry()
            item['methods']['clubs']['distribution']['type'] = kind
            self.assertEqual(reliability.build([item])['versions'], [])
        item = entry()
        item['methods']['clubs']['distribution']['schema'] = 'unversioned'
        self.assertEqual(reliability.build([item])['versions'], [])

    def test_invalid_probabilities_and_foreign_provenance_never_generate_data(self):
        for mutator in (
            lambda row: row.update(source='football-data.org'),
            lambda row: row['fixtures'][0].update(provider='football-data.org'),
            lambda row: row['fixtures'][0].update(match_id=True),
            lambda row: row['methods']['clubs'].update(league='61'),
            lambda row: row['methods']['clubs']['distribution']['distributions'][0].update({'1st': float('nan')}),
            lambda row: row['methods']['clubs']['distribution']['distributions'][0].update({'1st': .9}),
            lambda row: row['methods']['clubs'].update(training_before='2026-10-21'),
        ):
            item = entry()
            mutator(item)
            self.assertEqual(reliability.build([item])['versions'], [])

    def test_partial_results_in_pending_group_are_used_individually(self):
        item = entry(status='pending')
        item['observed'].pop()
        result = self.result([item])
        self.assertEqual(result['evaluated'], 1)
        self.assertEqual(result['pending'], 1)

    def test_unresolved_or_void_result_is_excluded_without_counting_as_failure(self):
        item = entry(status='unresolved')
        item['observed'].pop()
        result = self.result([item])
        self.assertEqual(result['evaluated'], 1)
        self.assertEqual(result['excluded'], 1)
        item['status'] = 'void'
        result = self.result([item])
        self.assertEqual(result['evaluated'], 0)
        self.assertEqual(result['excluded'], 2)

    def test_current_score_corrections_are_validated_against_result_label(self):
        item = entry()
        item['result_history'] = [{'observed': deepcopy(item['observed'])}]
        item['observed'][0].update(ht='2-0', ft='2-0', value='1st')
        self.assertEqual(self.result([item])['records'][0]['actual'], '1st')
        item['observed'][0]['value'] = '2nd'
        result = self.result([item])
        self.assertEqual(result['excluded'], 1)
        self.assertEqual(result['evaluated'], 1)

    def test_contradictory_scores_or_premature_observations_are_excluded(self):
        for mutation in ('contradictory', 'score', 'premature'):
            item = entry()
            if mutation == 'contradictory':
                item['observed'].append(dict(match_id='1', ht='2-0', ft='2-0', value='1st'))
            elif mutation == 'score':
                item['observed'][0].update(ht='2-0', ft='1-0', value='1st')
            else:
                item['checked_at'] = '2026-10-20T11:59:59Z'
            result = self.result([item])
            self.assertGreater(result['excluded'], 0)

    def test_bin_endpoints_zero_and_one_are_retained(self):
        p = [{'1st': 1, 'Égal': 0, '2nd': 0}, {'1st': 0, 'Égal': 1, '2nd': 0}]
        result = self.result([entry(probabilities=p)])
        self.assertEqual(result['results']['1st']['bins'][9]['n'], 1)
        self.assertEqual(result['results']['1st']['bins'][0]['n'], 1)

    def test_selection_metadata_ranking_and_group_success_cannot_filter_population(self):
        item = entry()
        item.update(actual=['Égal', 'Égal'], evaluations={'clubs': {'top3': False}},
                    metadata={'selection': {'requested_sequence': ['Égal', 'Égal']}})
        item['methods']['clubs']['ranking'] = []
        original = deepcopy(item)
        result = self.result([item])
        self.assertEqual(result['evaluated'], 2)
        self.assertEqual(item, original)

    def test_empty_or_no_clubs_history_is_explicitly_empty(self):
        self.assertEqual(reliability.build([])['versions'], [])
        item = entry()
        item['methods'].pop('clubs')
        report = reliability.build([item])
        self.assertEqual(report['versions'], [])
        self.assertEqual(report['snapshot_exclusions']['no_saved_clubs_method'], 1)


if __name__ == '__main__':
    unittest.main()
