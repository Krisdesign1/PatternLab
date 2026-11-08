"""First-capture, paired probabilities must remain independent of later choices."""
from copy import deepcopy
from datetime import datetime, timezone
from math import log
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

import individual_probability_validation as validation
import probability_implementation as implementation


NOW = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)


def distribution(first=.2, equal=.3, second=.5):
    return dict(zip(validation.LABELS, (first, equal, second)))


def observation(*, revision=1, checked='2026-11-03T19:00:00Z', state='settled',
                ht='1-0', ft='1-0', actual='1st', **extra):
    if state != 'settled':
        ht = ft = actual = None
    return dict(revision=revision, checked_at=checked, status=state,
                ht=ht, ft=ft, actual=actual, **extra)


def capture(match_id='1', *, identity=None, created='2026-11-03T10:00:00Z',
            kickoff='2026-11-03T15:00:00Z', baseline='default',
            probabilities=None, events='default'):
    if baseline == 'default':
        baseline = distribution()
    if events == 'default':
        events = [observation()]
    return dict(id=identity or 'forecast-'+str(match_id), source='API-Football',
        created_at=created, before=created[:10], model_version=validation.MODEL_VERSION,
        engine_version=validation.ENGINE_VERSION,
        model_code_sha256=implementation.code_hashes(),
        fixture=dict(match_id=str(match_id), league='39', home_id='10', away_id='20',
                     timestamp=kickoff, provider='api-football'),
        probabilities=distribution(.6, .3, .1) if probabilities is None else probabilities,
        baseline=baseline, result_history=deepcopy(events))


def protocol(*, start='2026-11-02T00:00:00Z'):
    return validation._seal(dict(version=validation.VERSION,
        created_at='2026-11-01T12:00:00Z', start_at=start,
        model_version=validation.MODEL_VERSION, engine_version=validation.ENGINE_VERSION,
        model_code_sha256=implementation.code_hashes(),
        methods=['frequency', 'clubs']))


class IndividualValidationBuildTests(unittest.TestCase):
    def build(self, rows, **kwargs):
        return validation.build(rows, now=kwargs.pop('now', NOW), **kwargs)

    def test_prospective_requires_capture_attestation_and_keeps_legacy_diagnostic(self):
        row = capture()
        row.pop('model_code_sha256')
        diagnostic = self.build([row])
        future = self.build([row], protocol=protocol())
        self.assertEqual(diagnostic['evaluated'], 1)
        self.assertEqual(future['matches'], 0)
        self.assertEqual(future['exclusions']['incompatible_model_code'], 1)

    def test_later_attested_capture_does_not_replace_unattested_first_one(self):
        first = capture(identity='first')
        first.pop('model_code_sha256')
        later = capture(identity='later', created='2026-11-03T11:00:00Z')
        future = self.build([later, first], protocol=protocol())
        self.assertEqual(future['matches'], 0)
        self.assertEqual(future['exclusions']['incompatible_model_code'], 1)

    def test_void_without_baseline_does_not_hold_a_closed_week_open(self):
        row = capture(baseline=None, events=[observation(state='void')])
        report = self.build([row], protocol=protocol())
        self.assertEqual(report['evaluated'], 0)
        self.assertEqual(report['closed_weeks'], 1)

    def test_verified_normalized_provider_schedule_is_naive_utc_not_local_time(self):
        row = capture()
        row['result_history'][0]['schedule_resolution'] = dict(
            original_timestamp=row['fixture']['timestamp'], forecast_created_at=row['created_at'],
            effective_timestamp='2026-11-03T16:00:00', raw_status='FT')
        report = self.build([row])
        self.assertEqual(report['evaluated'], 1)
        self.assertEqual(report['rows'][0]['state'], 'settled')

    def test_empty_and_pending_results_have_no_fabricated_zero_performance(self):
        for rows in ([], [capture(events=[])],
                     [capture(events=[observation(state='unresolved')])],
                     [capture(events=[observation(state='void')])]):
            with self.subTest(rows=rows):
                report = self.build(rows)
                self.assertEqual(report['evaluated'], 0)
                self.assertIsNone(report['delta_logloss'])
                for method in report['metrics'].values():
                    self.assertEqual(method['matches'], 0)
                    for key in ('accuracy', 'logloss', 'brier'):
                        self.assertIsNone(method[key])
                self.assertTrue(all(row['coverage'] is None for row in report['thresholds']))
                self.assertTrue(all(row['observed'] is None for row in report['calibration']))

    def test_first_capture_keeps_its_missing_baseline_and_does_not_borrow_later_one(self):
        first = capture(baseline=None)
        later = capture(identity='later', created='2026-11-03T11:00:00Z')
        report = self.build([later, first])
        self.assertEqual(report['matches'], 1)
        self.assertEqual(report['duplicates_removed'], 1)
        self.assertEqual(report['rows'][0]['forecast_id'], first['id'])
        self.assertEqual(report['missing_baseline'], 1)
        self.assertEqual(report['evaluated'], 0)
        self.assertIsNone(report['metrics']['frequency']['logloss'])

    def test_first_pending_capture_does_not_borrow_later_result_or_favorable_probabilities(self):
        first = capture(events=[], probabilities=distribution(.1, .2, .7))
        later = capture(identity='later', created='2026-11-03T11:00:00Z',
                        probabilities=distribution(.9, .05, .05))
        report = self.build([later, first])
        self.assertEqual(report['counts'], {'pending': 1})
        self.assertEqual(report['evaluated'], 0)
        self.assertEqual(report['rows'][0]['clubs'], first['probabilities'])

    def test_selection_is_input_order_independent_with_stable_tied_capture_ids(self):
        first = capture(identity='a', events=[])
        tied = capture(identity='z')
        a, b = self.build([first, tied]), self.build([tied, first])
        self.assertEqual(a, b)
        self.assertEqual(a['rows'][0]['forecast_id'], 'a')
        self.assertEqual(a['evaluated'], 0)

    def test_identical_matches_across_repeated_rankings_count_only_once(self):
        rows = [capture(identity=f'forecast-{index:03d}') for index in range(100)]
        report = self.build(rows)
        self.assertEqual(report['matches'], 1)
        self.assertEqual(report['evaluated'], 1)
        self.assertEqual(report['duplicates_removed'], 99)

    def test_baseline_is_taken_from_selected_capture_and_metrics_are_exact(self):
        a = capture(probabilities=distribution(.6, .3, .1), baseline=distribution(.2, .3, .5))
        b = capture('2', probabilities=distribution(.2, .3, .5),
                    baseline=distribution(.6, .3, .1),
                    events=[observation(ht='0-0', ft='1-0', actual='2nd')])
        report = self.build([a, b])
        self.assertEqual(report['evaluated'], 2)
        self.assertEqual(report['metrics']['clubs']['correct'], 2)
        self.assertEqual(report['metrics']['frequency']['correct'], 0)
        expected_clubs = (-log(.6)-log(.5))/2
        expected_frequency = (-log(.2)-log(.1))/2
        self.assertAlmostEqual(report['metrics']['clubs']['logloss'], expected_clubs)
        self.assertAlmostEqual(report['metrics']['frequency']['logloss'], expected_frequency)
        self.assertAlmostEqual(report['delta_logloss'], expected_clubs-expected_frequency)
        self.assertAlmostEqual(report['metrics']['clubs']['brier'], (.26+.38)/2)
        self.assertAlmostEqual(report['metrics']['frequency']['brier'], (.98+1.26)/2)

    def test_confidence_threshold_reference_uses_identical_selected_fixtures(self):
        a = capture(probabilities=distribution(.7, .2, .1), baseline=distribution(.2, .3, .5))
        b = capture('2', probabilities=distribution(.2, .2, .6), baseline=distribution(.6, .3, .1),
                    events=[observation(ht='0-0', ft='1-0', actual='2nd')])
        c = capture('3', probabilities=distribution(.4, .3, .3), baseline=distribution(.2, .6, .2),
                    events=[observation(ht='1-0', ft='2-0', actual='Égal')])
        report = self.build([a, b, c])
        thresholds = {row['threshold']: row for row in report['thresholds']}
        for threshold in (.5, .6):
            selected = thresholds[threshold]
            self.assertEqual(selected['evaluated'], 2)
            self.assertAlmostEqual(selected['coverage'], 2/3)
            self.assertAlmostEqual(selected['mean_confidence'], .65)
            self.assertEqual(selected['metrics']['clubs']['accuracy'], 1)
            self.assertEqual(selected['metrics']['frequency']['accuracy'], 0)
            self.assertAlmostEqual(selected['metrics']['frequency']['logloss'], (-log(.2)-log(.1))/2)
        self.assertEqual(thresholds[.7]['evaluated'], 1)
        self.assertAlmostEqual(thresholds[.7]['coverage'], 1/3)

    def test_calibration_covers_every_class_for_both_methods_and_exact_boundaries(self):
        a = capture(probabilities=distribution(.7, .2, .1))
        b = capture('2', probabilities=distribution(.1, .2, .7),
                    events=[observation(ht='0-0', ft='1-0', actual='2nd')])
        bins = self.build([a, b])['calibration']
        self.assertEqual(len(bins), 60)
        for method in ('frequency', 'clubs'):
            for label in validation.LABELS:
                self.assertEqual(sum(row['matches'] for row in bins
                                     if row['method'] == method and row['label'] == label), 2)
        chosen = next(row for row in bins if row['method'] == 'clubs'
                      and row['label'] == '1st' and row['lower'] == .7)
        self.assertEqual(chosen['matches'], 1)
        self.assertEqual(chosen['announced'], .7)
        self.assertEqual(chosen['observed'], 1)
        empty = next(row for row in bins if row['method'] == 'clubs'
                     and row['label'] == '1st' and row['lower'] == .9)
        self.assertEqual(empty['matches'], 0)
        self.assertIsNone(empty['announced'])
        self.assertIsNone(empty['observed'])

    def test_result_revisions_are_selected_as_of_reading_time(self):
        item = capture(events=[observation(revision=1), observation(revision=2,
            checked='2026-11-05T19:00:00Z', ht='0-0', ft='1-0', actual='2nd')])
        early = self.build([item], now=datetime(2026, 11, 4, tzinfo=timezone.utc))
        later = self.build([item])
        self.assertEqual(early['rows'][0]['actual'], '1st')
        self.assertEqual(later['rows'][0]['actual'], '2nd')
        self.assertEqual(early['metrics']['clubs']['correct'], 1)
        self.assertEqual(later['metrics']['clubs']['correct'], 0)
        self.assertAlmostEqual(later['metrics']['clubs']['logloss'], -log(.1))

    def test_no_result_known_as_of_time_is_pending_even_if_current_projection_is_settled(self):
        item = capture(events=[observation(checked='2026-11-05T19:00:00Z')])
        item.update(status='settled', ht='1-0', ft='1-0', actual='1st', checked_at='2026-11-05T19:00:00Z')
        report = self.build([item], now=datetime(2026, 11, 4, tzinfo=timezone.utc))
        self.assertEqual(report['counts'], {'pending': 1})
        self.assertEqual(report['evaluated'], 0)

    def test_regulation_scores_determine_outcome_even_when_extra_time_or_penalties_are_present(self):
        item = capture(events=[observation(ht='1-0', ft='2-0', actual='Égal',
            raw_status='PEN', extra_time='5-0', penalties='4-2')])
        report = self.build([item])
        self.assertEqual(report['rows'][0]['actual'], 'Égal')
        self.assertAlmostEqual(report['metrics']['clubs']['logloss'], -log(.3))

    def test_conflicting_scores_or_result_checked_before_kickoff_remain_unresolved(self):
        bad = [observation(actual='2nd'), observation(ht='2-0', ft='1-0'),
               observation(checked='2026-11-03T14:00:00Z')]
        for event in bad:
            with self.subTest(event=event):
                report = self.build([capture(events=[event])])
                self.assertEqual(report['counts'], {'unresolved': 1})
                self.assertEqual(report['exclusions']['invalid_result'], 1)
                self.assertEqual(report['evaluated'], 0)
                self.assertIsNone(report['metrics']['clubs']['logloss'])

    def test_effective_postponement_accepts_only_result_after_corrected_kickoff(self):
        item = capture()
        audit = dict(original_timestamp=item['fixture']['timestamp'], forecast_created_at=item['created_at'],
                     effective_timestamp='2026-11-04T15:00:00Z', raw_status='FT')
        item['result_history'] = [observation(checked='2026-11-04T19:00:00Z', schedule_resolution=audit)]
        self.assertEqual(self.build([item])['evaluated'], 1)
        item['result_history'][0]['checked_at'] = '2026-11-04T14:00:00Z'
        self.assertEqual(self.build([item])['counts'], {'unresolved': 1})
        item['result_history'][0]['schedule_resolution']['forecast_created_at'] = '2026-11-03T11:00:00Z'
        self.assertEqual(self.build([item])['evaluated'], 0)

    def test_advanced_effective_kickoff_cannot_make_capture_noncausal(self):
        item = capture(created='2026-11-03T14:00:00Z')
        audit = dict(original_timestamp=item['fixture']['timestamp'], forecast_created_at=item['created_at'],
                     effective_timestamp='2026-11-03T13:00:00Z', raw_status='FT')
        item['result_history'] = [observation(schedule_resolution=audit)]
        self.assertEqual(self.build([item])['counts'], {'unresolved': 1})
        item = capture(created='2026-11-03T10:00:00Z')
        audit['forecast_created_at'] = item['created_at']
        item['result_history'] = [observation(schedule_resolution=audit)]
        self.assertEqual(self.build([item])['evaluated'], 1)

    def test_future_matches_remain_pending_and_future_captures_are_excluded(self):
        item = capture(created='2026-11-16T10:00:00Z', kickoff='2026-11-17T15:00:00Z', events=[])
        future = capture('2', created='2026-11-17T10:00:00Z', kickoff='2026-11-17T15:00:00Z', events=[])
        report = self.build([item, future])
        self.assertEqual(report['matches'], 1)
        self.assertEqual(report['counts'], {'pending': 1})
        self.assertEqual(report['evaluated'], 0)
        self.assertEqual(report['exclusions']['invalid_snapshot'], 1)
        self.assertEqual(report['closed_weeks'], 0)

    def test_historical_rows_do_not_enter_prospective_protocol(self):
        old = capture('2', created='2026-11-01T10:00:00Z', kickoff='2026-11-01T15:00:00Z',
                      events=[observation(checked='2026-11-01T19:00:00Z')])
        current = capture()
        self.assertEqual(self.build([old, current])['evaluated'], 2)
        report = self.build([old, current], protocol=protocol())
        self.assertEqual(report['scope'], 'prospective')
        self.assertEqual(report['evaluated'], 1)
        self.assertEqual(report['exclusions']['before_protocol_start'], 1)
        self.assertEqual(report['protocol_id'], protocol()['id'])

    def test_historical_first_capture_cannot_become_prospective_evidence_by_recapture(self):
        first = capture(created='2026-11-01T10:00:00Z')
        later = capture(identity='later', created='2026-11-03T11:00:00Z')
        report = self.build([later, first], protocol=protocol())
        self.assertEqual(report['matches'], 0)
        self.assertEqual(report['evaluated'], 0)

    def test_calendar_week_is_open_until_end_and_while_any_captured_result_is_pending(self):
        good = capture()
        pending = capture('2', events=[])
        report = self.build([good, pending], protocol=protocol())
        self.assertEqual(report['weekly'][0]['calendar_closed'], True)
        self.assertEqual(report['weekly'][0]['eligible'], False)
        self.assertEqual(report['closed_weeks'], 0)
        self.assertEqual(report['evaluated'], 1)
        self.assertEqual(report['weekly'][0]['counts'], {'settled': 1, 'pending': 1})
        report = self.build([good], now=datetime(2026, 11, 8, 23, 59, tzinfo=timezone.utc), protocol=protocol())
        self.assertFalse(report['weekly'][0]['calendar_closed'])
        report = self.build([good], now=datetime(2026, 11, 9, tzinfo=timezone.utc), protocol=protocol())
        self.assertTrue(report['weekly'][0]['eligible'])

    def test_incomplete_start_week_and_missing_baselines_do_not_form_closed_week(self):
        report = self.build([capture()], protocol=protocol(start='2026-11-02T01:00:00Z'))
        self.assertEqual(report['closed_weeks'], 0)
        self.assertFalse(report['weekly'][0]['calendar_closed'])
        report = self.build([capture(baseline=None)], protocol=protocol())
        self.assertEqual(report['closed_weeks'], 0)
        self.assertFalse(report['weekly'][0]['eligible'])

    def test_void_result_is_reported_excluded_from_loss_and_does_not_hold_week_open(self):
        report = self.build([capture(), capture('2', events=[observation(state='void')])], protocol=protocol())
        self.assertEqual(report['matches'], 2)
        self.assertEqual(report['counts'], {'settled': 1, 'void': 1})
        self.assertEqual(report['evaluated'], 1)
        self.assertEqual(report['closed_weeks'], 1)

    def test_daily_display_uses_kinshasa_while_weeks_use_utc(self):
        item = capture(created='2026-11-01T10:00:00Z', kickoff='2026-11-01T23:30:00Z',
                       events=[observation(checked='2026-11-02T03:00:00Z')])
        report = self.build([item])
        self.assertEqual(report['rows'][0]['day'], '2026-11-02')
        self.assertEqual(report['rows'][0]['week'], '2026-10-26')

    def test_snapshot_sources_versions_identifiers_distributions_and_causal_cutoffs_are_checked(self):
        changes = [lambda r:r.update(source='Other'),
                   lambda r:r['fixture'].update(provider='other'),
                   lambda r:r['fixture'].update(home_id=True),
                   lambda r:r['fixture'].update(away_id='10'),
                   lambda r:r['fixture'].update(match_id='0'),
                   lambda r:r['fixture'].update(league='٣٩'),
                   lambda r:r.update(created_at=r['fixture']['timestamp']),
                   lambda r:r.update(created_at='2026-11-03T10:00:00'),
                   lambda r:r.update(before='2026-11-04'),
                   lambda r:r.update(probabilities=distribution(.6, .3, .2)),
                   lambda r:r.update(probabilities=distribution(True, 0, 0)),
                   lambda r:r.update(probabilities=distribution(float('nan'), .3, .5))]
        for change in changes:
            item = capture(); change(item)
            with self.subTest(item=item):
                report = self.build([item])
                self.assertEqual(report['matches'], 0)
                self.assertEqual(report['exclusions']['invalid_snapshot'], 1)
        for field in ('engine_version', 'model_version'):
            wrong = capture(); wrong[field] = 'different'
            report = self.build([wrong, capture('2')])
            self.assertEqual(report['matches'], 1)
            self.assertEqual(report['exclusions']['other_version'], 1)

    def test_invalid_baseline_excludes_pair_without_dropping_capture(self):
        item = capture(baseline=distribution(.6, .3, .2))
        report = self.build([item])
        self.assertEqual(report['matches'], 1)
        self.assertEqual(report['missing_baseline'], 1)
        self.assertEqual(report['evaluated'], 0)

    def test_input_rows_and_protocol_are_not_modified(self):
        rows, plan = [capture(), capture('2', events=[])], protocol()
        previous = deepcopy((rows, plan))
        self.build(rows, protocol=plan)
        self.assertEqual((rows, plan), previous)


class IndividualValidationProtocolTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.code = self.root/'code'
        self.code.mkdir()
        self.storage = self.root/'storage'
        self.enrichment = 'validation_selection.json'
        selected = dict(policies=dict(scores='clubs', rich='clubs'), metrics={
            name:dict(logloss=1.05+index*.01) for index, name in enumerate(('frequency', 'clubs', 'scores', 'rich'))})
        (self.code/self.enrichment).write_text(json.dumps(selected))
        for name in validation.SOURCE_FILES:
            (self.code/name).write_text('# frozen source '+name+'\n')
        for target, value in (('ROOT', self.code), ('ENRICHMENT', self.enrichment)):
            patcher = patch.object(validation, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def prepare(self, **kwargs):
        return validation.prepare(self.storage, now=kwargs.pop('now', NOW), **kwargs)

    def test_reading_missing_protocol_creates_nothing(self):
        self.assertIsNone(validation.load_protocol(self.storage))
        self.assertFalse(self.storage.exists())

    def test_prepare_freezes_rejected_candidate_versions_hashes_and_future_local_day(self):
        clock = datetime(2026, 10, 28, 23, 30, tzinfo=timezone.utc)
        before = {p.name:p.read_bytes() for p in self.code.iterdir()}
        plan = self.prepare(now=clock)
        self.assertEqual(plan, validation.load_protocol(self.storage))
        self.assertEqual(plan['start_at'], '2026-10-30T00:00:00+01:00')
        self.assertGreater(validation._stamp(plan['start_at']), clock)
        self.assertEqual(plan['methods'], ['frequency', 'clubs'])
        self.assertEqual(plan['enriched']['status'], 'rejected_on_historical_validation')
        self.assertEqual(plan['enriched']['selected_policy'], 'clubs')
        self.assertEqual(set(plan['code_sha256']), set(validation.SOURCE_FILES))
        self.assertEqual(validation.compatibility(plan), dict(compatible=True, changed=[]))
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.code.iterdir()})
        self.assertEqual(list(self.storage.rglob('*.*')), [validation.protocol_path(self.storage)])

    def test_protocol_is_exclusive_and_cannot_overwrite_first_rules(self):
        self.prepare()
        saved = validation.protocol_path(self.storage).read_bytes()
        with self.assertRaises(FileExistsError):
            self.prepare(now=datetime(2026, 11, 18, tzinfo=timezone.utc))
        self.assertEqual(validation.protocol_path(self.storage).read_bytes(), saved)

    def test_default_production_location_ignores_injected_historical_clock(self):
        # ROOT itself is temporary, so even the default-location branch cannot
        # touch the real application data or an existing production protocol.
        wall_clock = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
        historical = datetime(2020, 1, 1, tzinfo=timezone.utc)
        with patch.object(validation, '_now', side_effect=lambda now: wall_clock if now is None else now):
            plan = validation.prepare(now=historical)
        self.assertEqual(validation._stamp(plan['created_at']), wall_clock)
        self.assertEqual(plan['start_at'], '2026-11-17T00:00:00+01:00')
        self.assertTrue(validation.protocol_path().is_relative_to(self.root))
        self.assertFalse(self.storage.exists())

    def test_tampering_and_resealed_incoherent_or_wrong_versions_are_rejected(self):
        original = self.prepare()
        path = validation.protocol_path(self.storage)
        changed = deepcopy(original); changed['start_at'] = changed['created_at']
        path.write_text(json.dumps(changed))
        with self.assertRaises(ValueError):
            validation.load_protocol(self.storage)
        for field, value in [('start_at', original['created_at']), ('model_version', 'other'),
                             ('engine_version', 'other'), ('methods', ['clubs'])]:
            changed = {k:v for k,v in original.items() if k != 'id'}
            changed[field] = value
            path.write_text(json.dumps(validation._seal(changed)))
            with self.subTest(field=field), self.assertRaises(ValueError):
                validation.load_protocol(self.storage)

    def test_changed_code_or_enrichment_is_reported_without_rewriting_protocol(self):
        plan = self.prepare()
        saved = validation.protocol_path(self.storage).read_bytes()
        name = validation.SOURCE_FILES[0]
        (self.code/name).write_text('# changed\n')
        (self.code/self.enrichment).write_text('{}')
        report = validation.compatibility(plan)
        self.assertFalse(report['compatible'])
        self.assertCountEqual(report['changed'], [name, self.enrichment])
        self.assertEqual(validation.protocol_path(self.storage).read_bytes(), saved)

    def test_missing_or_nonrejected_enriched_selection_never_creates_protocol(self):
        for content in ({}, dict(policies=dict(rich='head'))):
            (self.code/self.enrichment).write_text(json.dumps(content))
            with self.subTest(content=content), self.assertRaises(ValueError):
                self.prepare()
            self.assertFalse(self.storage.exists())

    def test_naive_clocks_are_refused_before_any_write(self):
        with self.assertRaises(ValueError):
            self.prepare(now=datetime(2026, 11, 16))
        self.assertFalse(self.storage.exists())
        with self.assertRaises(ValueError):
            validation.build([], now=datetime(2026, 11, 16))


if __name__ == '__main__':
    unittest.main()
