"""Pure snapshot parity, causal isolation and tamper detection; no I/O or fit."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from club_identity import fixture_identity
import match_predictor as predictor
import prematch_feature_snapshot as snapshots
import scenario_features as features
from test_scenario_features import match


NOW = datetime(2026, 2, 15, 10, tzinfo=timezone.utc)


def target(**extra):
    value = match(999, timestamp='2026-02-15T16:00:00', season=2026)
    for key in ('ht', 'ft', 'value'):
        value.pop(key)
    value.update(extra)
    return value


def history():
    return [match(i+1, day=i//4, ht=(i%3,0), ft=(i%3+1, i%2), season=2026)
            for i in range(40)]


def source_refs(rows):
    return [dict(match_id=row['match_id'], observation_id=(format(int(row['match_id']), '064x')),
                 observed_at_utc='2026-02-14T20:00:00Z', time_kind=snapshots.TIME_KIND,
                 normalized_sha256=snapshots.digest(row)) for row in rows]


def target_ref():
    return dict(match_id='999', observation_id='a'*64, observed_at_utc='2026-02-15T09:00:00Z',
                time_kind=snapshots.TIME_KIND, raw_sha256='b'*64)


def build(rows=None, fixture=None, **kwargs):
    rows = history() if rows is None else rows
    references = kwargs.pop('source_refs') if 'source_refs' in kwargs else source_refs(rows)
    return snapshots.build(rows, target() if fixture is None else fixture,
                           decision_at=kwargs.pop('decision_at', NOW),
                           source_refs=references,
                           target_ref=kwargs.pop('target_ref', target_ref()), **kwargs)


def reseal(snapshot):
    value = deepcopy(snapshot)
    value.pop('id', None)
    value['id'] = snapshots.digest(value)
    return value


class PrematchFeatureSnapshotTests(unittest.TestCase):
    def test_feature_and_clubs_parity_with_existing_formulas_without_target_scores(self):
        rows, fixture = history(), target()
        snapshot = build(rows, fixture)
        self.assertEqual(snapshot['features'], features.feature_for_fixture(rows, fixture, before='2026-02-15'))
        self.assertEqual(tuple(snapshot['features']), features.FEATURE_COLUMNS)
        estimate = predictor.estimate(rows, '39', fixture['home'], fixture['away'], '2026-02-15', **fixture_identity(fixture))
        self.assertEqual(snapshot['references']['clubs'], estimate['probabilities'])
        self.assertEqual(snapshot['references']['frequency'], estimate['baseline'])
        self.assertTrue(snapshot['references']['personalized'])
        self.assertEqual(snapshot['feature_schema']['class_order'], ['1st', 'Égal', '2nd'])
        self.assertFalse(set(('ht','ft','value','label','match_id')) & set(snapshot['features']))
        self.assertFalse(snapshot['prospective_confirmation'])
        self.assertFalse(snapshot['provider_publication_time_proven'])

    def test_training_style_example_and_fixture_inference_have_exact_feature_parity(self):
        rows = history()
        labelled = match(999, timestamp='2026-02-15T16:00:00', season=2026)
        example = features.build_examples(rows+[labelled], '39', provider='api-football')[-1]
        self.assertEqual(build()['features'], example['features'])

    def test_snapshot_rebuild_is_self_contained_without_storage_network_or_fit(self):
        value = build()
        with patch('api_football.load_matches', side_effect=AssertionError('No source read')), \
                patch('catboost_scenarios._fit', side_effect=AssertionError('No fit')):
            self.assertEqual(snapshots.validate(value), value)

    def test_future_and_same_day_malformed_results_are_excluded_before_inspection(self):
        rows = history()
        original = build(rows)
        future = dict(match(888, timestamp='2026-02-16T12:00:00'), ht=None, ft=object(), value='invalid')
        same_day = dict(match(889, timestamp='2026-02-15T01:00:00'), ht=None, ft=None, value=None)
        injected = rows+[future,same_day]
        self.assertEqual(build(injected, source_refs=source_refs(rows)), original)

    def test_offset_midnight_cannot_smuggle_a_current_utc_day_result_into_history(self):
        rows=history()
        near=match(800,timestamp='2026-02-14T23:30:00-02:00')
        near.update(ht=None, ft=None, value='unreadable')
        self.assertEqual(build(rows+[near],source_refs=source_refs(rows)),build(rows))
        earlier=match(801,timestamp='2026-02-15T00:30:00+02:00')
        refs=source_refs(rows+[earlier]);refs[-1]['observed_at_utc']='2026-02-15T01:00:00Z'
        included=build(rows+[earlier],source_refs=refs)
        self.assertIn('801',[row['match_id'] for row in included['history_rows']])

    def test_target_scores_unknown_fields_started_status_or_bool_ids_are_rejected(self):
        invalid = [target(**{key:None}) for key in ('ht','ft','value','score','goals','actual','label')]
        invalid += [target(match_id=True),target(home_id=True),target(away_id='1'),
                    target(provider='football-data.org'), target(raw_status='FT'),target(status='FINISHED'),
                    target(is_upcoming=False),target(timestamp=NOW.isoformat()),target(season=True)]
        for fixture in invalid:
            with self.subTest(fixture=fixture), self.assertRaises(snapshots.SnapshotError):
                build(fixture=fixture)

    def test_historical_target_id_and_mixed_source_or_league_are_refused(self):
        rows=history()
        for changed in (dict(rows[0],match_id='999'),dict(rows[0],provider='football-data.org'),
                        dict(rows[0],league='61'),dict(rows[0],home_id=True)):
            with self.subTest(changed=changed),self.assertRaises(snapshots.SnapshotError):
                build([changed]+rows[1:])

    def test_reference_must_bind_every_used_row_and_be_strictly_before_decision(self):
        refs=source_refs(history())
        invalid=[refs[:-1]]
        for change in ({'observed_at_utc':NOW.isoformat()}, {'observed_at_utc':(NOW+timedelta(seconds=1)).isoformat()},
                       {'observed_at_utc':'2026-02-14T20:00:00'}, {'normalized_sha256':'c'*64},
                       {'observation_id':'x'*64},{'time_kind':'provider_published'}):
            copied=deepcopy(refs);copied[0].update(change);invalid.append(copied)
        copied=deepcopy(refs);copied[0].pop('observation_id');invalid.append(copied)
        for values in invalid:
            with self.subTest(values=values),self.assertRaises(snapshots.SnapshotError):
                build(source_refs=values)

    def test_target_reference_identity_hash_and_time_are_checked(self):
        for change in ({'match_id':'998'}, {'observed_at_utc':NOW.isoformat()}, {'raw_sha256':'invalid'},
                       {'time_kind':'estimated_from_kickoff'}):
            reference=dict(target_ref(),**change)
            with self.subTest(reference=reference),self.assertRaises(snapshots.SnapshotError):
                build(target_ref=reference)

    def test_historical_final_receipt_cannot_precede_its_own_kickoff(self):
        refs=source_refs(history())
        refs[0]['observed_at_utc']='2025-12-31T00:00:00Z'
        with self.assertRaises(snapshots.SnapshotError):build(source_refs=refs)

    def test_equal_duplicates_are_idempotent_conflicting_rows_and_receipts_are_rejected(self):
        rows=history();refs=source_refs(rows)
        self.assertEqual(build(rows+[deepcopy(rows[0])],source_refs=refs+[deepcopy(refs[0])]),build(rows))
        changed=dict(rows[0],home='Renamed conflicting duplicate')
        with self.assertRaises(snapshots.SnapshotError):
            build(rows+[changed],source_refs=refs)
        with self.assertRaises(snapshots.SnapshotError):
            build(rows,source_refs=refs+[dict(refs[0],observation_id='d'*64)])

    def test_saved_values_and_resealed_features_or_schema_are_not_rewritten(self):
        original=build()
        altered=deepcopy(original);altered['features']['league_count']=0
        with self.assertRaises(snapshots.SnapshotError):
            snapshots.validate(altered)
        for change in ('features','references','feature_schema','prospective_confirmation'):
            altered=deepcopy(original)
            if change=='features':altered[change]['league_count']=0
            elif change=='references':altered[change]['clubs']['1st']=1
            elif change=='feature_schema':altered[change]['columns']=[]
            else:altered[change]=True
            with self.subTest(change=change),self.assertRaises(snapshots.SnapshotError):
                snapshots.validate(reseal(altered))
        self.assertEqual(snapshots.validate(original),original)

    def test_rebuild_preserves_rows_inputs_and_reference_order_is_irrelevant(self):
        rows=history();refs=source_refs(rows);fixture=target();reference=target_ref()
        prior=deepcopy((rows,refs,fixture,reference))
        snapshot=build(rows,fixture,source_refs=refs,target_ref=reference)
        self.assertEqual(snapshot,build(rows[::-1],fixture,source_refs=refs[::-1],target_ref=reference))
        self.assertEqual((rows,refs,fixture,reference),prior)
        snapshot['history_rows'][0]['ht']='tampered'
        self.assertEqual((rows,refs,fixture,reference),prior)

    def test_empty_history_has_explicit_cold_start_and_no_synthetic_references(self):
        snapshot=build([],source_refs=[])
        self.assertEqual(snapshot['history_rows'],[])
        self.assertEqual(snapshot['source_refs'],[])
        self.assertEqual(snapshot['features']['league_count'],0)
        self.assertFalse(snapshot['references']['eligible'])
        self.assertEqual(snapshot['references']['frequency'],{'1st':1/3,'Égal':1/3,'2nd':1/3})

    def test_naive_decision_is_refused_but_normalized_naive_fixture_is_explicit_utc(self):
        with self.assertRaises(snapshots.SnapshotError):
            build(decision_at='2026-02-15T10:00:00')
        self.assertEqual(build(fixture=target(timestamp='2026-02-15T16:00:00'))['fixture']['timestamp'],
                         '2026-02-15T16:00:00')


if __name__=='__main__':
    unittest.main()
