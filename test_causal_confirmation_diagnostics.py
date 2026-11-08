from copy import deepcopy
import json
from math import log
import unittest
from unittest.mock import patch

import causal_candidate
import causal_confirmation_diagnostics as diagnostics


class ConfirmationDiagnosticsTests(unittest.TestCase):
    @staticmethod
    def distribution(first=.6, equal=.3, second=.1):
        return {'1st': first, 'Égal': equal, '2nd': second}

    def row(self, identity='1', *, actual='1st', status='settled', admissible=True,
            timing='local_commit_ack', league='10', timestamp='2026-10-31T12:00:00', methods=None):
        probabilities = self.distribution()
        return dict(forecast_id='forecast-'+identity,
                    fixture=dict(match_id=identity, league=league, timestamp=timestamp),
                    status=status, admissible=admissible, timing_quality=timing, actual=actual,
                    methods=deepcopy(methods if methods is not None else
                                     {method: probabilities for method in diagnostics.METHODS}))

    @staticmethod
    def protocol(rows=None):
        return dict(id='protocol-one', confirmation_start='2026-10-31T00:00:00+00:00',
                    confirmation_end='2026-11-01T00:00:00+00:00', rows=[] if rows is None else rows,
                    metrics={'untrusted': 'ignored'}, evaluated=99999)

    def build(self, rows):
        return diagnostics.build(self.protocol(rows))

    def test_hand_calculated_paired_scores_and_directions(self):
        first = self.row(methods=dict(candidate=self.distribution(.6, .3, .1),
            clubs=self.distribution(.5, .25, .25), frequency=self.distribution(1/3, 1/3, 1/3)))
        second = self.row('2', actual='2nd', methods=dict(candidate=self.distribution(.2, .3, .5),
            clubs=self.distribution(.2, .4, .4), frequency=self.distribution(.25, .25, .5)))
        result = self.build([first, second])
        self.assertEqual(result['counts']['evaluated'], 2)
        candidate = result['metrics']['candidate']
        self.assertAlmostEqual(candidate['logloss'], (-log(.6)-log(.5))/2)
        self.assertAlmostEqual(candidate['brier'], (.26+.38)/2)
        self.assertEqual(candidate['accuracy'], 1)
        self.assertAlmostEqual(result['metrics']['clubs']['brier'], (.375+.56)/2)
        self.assertEqual(result['metrics']['clubs']['accuracy'], .5)
        delta = result['paired_deltas']['clubs']
        self.assertEqual(delta['count'], 2)
        self.assertAlmostEqual(delta['logloss'], log(2/3)/2)
        self.assertAlmostEqual(delta['brier'], -.1475)
        self.assertEqual(delta['accuracy'], .5)
        for reference in diagnostics.REFERENCES:
            for score in diagnostics.SCORES:
                self.assertAlmostEqual(result['paired_deltas'][reference][score],
                    sum(row['deltas'][reference][score] for row in result['paired_rows'])/2)
                self.assertAlmostEqual(result['paired_deltas'][reference][score],
                    candidate[score]-result['metrics'][reference][score])

    def test_metric_conventions_zero_log_floor_brier_sum_and_favorite_tie(self):
        zero = self.distribution(0, .5, .5)
        result = self.build([self.row(methods={method: zero for method in diagnostics.METHODS})])
        metrics = result['metrics']['candidate']
        self.assertAlmostEqual(metrics['logloss'], -log(causal_candidate.PROBABILITY_FLOOR))
        self.assertEqual(metrics['brier'], 1.5)
        self.assertEqual(metrics['accuracy'], 0)
        tie = self.distribution(.5, .5, 0)
        tied = self.build([self.row(methods={method: tie for method in diagnostics.METHODS})])
        self.assertEqual(tied['metrics']['candidate']['accuracy'], 1)
        confidence = tied['calibration']['candidate']['favorite_confidence'][5]
        self.assertEqual(confidence['observed_frequency'], 1)
        self.assertEqual(confidence['gap'], .5)

    def test_fixed_class_bins_include_zero_one_and_empty_bins(self):
        methods = {method: self.distribution(1, 0, 0) for method in diagnostics.METHODS}
        result = self.build([self.row(methods=methods)])
        for method in diagnostics.METHODS:
            classes = result['calibration'][method]['classes']
            self.assertEqual(tuple(classes), diagnostics.CLASS_ORDER)
            for bins in list(classes.values()) + [result['calibration'][method]['favorite_confidence']]:
                self.assertEqual(len(bins), 10)
                self.assertEqual(sum(item['count'] for item in bins), 1)
                for index, item in enumerate(bins):
                    self.assertEqual((item['lower'], item['upper']), (index/10, (index+1)/10))
                    self.assertEqual(item['upper_inclusive'], index == 9)
                    if item['count'] == 0:
                        self.assertIsNone(item['mean_probability'])
                        self.assertIsNone(item['observed_frequency'])
                        self.assertIsNone(item['gap'])
            self.assertEqual(classes['1st'][9]['mean_probability'], 1)
            self.assertEqual(classes['1st'][9]['observed_frequency'], 1)
            self.assertEqual(classes['Égal'][0]['mean_probability'], 0)
            self.assertEqual(classes['Égal'][0]['observed_frequency'], 0)

    def test_every_fixed_probability_boundary_belongs_to_bin_starting_there(self):
        rows = []
        for index in range(11):
            first = index/10
            methods = {method: self.distribution(first, 1-first, 0) for method in diagnostics.METHODS}
            rows.append(self.row(str(index), methods=methods))
        bins = self.build(rows)['calibration']['candidate']['classes']['1st']
        self.assertEqual([item['count'] for item in bins], [1]*9+[2])
        self.assertEqual(bins[3]['mean_probability'], .3)
        self.assertAlmostEqual(bins[9]['mean_probability'], .95)

    def test_gaps_are_observed_minus_predicted_for_every_class_and_favorite(self):
        result = self.build([self.row(), self.row('2', actual='2nd')])
        classes = result['calibration']['candidate']['classes']
        self.assertEqual(classes['1st'][6]['count'], 2)
        self.assertAlmostEqual(classes['1st'][6]['gap'], .5-.6)
        self.assertAlmostEqual(classes['Égal'][3]['gap'], -.3)
        self.assertAlmostEqual(classes['2nd'][1]['gap'], .5-.1)
        favorite = result['calibration']['candidate']['favorite_confidence'][6]
        self.assertEqual(favorite['observed_frequency'], .5)
        self.assertAlmostEqual(favorite['gap'], -.1)

    def test_exclusion_counts_partition_population_and_ignore_sporting_labels_without_proof(self):
        rows = [self.row(),
            self.row('2', status='late_capture', admissible=False),
            self.row('3', status='missing_ack', admissible=False, timing='missing_commit_ack'),
            self.row('4', status='unattested_capture', admissible=False, timing='synthetic_unattested'),
            self.row('5', timing='synthetic_unattested'),
            self.row('6', timing='missing_commit_ack'),
            self.row('7', admissible=False),
            self.row('8', status='pending', actual=None),
            self.row('9', status='unresolved', actual=None),
            self.row('10', status='void', actual=None)]
        result = self.build(rows)
        counts = result['counts']
        self.assertEqual((counts['forecasts'], counts['evaluated'], counts['excluded']), (10, 1, 9))
        self.assertEqual(sum(counts['statuses'].values()), 10)
        self.assertEqual(sum(counts['exclusion_reasons'].values()), 9)
        self.assertEqual(counts['exclusion_reasons'], dict(pending=1, unresolved=1, void=1,
            late_capture=1, missing_ack=2, unattested_capture=2, inadmissible_capture=1))
        self.assertTrue(all(value['count'] == 1 for value in result['metrics'].values()))
        self.assertEqual(len(result['paired_rows']), 1)

    def test_empty_population_has_no_fabricated_scores_rates_or_intervals(self):
        result = self.build([])
        self.assertEqual(result['metrics'], {})
        self.assertEqual(result['counts']['evaluated'], 0)
        self.assertEqual(result['sample']['actual_classes'], {'1st': 0, 'Égal': 0, '2nd': 0})
        self.assertEqual(result['by_utc_day'], [])
        self.assertEqual(result['by_league'], [])
        for reference in result['paired_deltas'].values():
            self.assertEqual(reference, dict(count=0, logloss=None, brier=None, accuracy=None))
        for method in result['calibration'].values():
            for bins in list(method['classes'].values())+[method['favorite_confidence']]:
                self.assertTrue(all(item['count'] == 0 and item['gap'] is None for item in bins))
        self.assertIsNone(result['sample']['evaluated_kickoff_span_hours'])
        self.assertIsNone(result['inference']['confidence_intervals'])
        self.assertIsNone(result['inference']['significance_tests'])
        self.assertFalse(result['inference']['available'])

    def test_monoclass_and_single_day_counts_do_not_claim_independence_or_advantage(self):
        rows = [self.row(str(index), league=str(index % 2),
                         timestamp=f'2026-10-31T{12+index:02d}:00:00') for index in range(4)]
        result = self.build(rows)
        sample = result['sample']
        self.assertEqual(sample['registered_utc_days'], 1)
        self.assertTrue(sample['registered_one_day_pilot'])
        self.assertEqual(sample['evaluated_utc_days'], 1)
        self.assertEqual(sample['evaluated_leagues'], 2)
        self.assertEqual(sample['evaluated_kickoff_utc_hours'], 4)
        self.assertEqual(sample['evaluated_kickoff_span_hours'], 3)
        self.assertEqual(sample['actual_classes'], {'1st': 4, 'Égal': 0, '2nd': 0})
        self.assertFalse(result['inference']['available'])
        self.assertFalse(result['automatic_promotion'])
        self.assertFalse(result['predictive_improvement_demonstrated'])
        self.assertEqual(result['support']['status'], 'unknown')

    def test_utc_day_and_league_summaries_use_the_same_paired_population(self):
        protocol = self.protocol([
            self.row('1', league='20', timestamp='2026-11-01T00:15:00+02:00'),
            self.row('2', league='10', actual='2nd', timestamp='2026-11-01T01:00:00+00:00'),
            self.row('3', league='10', status='pending', actual=None,
                     timestamp='2026-11-01T03:00:00+00:00')])
        protocol['confirmation_end'] = '2026-11-02T00:00:00+00:00'
        result = diagnostics.build(protocol)
        self.assertEqual([item['day'] for item in result['by_utc_day']], ['2026-10-31', '2026-11-01'])
        self.assertEqual(result['sample']['evaluated_utc_days'], 2)
        self.assertEqual([item['league'] for item in result['by_league']], ['10', '20'])
        first = result['by_utc_day'][0]
        self.assertEqual(first['actual_classes'], {'1st': 1, 'Égal': 0, '2nd': 0})
        second = result['by_league'][0]
        self.assertEqual(second['counts']['forecasts'], 2)
        self.assertEqual(second['counts']['evaluated'], 1)
        self.assertEqual(second['metrics']['candidate']['count'], 1)
        self.assertEqual(second['actual_classes'], {'1st': 0, 'Égal': 0, '2nd': 1})

    def test_report_metrics_are_ignored_output_is_deterministic_finite_json_and_input_is_untouched(self):
        protocol = self.protocol([self.row('2'), self.row('1')])
        before = deepcopy(protocol)
        first = diagnostics.build(protocol)
        self.assertEqual(protocol, before)
        second = deepcopy(protocol)
        second['rows'].reverse()
        second['metrics'] = float('nan')
        second['evaluated'] = -100
        self.assertEqual(diagnostics.build(second), first)
        self.assertEqual(json.loads(json.dumps(first, allow_nan=False)), first)
        self.assertEqual(len(first['source_sha256']), 64)
        second['rows'][0]['methods']['candidate'] = self.distribution(.5, .3, .2)
        self.assertNotEqual(diagnostics.build(second)['source_sha256'], first['source_sha256'])

    def test_no_file_access_prediction_or_training(self):
        protocol = self.protocol([self.row()])
        with patch('builtins.open', side_effect=AssertionError('No file access')), \
             patch.object(causal_candidate, 'fit_candidate', side_effect=AssertionError('No fit')), \
             patch.object(causal_candidate, 'predict_candidate', side_effect=AssertionError('No predict')):
            result = diagnostics.build(protocol)
        self.assertEqual(result['counts']['evaluated'], 1)

    def test_invalid_probability_distributions_rejected_even_for_excluded_rows(self):
        bad = [self.distribution(True, 0, 0), self.distribution(float('nan'), .5, .5),
               self.distribution(float('inf'), 0, 0), self.distribution(-.1, .6, .5),
               self.distribution(1.1, 0, -.1), self.distribution(.2, .2, .2),
               {'1st': .5, 'Égal': .5}, {'1st': .5, 'Égal': .5, '2nd': 0, 'extra': 0}]
        for probabilities in bad:
            with self.subTest(probabilities=probabilities):
                row = self.row(status='pending', actual=None)
                row['methods']['candidate'] = probabilities
                with self.assertRaises(ValueError):
                    self.build([row])

    def test_duplicate_forecast_or_match_identifiers_rejected(self):
        for field in ('forecast_id', 'match_id'):
            rows = [self.row('1'), self.row('2')]
            if field == 'match_id':
                rows[1]['fixture'][field] = 1  # Integer/string aliases must collide.
            else:
                rows[1][field] = rows[0][field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.build(rows)

    def test_missing_or_inconsistent_row_fields_are_rejected(self):
        mutations = [lambda row: row.update(admissible=1), lambda row: row.update(actual='other'),
            lambda row: row.update(status='unknown'), lambda row: row.update(timing_quality='unknown'),
            lambda row: row.update(actual=None), lambda row: row['methods'].pop('clubs'),
            lambda row: row['fixture'].update(match_id=True),
            lambda row: row['fixture'].update(timestamp='2026-10-31'),
            lambda row: row['fixture'].update(timestamp='2026-11-01T00:00:00Z'),
            lambda row: row.update(status='pending')]
        for index, mutate in enumerate(mutations):
            row = self.row(); mutate(row)
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.build([row])

    def test_single_protocol_and_complete_utc_window_required(self):
        for protocol in ([self.protocol()], {'protocols': [self.protocol()]},
                         dict(self.protocol(), rows={}), dict(self.protocol(), id=True),
                         dict(self.protocol(), confirmation_start='2026-10-31T00:00:00'),
                         dict(self.protocol(), confirmation_end='2026-10-31T00:00:00Z'),
                         dict(self.protocol(), confirmation_end='2026-11-01T01:00:00Z')):
            with self.subTest(protocol=protocol), self.assertRaises(ValueError):
                diagnostics.build(protocol)

    def test_current_implementation_compatibility_is_preserved_in_export(self):
        for compatible in (True, False, None):
            protocol = dict(self.protocol(), implementation_compatible=compatible)
            self.assertIs(diagnostics.build(protocol)['implementation_compatible'], compatible)
        with self.assertRaises(ValueError):
            diagnostics.build(dict(self.protocol(), implementation_compatible=1))


if __name__ == '__main__':
    unittest.main()
