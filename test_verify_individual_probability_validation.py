"""Adversarial checks for the independent archive/report verifier only."""
from copy import deepcopy
from datetime import datetime, timezone
import gzip
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts import verify_individual_probability_validation as verifier


READING = datetime(2026, 11, 5, tzinfo=timezone.utc)


def capture(identity='first', created='2026-11-03T10:00:00+00:00'):
    return dict(id=identity, source='API-Football', model_version=verifier.MODEL,
                engine_version=verifier.ENGINE, created_at=created, before='2026-11-03',
                fixture=dict(provider='api-football', match_id='100', league='39',
                             home_id='10', away_id='20', timestamp='2026-11-03T16:00:00+00:00'),
                probabilities={'1st': 0.6, 'Égal': 0.2, '2nd': 0.2},
                baseline={'1st': 0.4, 'Égal': 0.3, '2nd': 0.3},
                model_code_sha256={'model.py': 'fixed'},
                result_history=[dict(revision=1, status='settled', checked_at='2026-11-03T19:00:00+00:00',
                                     ht='1-0', ft='1-0', actual='1st')])


class IndependentVerificationTests(unittest.TestCase):
    def test_known_metrics_thresholds_and_all_class_calibration(self):
        result = verifier.project([capture()], READING)
        self.assertEqual((result['matches'], result['evaluated']), (1, 1))
        for method, probability, brier in (('clubs', 0.6, 0.24), ('frequency', 0.4, 0.54)):
            metrics = result['metrics'][method]
            self.assertEqual((metrics['correct'], metrics['accuracy']), (1, 1))
            self.assertAlmostEqual(metrics['logloss'], -math.log(probability))
            self.assertAlmostEqual(metrics['brier'], brier)
        self.assertEqual([row['evaluated'] for row in result['thresholds']], [1, 1, 0])
        self.assertEqual([row['coverage'] for row in result['thresholds']], [1, 1, 0])
        self.assertEqual(len(result['calibration']), 60)
        for method in verifier.METHODS:
            for label in verifier.LABELS:
                self.assertEqual(sum(row['matches'] for row in result['calibration']
                                     if row['method'] == method and row['label'] == label), 1)
        bin_row = next(row for row in result['calibration']
                       if row['method'] == 'clubs' and row['label'] == '1st' and row['lower'] == 0.6)
        self.assertEqual((bin_row['announced'], bin_row['observed']), (0.6, 1))

    def test_first_global_capture_precedes_start_and_model_code_filters(self):
        old = capture()
        later = capture('later', '2026-11-03T12:00:00+00:00')
        plan = dict(id='protocol', start_at='2026-11-03T11:00:00+00:00',
                    model_code_sha256={'model.py': 'fixed'})
        result = verifier.project([later, old], READING, plan)
        self.assertEqual(result['matches'], 0)
        self.assertEqual(result['exclusions'], {'before_protocol_start': 1})
        plan['start_at'] = '2026-11-03T09:00:00+00:00'
        old['model_code_sha256'] = {'model.py': 'different'}
        result = verifier.project([later, old], READING, plan)
        self.assertEqual(result['matches'], 0)
        self.assertEqual(result['exclusions'], {'incompatible_model_code': 1})

    def test_another_version_does_not_block_the_pinned_first_capture(self):
        old = capture()
        old['engine_version'] = 'other-engine'
        later = capture('later', '2026-11-03T12:00:00+00:00')
        result = verifier.project([old, later], READING)
        self.assertEqual(result['rows'][0]['forecast_id'], 'later')
        self.assertEqual(result['exclusions'], {'other_version': 1})

    def test_later_capture_cannot_supply_baseline_or_own_result(self):
        old = capture()
        old['baseline'] = None
        old['result_history'] = []
        later = capture('later', '2026-11-03T12:00:00+00:00')
        result = verifier.project([later, old], READING)
        self.assertEqual((result['matches'], result['evaluated'], result['missing_baseline']), (1, 0, 1))
        self.assertEqual(result['rows'][0]['state'], 'pending')
        self.assertEqual(result['duplicates_removed'], 1)

    def test_own_history_asof_overrides_top_level_and_later_capture(self):
        first = capture()
        first.update(status='settled', actual='2nd', ht='0-0', ft='1-0')
        first['result_history'].append(dict(revision=2, checked_at='2026-11-06T19:00:00+00:00',
                                            status='settled', ht='0-0', ft='1-0', actual='2nd'))
        later = capture('later', '2026-11-03T12:00:00+00:00')
        later['result_history'][0].update(ht='0-0', ft='1-0', actual='2nd')
        result = verifier.project([later, first], READING)
        self.assertEqual(result['rows'][0]['actual'], '1st')
        earlier = verifier.project([first], datetime(2026, 11, 3, 18, tzinfo=timezone.utc))
        self.assertEqual(earlier['rows'][0]['state'], 'pending')

    def test_provider_naive_effective_timestamp_means_utc_only(self):
        row = capture()
        row['fixture']['timestamp'] = '2026-11-03T14:00:00+00:00'
        result = row['result_history'][0]
        result['checked_at'] = '2026-11-03T15:30:00+00:00'
        result['schedule_resolution'] = dict(original_timestamp=row['fixture']['timestamp'],
                                             forecast_created_at=row['created_at'],
                                             effective_timestamp='2026-11-03T16:00:00')
        reading = datetime(2026, 11, 3, 15, 45, tzinfo=timezone.utc)
        projected = verifier.project([row], reading)
        self.assertEqual(projected['counts'], {'unresolved': 1})
        result['schedule_resolution']['effective_timestamp'] = '2026-11-03T16:00:00+01:00'
        self.assertEqual(verifier.project([row], reading)['counts'], {'settled': 1})
        result['checked_at'] = '2026-11-03T15:30:00'
        self.assertEqual(verifier.project([row], reading)['counts'], {'unresolved': 1})

    def test_history_cutoff_uses_utc_dates_and_capture_clock_is_aware(self):
        row = capture(created='2026-11-04T00:30:00+01:00')
        row['before'] = '2026-11-04'
        row['fixture']['timestamp'] = '2026-11-04T16:00:00+01:00'
        self.assertEqual(verifier.project([row], READING)['exclusions'], {'invalid_snapshot': 1})
        row = capture(created='2026-11-03T10:00:00')
        self.assertEqual(verifier.project([row], READING)['exclusions'], {'invalid_snapshot': 1})

    def test_report_tampering_in_ids_metrics_counts_or_bins_is_detected(self):
        source = [capture()]
        report = dict(as_of=READING.isoformat(), protocol=None, compatibility=None,
                      descriptive=verifier.project(source, READING), prospective=None)
        self.assertEqual(verifier.verify(source, report)['status'], 'verified')
        changes = (lambda d: d['rows'][0].update(forecast_id='substituted'),
                   lambda d: d['metrics']['clubs'].update(correct=0),
                   lambda d: d['counts'].update(pending=1),
                   lambda d: d['calibration'][0].update(matches=99))
        for change in changes:
            altered = deepcopy(report)
            change(altered['descriptive'])
            self.assertEqual(verifier.verify(source, altered)['status'], 'failed')

    def test_cli_runs_isolated_and_never_overwrites_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, report, output = root/'source.json.gz', root/'report.json', root/'verification.json'
            records = [capture()]
            with gzip.open(source, 'wt', encoding='utf-8') as handle:
                json.dump(records, handle)
            report.write_text(json.dumps(dict(as_of=READING.isoformat(), protocol=None, compatibility=None,
                                              descriptive=verifier.project(records, READING), prospective=None)))
            command = [sys.executable, '-B', '-I', str(Path(verifier.__file__).resolve()),
                       '--source', str(source), '--report', str(report), '--output', str(output)]
            first = subprocess.run(command, cwd=root, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            content = output.read_bytes()
            result = json.loads(content)
            self.assertFalse(result['project_modules_imported'])
            self.assertEqual(result['status'], 'verified')
            second = subprocess.run(command, cwd=root, capture_output=True, text=True)
            self.assertEqual(second.returncode, 2)
            self.assertEqual(output.read_bytes(), content)


if __name__ == '__main__':
    unittest.main()
