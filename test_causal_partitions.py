"""Chronological gates, withheld confirmation and native lot03 compatibility."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import api_match_workspace as calendar
import causal_feature_artifacts as artifacts
from causal_feature_workspace import VERSION as COHORT_VERSION, digest
import causal_partitions as partitions
import prematch_feature_snapshot as snapshots
from test_api_football import fixture


def windows():
    dates = ['2026-01-01', '2026-02-01', '2026-03-01', '2026-04-01', '2026-05-01']
    return {name: dict(start=dates[i]+'T00:00:00Z', end=dates[i+1]+'T00:00:00Z')
            for i, name in enumerate(partitions.PHASES)}


def seal(value):
    value.pop('id', None)
    value['id'] = digest(value)
    return value


def synthetic_bundle():
    """Twelve full source-bound fixtures, not observations from production."""
    requests, entries, saved, examples = [], [], {}, []
    for phase_index, month in enumerate((1, 2, 3, 4)):
        for label_index in range(3):
            mid = 900001 + phase_index*10 + label_index
            raw = fixture(mid, status='NS', season=2026)
            raw['fixture']['date'] = f'2026-{month:02d}-{10+label_index:02d}T16:00:00Z'
            raw['teams']['home'].update(id=mid*2, name=f'Home {mid}')
            raw['teams']['away'].update(id=mid*2+1, name=f'Away {mid}')
            decision = f'2026-{month:02d}-{10+label_index:02d}T10:00:00+00:00'
            observation = f'2026-{month:02d}-{10+label_index:02d}T09:00:00+00:00'
            target = calendar.fixture_from_raw(raw)
            ref = dict(match_id=str(mid), observation_id=format(mid, '064x'), observed_at_utc=observation,
                       time_kind='local_import_recorded', raw_sha256=digest(raw))
            snapshot = snapshots.build([], target, decision_at=decision, source_refs=[], target_ref=ref, target_raw=raw)
            sid = snapshot['id']; saved[sid] = snapshot
            request = dict(match_id=str(mid), decision_at=decision)
            requests.append(request)
            entries.append(dict(**request, snapshot_id=sid, history_quality_exclusions=[]))
            result = deepcopy(raw); result['fixture']['status']['short'] = 'FT'
            ht, ft = [((1, 0), (1, 0)), ((1, 0), (1, 1)), ((0, 0), (1, 0))][label_index]
            result['score']['halftime'] = dict(zip(('home', 'away'), ht))
            result['score']['fulltime'] = dict(zip(('home', 'away'), ft))
            normalized, _ = api._normalize_matches([result]); row = normalized[0]
            examples.append(dict(snapshot_id=sid, features=deepcopy(snapshot['features']),
                label=row['value'], match=deepcopy(snapshot['fixture']), decision_at=decision,
                ht=row['ht'], ft=row['ft'], outcome_row=row,
                outcome_source=dict(match_id=str(mid), observation_id=format(mid+1000, '064x'),
                    observed_at_utc=f'2026-{month:02d}-{10+label_index:02d}T19:00:00+00:00',
                    time_kind='local_import_recorded', normalized_sha256=digest(row))))
    manifest = seal(dict(version=COHORT_VERSION, kind='feature_inputs_only', requests=requests,
        entries=entries, exclusions=[], requested=12, included=12, excluded=0, unique_fixtures=12,
        implementation={}, temporal_quality='retrospective_conditional', prospective_confirmation=False,
        automatic_model_training=False, active_model_changed=False))
    outcomes = seal(dict(version=COHORT_VERSION, kind='labelled_feature_examples', cohort_id=manifest['id'],
        outcome_at='2026-05-02T00:00:00+00:00', examples=examples, exclusions=[], evaluated=12,
        not_evaluable=0, temporal_quality='retrospective_conditional', prospective_confirmation=False,
        automatic_model_training=False, active_model_changed=False))
    artifacts.validate_cohort(manifest, saved); artifacts.validate_outcomes(outcomes, manifest, saved)
    return dict(manifest=manifest, snapshots=saved, outcomes=outcomes)


class CausalPartitionTests(unittest.TestCase):
    def setUp(self):
        self.bundle = synthetic_bundle()
        self.plan = partitions.make_plan(windows(), minimums=dict(train=3, tuning=3, calibration=3))

    def test_four_explicit_periods_share_one_schema_and_exclude_confirmation_labels(self):
        result = partitions.prepare(self.bundle, self.plan)
        self.assertTrue(result['ready'])
        self.assertEqual([result['phases'][name]['included'] for name in partitions.PHASES], [3]*4)
        self.assertEqual([result['phases'][name]['usable_labels'] for name in partitions.PHASES], [3, 3, 3, 0])
        self.assertIsNone(result['phases']['confirmation']['classes'])
        development = partitions.development_inputs(result, self.bundle)
        self.assertEqual(set(development['phases']), {'train', 'tuning', 'calibration'})
        self.assertEqual(len(development['snapshots']), 9)
        confirmation_ids = {row['snapshot_id'] for row in result['phases']['confirmation']['entries']}
        self.assertFalse(confirmation_ids & set(development['snapshots']))
        self.assertFalse(result['prospective_confirmation'])

    def test_bounds_reject_overlaps_gaps_nonmidnight_and_empty_periods(self):
        for name, field, value in [('tuning', 'start', '2026-01-31T00:00:00Z'),
                                    ('tuning', 'start', '2026-02-02T00:00:00Z'),
                                    ('train', 'start', '2026-01-01T01:00:00Z'),
                                    ('train', 'end', '2026-01-01T00:00:00Z')]:
            changed = windows(); changed[name][field] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                partitions.make_plan(changed)
        changed = windows(); changed['train']['start'] = '2025-12-31T23:00:00-01:00'
        self.assertEqual(partitions.make_plan(changed)['windows']['train']['start'], '2026-01-01T00:00:00+00:00')

    def test_label_at_boundary_or_later_is_not_available_in_prior_training(self):
        for stamp in ['2026-02-01T00:00:00+00:00', '2026-04-30T19:00:00+00:00']:
            changed = deepcopy(self.bundle)
            changed['outcomes']['examples'][0]['outcome_source']['observed_at_utc'] = stamp
            seal(changed['outcomes'])
            result = partitions.prepare(changed, self.plan)
            self.assertEqual(result['phases']['train']['usable_labels'], 2)
            self.assertFalse(result['ready'])
            self.assertEqual(result['phases']['train']['entries'][0]['label_status'], 'label_not_available_before_next_period')

    def test_all_three_boundaries_exclude_exactly_equal_label_receipts(self):
        for index, name in enumerate(partitions.DEVELOPMENT):
            changed = deepcopy(self.bundle)
            changed['outcomes']['examples'][index*3]['outcome_source']['observed_at_utc'] = self.plan['windows'][name]['end']
            seal(changed['outcomes'])
            result = partitions.prepare(changed, self.plan)
            self.assertEqual(result['phases'][name]['usable_labels'], 2)

    def test_future_outcome_reads_and_hidden_confirmation_fields_are_rejected(self):
        changed = deepcopy(self.bundle)
        changed['outcomes']['outcome_at'] = '2099-01-01T00:00:00Z'; seal(changed['outcomes'])
        with self.assertRaises(ValueError): partitions.prepare(changed, self.plan)
        result = partitions.prepare(self.bundle, self.plan)
        changed = partitions.development_inputs(result, self.bundle)
        changed['confirmation_examples'] = deepcopy(self.bundle['outcomes']['examples'][9:]); seal(changed)
        with self.assertRaises(ValueError): partitions.validate_development(changed)

    def test_missing_class_blocks_training_even_when_total_support_suffices(self):
        changed = deepcopy(self.bundle)
        row = changed['outcomes']['examples'].pop(0)
        changed['outcomes']['exclusions'].append(dict(snapshot_id=row['snapshot_id'],
            match_id=row['match']['match_id'], reason='missing_result'))
        changed['outcomes']['evaluated'] -= 1; changed['outcomes']['not_evaluable'] += 1
        seal(changed['outcomes'])
        plan = partitions.make_plan(windows(), minimums=dict(train=2, tuning=3, calibration=3))
        result = partitions.prepare(changed, plan)
        self.assertEqual(result['phases']['train']['usable_labels'], 2)
        self.assertFalse(result['ready'])
        self.assertTrue(any('classes absentes' in value for value in result['blockers']))

    def test_end_is_exclusive_and_exclusions_keep_requested_denominator(self):
        narrow = windows()
        # All requests predate these four future windows.
        for row in narrow.values():
            row['start'] = row['start'].replace('2026-', '2027-')
            row['end'] = row['end'].replace('2026-', '2027-')
        result = partitions.prepare(self.bundle, partitions.make_plan(narrow))
        self.assertEqual(len(result['outside_windows']), 12)
        self.assertEqual(result['requested'], 12)
        self.assertTrue(all(value['requested'] == 0 for value in result['phases'].values()))

    def test_missing_labels_are_not_losses_and_do_not_remove_input_population(self):
        self.bundle['outcomes'] = None
        result = partitions.prepare(self.bundle, self.plan)
        self.assertEqual(result['requested'], 12)
        self.assertEqual(result['phases']['train']['included'], 3)
        self.assertEqual(result['phases']['train']['usable_labels'], 0)
        self.assertFalse(result['ready'])
        self.assertFalse(partitions.development_inputs(result, self.bundle)['phases']['train'])

    def test_confirmation_changes_do_not_change_development_examples_or_readiness(self):
        first = partitions.prepare(self.bundle, self.plan)
        changed = deepcopy(self.bundle)
        for row in changed['outcomes']['examples'][9:]:
            row['ht'] = row['outcome_row']['ht'] = '2-0'
            row['ft'] = row['outcome_row']['ft'] = '2-0'
            row['label'] = row['outcome_row']['value'] = '1st'
            row['outcome_source']['normalized_sha256'] = digest(row['outcome_row'])
        seal(changed['outcomes'])
        later = partitions.prepare(changed, self.plan)
        self.assertEqual(first['phases'], later['phases'])
        self.assertEqual(first['ready'], later['ready'])
        for name in partitions.DEVELOPMENT:
            self.assertEqual(partitions.development_inputs(first, self.bundle)['phases'][name],
                             partitions.development_inputs(later, changed)['phases'][name])

    def test_decision_and_kickoff_must_share_the_period_even_when_input_was_saved(self):
        # A valid source-bound fixture can be known on the eve of the next period.
        changed = deepcopy(self.bundle)
        old = changed['manifest']['entries'][3]; old_sid = old['snapshot_id']
        snapshot = changed['snapshots'].pop(old_sid)
        snapshot['decision_at'] = '2026-01-31T10:00:00+00:00'
        snapshot['target_ref']['observed_at_utc'] = '2026-01-31T09:00:00+00:00'
        rebuilt = snapshots.build([], snapshot['fixture'], decision_at=snapshot['decision_at'], source_refs=[],
                                  target_ref=snapshot['target_ref'], target_raw=snapshot['target_observation'])
        sid = rebuilt['id']; changed['snapshots'][sid] = rebuilt
        old.update(snapshot_id=sid, decision_at=rebuilt['decision_at'])
        changed['manifest']['requests'][3]['decision_at'] = rebuilt['decision_at']
        seal(changed['manifest']); changed['outcomes'] = None
        result = partitions.prepare(changed, self.plan)
        self.assertIn('kickoff_crosses_partition', [row['reason'] for row in result['phases']['train']['exclusions']])

    def test_duplicate_fixture_or_resealed_false_readiness_is_rejected(self):
        changed = deepcopy(self.bundle)
        changed['manifest']['requests'].append(deepcopy(changed['manifest']['requests'][0]))
        changed['manifest']['requested'] += 1; seal(changed['manifest'])
        with self.assertRaises(ValueError): partitions.prepare(changed, self.plan)
        result = partitions.prepare(self.bundle, self.plan)
        result['ready'] = False; seal(result)
        with self.assertRaises(ValueError): partitions.validate_partition(result, self.bundle, self.plan)

    def test_changed_recipe_minima_or_prospective_claim_cannot_be_resealed(self):
        for key, value in [('prospective_confirmation', True), ('automatic_promotion', True),
                            ('recipe', {}), ('minimums', dict(train=True, tuning=3, calibration=3))]:
            changed = deepcopy(self.plan); changed[key] = value; seal(changed)
            with self.subTest(key=key), self.assertRaises(ValueError): partitions.validate_plan(changed)

    def test_development_rejects_confirmation_and_repeated_examples_even_if_resealed(self):
        result = partitions.prepare(self.bundle, self.plan)
        original = partitions.development_inputs(result, self.bundle)
        changed = deepcopy(original); changed['phases']['confirmation'] = []; seal(changed)
        with self.assertRaises(ValueError): partitions.validate_development(changed)
        changed = deepcopy(original); changed['phases']['train'].append(deepcopy(changed['phases']['train'][0])); seal(changed)
        with self.assertRaises(ValueError): partitions.validate_development(changed)

    def test_self_contained_exclusive_bundle_reloads_without_sources_and_detects_alterations(self):
        result = partitions.prepare(self.bundle, self.plan)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)/'partition'
            partitions.write_partition_bundle(root, self.bundle, self.plan, result)
            with patch.object(api, 'load_matches', side_effect=AssertionError('No current source')):
                loaded = partitions.load_partition_bundle(root)
            self.assertEqual(loaded['partition'], result)
            self.assertTrue(loaded['development']['ready'])
            with self.assertRaises(FileExistsError): partitions.write_partition_bundle(root, self.bundle, self.plan, result)
            (root/'partition.json').write_text('{}')
            with self.assertRaises(ValueError): partitions.load_partition_bundle(root)

    def test_default_gates_refuse_tiny_realistic_development_before_fit(self):
        import causal_candidate as candidates
        plan = partitions.make_plan(windows())
        result = partitions.prepare(self.bundle, plan)
        value = partitions.development_inputs(result, self.bundle)
        with patch.object(candidates, '_fit', side_effect=AssertionError('Not enough data')) as fitting:
            with self.assertRaises(ValueError): candidates.fit_candidate(value)
        fitting.assert_not_called()


if __name__ == '__main__':
    unittest.main()
