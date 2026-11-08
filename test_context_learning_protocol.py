"""Temporal and immutability checks, without provider, model or production I/O."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

import context_learning_protocol as protocol


NOW=datetime(2026,11,7,12,tzinfo=timezone.utc)
SOURCE_ID='a'*64


def reseal(value):
    value.pop('id',None);value['id']=protocol.digest(value);return value


def evidence():
    source=dict(kind='events',fixture_id='90',observed_at='2026-10-20T14:00:00Z',
        kickoff_utc='2026-10-20T11:00:00Z',provider='api-football',
        provenance='local_import_recorded',quarantined=False,eligible=True)
    return protocol.make_evidence_manifest({SOURCE_ID:source})


def row(phase='train',mid='99',label='1st',refs=None):
    day=dict(train='2026-10-29',tuning='2026-11-02',calibration='2026-11-04',
             descriptive='2026-11-06',confirmation='2026-11-09')[phase]
    e=evidence() if refs is None else refs
    return dict(match_id=mid,decision_at=day+'T11:00:00Z',kickoff_utc=day+'T12:00:00Z',
        target_observed_at=day+'T10:00:00Z',target_status_at_decision='NS',quarantined=False,
        feature_source_ids=list(e['sources']),feature_evidence_id=e['id'],
        features_latest_observed_at='2026-10-20T14:00:00Z',
        features={'score_rate':1.0,'events_missing':0},
        label=label if phase != 'confirmation' else None,
        label_status='FT' if phase != 'confirmation' else None,
        label_observed_at=day+'T15:00:00Z' if phase != 'confirmation' else None)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        clock=patch.object(protocol,'_now',return_value=NOW);clock.start();self.addCleanup(clock.stop)
        self.plan=protocol.make_protocol(minimums={name:3 for name in protocol.PHASES[:-1]})
        self.evidence=evidence()

    def test_fixed_recipe_and_explicit_descriptive_future_contract(self):
        protocol.validate_protocol(self.plan)
        recipe=self.plan['recipe']
        self.assertEqual(recipe['class_order'],['1st','Égal','2nd'])
        self.assertEqual(recipe['heads'],['scores_only','scores_coverage','scores_coverage_goal_reactions'])
        self.assertFalse(recipe['class_balancing']);self.assertFalse(self.plan['automatic_promotion'])
        self.assertEqual(self.plan['confirmation']['minimum_resolved_fixtures'],500)
        self.assertEqual(self.plan['confirmation']['minimum_utc_days'],21)
        self.assertEqual(self.plan['decision_minutes_before_kickoff'],60)

    def test_bad_windows_and_protocol_declaration_cannot_be_backdated(self):
        for mutation in ('overlap','empty','naive','future_development','past_confirmation'):
            with self.subTest(mutation=mutation):
                windows=deepcopy(protocol.DEFAULT_WINDOWS)
                if mutation=='overlap':windows['tuning']['start']='2026-10-31T00:00:00Z'
                if mutation=='empty':windows['train']['end']=windows['train']['start']
                if mutation=='naive':windows['train']['start']='2026-10-27T00:00:00'
                if mutation=='future_development':windows['descriptive']['end']='2026-11-08T00:00:00Z'
                if mutation=='past_confirmation':windows['confirmation']['start']='2026-11-07T00:00:00Z'
                with self.assertRaises(ValueError):protocol.make_protocol(windows)
        for at in (NOW-timedelta(minutes=1),NOW+timedelta(minutes=1)):
            with self.assertRaises(ValueError):protocol.make_protocol(created_at=at)

    def test_protocol_tampering_is_detected_even_after_resealing_critical_policy(self):
        changed=deepcopy(self.plan);changed['minimums']['train']=0;reseal(changed)
        with self.assertRaises(ValueError):protocol.validate_protocol(changed)
        for key,value in [('automatic_promotion',True),('implementation_sha256','0'*64)]:
            changed=deepcopy(self.plan);changed[key]=value;reseal(changed)
            with self.assertRaises(ValueError):protocol.validate_protocol(changed)
        changed=deepcopy(self.plan);changed['confirmation']['confidence_level']=0.5;reseal(changed)
        with self.assertRaises(ValueError):protocol.validate_protocol(changed)
        changed=deepcopy(self.plan);changed['recipe']['candidates'][0]['depth']=10;reseal(changed)
        with self.assertRaises(ValueError):protocol.validate_protocol(changed)

    def test_half_open_phase_boundaries_and_aware_equivalent_instants(self):
        self.assertEqual(protocol.phase_for(self.plan,'2026-11-01T00:00:00Z'),'tuning')
        self.assertEqual(protocol.phase_for(self.plan,'2026-11-01T01:00:00+01:00'),'tuning')
        self.assertIsNone(protocol.phase_for(self.plan,'2026-11-07T12:00:00Z'))
        with self.assertRaises(ValueError):protocol.phase_for(self.plan,'2026-11-01T00:00:00')

    def test_h_minus_60_target_NS_quarantine_and_target_receipt_gates(self):
        self.assertEqual(protocol.row_issues(self.plan,row(),evidence=self.evidence),[])
        cases=[('decision_at','2026-10-29T11:01:00Z','decision_not_H_minus_60'),
               ('target_observed_at','2026-10-29T11:00:00Z','target_not_received_before_decision'),
               ('target_status_at_decision','FT','target_not_NS_at_decision'),
               ('quarantined',True,'target_quality_not_explicitly_accepted')]
        for key,value,expected in cases:
            changed=row();changed[key]=value
            self.assertIn(expected,protocol.row_issues(self.plan,changed,evidence=self.evidence))

    def test_feature_sources_strictly_precede_decision_and_exclude_target(self):
        for field,value,issue in [('observed_at','2026-10-29T11:00:00Z','feature_not_received_before_decision'),
                ('fixture_id','99','target_fixture_used_in_features'),
                ('kickoff_utc','2026-10-29T12:00:00Z','historical_source_chronology_invalid')]:
            sources=deepcopy(self.evidence['sources']);sources[SOURCE_ID][field]=value
            e=protocol.make_evidence_manifest(sources);sample=row(refs=e)
            sample['features_latest_observed_at']=sources[SOURCE_ID]['observed_at']
            self.assertIn(issue,protocol.row_issues(self.plan,sample,evidence=e))

    def test_recovered_quarantined_foreign_or_ineligible_sources_cannot_be_used(self):
        for field,value in [('provider','other'),('provenance','recovered_at_first_tracked_import'),
                            ('quarantined',True),('eligible',False)]:
            sources=deepcopy(self.evidence['sources']);sources[SOURCE_ID][field]=value
            e=protocol.make_evidence_manifest(sources)
            self.assertIn('feature_source_quality_or_provenance_invalid',
                          protocol.row_issues(self.plan,row(refs=e),evidence=e))

    def test_source_manifest_and_latest_receipt_are_bound_to_the_row(self):
        changed=row();changed['feature_evidence_id']='0'*64
        self.assertIn('feature_evidence_mismatch',protocol.row_issues(self.plan,changed,evidence=self.evidence))
        changed=row();changed['features_latest_observed_at']='2026-10-20T13:00:00Z'
        self.assertIn('latest_feature_receipt_mismatch',protocol.row_issues(self.plan,changed,evidence=self.evidence))
        changed=deepcopy(self.evidence);changed['sources'][SOURCE_ID]['observed_at']='2026-10-21T13:00:00Z'
        with self.assertRaises(ValueError):protocol.row_issues(self.plan,row(),evidence=changed)

    def test_missing_event_history_is_retained_explicitly_without_fake_receipt_time(self):
        e=protocol.make_evidence_manifest({});sample=row(refs=e)
        sample['features_latest_observed_at']=None;sample['features']['events_missing']=1
        self.assertEqual(protocol.row_issues(self.plan,sample,evidence=e),[])
        sample['features_latest_observed_at']='2026-10-20T14:00:00Z'
        self.assertIn('latest_feature_receipt_mismatch',protocol.row_issues(self.plan,sample,evidence=e))

    def test_compact_aggregate_requires_scope_digest_bounds_and_target_exclusion(self):
        aggregate=dict(kind='historical_aggregate',provider='api-football',provenance='local_import_recorded',
            quarantined=False,eligible=True,observed_at='2026-10-20T14:00:00Z',
            max_kickoff_utc='2026-10-20T11:00:00Z',source_count=20000,
            source_ids_sha256='b'*64,max_sequence=123,source_store_uuid='a'*32,excluded_fixture_ids=['99'])
        e=protocol.make_evidence_manifest({SOURCE_ID:aggregate});sample=row(refs=e)
        self.assertEqual(protocol.row_issues(self.plan,sample,evidence=e),[])
        aggregate['excluded_fixture_ids']=[];e=protocol.make_evidence_manifest({SOURCE_ID:aggregate})
        self.assertIn('aggregate_source_scope_invalid',protocol.row_issues(self.plan,row(refs=e),evidence=e))
        aggregate['excluded_fixture_ids']=['99'];aggregate['source_store_uuid']='unknown'
        e=protocol.make_evidence_manifest({SOURCE_ID:aggregate})
        self.assertIn('aggregate_source_scope_invalid',protocol.row_issues(self.plan,row(refs=e),evidence=e))

    def test_FT_labels_must_be_known_before_phase_end_not_just_before_fit_now(self):
        for field,value,issue in [('label_status','AET','regulation_FT_label_required'),
                ('label','unknown','regulation_FT_label_required'),
                ('label_observed_at','2026-11-01T00:00:00Z','label_not_available_before_phase_cutoff'),
                ('label_observed_at','2026-10-29T12:00:00Z','label_not_available_before_phase_cutoff')]:
            sample=row();sample[field]=value
            self.assertIn(issue,protocol.row_issues(self.plan,sample,evidence=self.evidence))

    def test_same_unique_fixtures_partition_and_fit_never_receive_descriptive_or_confirmation(self):
        rows=[]
        for phase_index,phase in enumerate(protocol.PHASES[:-1]):
            for class_index,label in enumerate(protocol.VALUES):
                rows.append(row(phase,str(100+phase_index*3+class_index),label))
        future=row('confirmation','200');future['hidden_result']='2nd';rows.append(future)
        partition=protocol.partition_rows(self.plan,rows,evidence=self.evidence)
        self.assertTrue(partition['technical_training_ready'])
        self.assertFalse(partition['historical_evaluation_independent'])
        self.assertEqual(partition['counts'],dict(train=3,tuning=3,calibration=3,descriptive=3,confirmation=1))
        self.assertNotIn('hidden_result',partition['phases']['confirmation'][0])
        self.assertNotIn('label',partition['phases']['confirmation'][0])
        development=protocol.development_rows(self.plan,rows,evidence=self.evidence)
        self.assertEqual(set(development['phases']),set(protocol.FIT_PHASES))
        self.assertFalse(development['confirmation_labels_used'])
        with self.assertRaisesRegex(ValueError,'Duplicate target'):
            protocol.partition_rows(self.plan,[rows[0],deepcopy(rows[0])],evidence=self.evidence)

    def test_confirmation_labels_are_refused_and_small_fit_reports_blockers(self):
        future=row('confirmation','200');future['label']='2nd'
        result=protocol.partition_rows(self.plan,[future,row()],evidence=self.evidence)
        self.assertFalse(result['technical_training_ready'])
        self.assertEqual(result['counts']['confirmation'],0)
        self.assertIn('confirmation_label_supplied',result['exclusions'][0]['reasons'])
        self.assertIn('train:insufficient_examples',result['blockers'])
        self.assertIn('train:missing_classes',result['blockers'])

    def test_candidate_frozen_now_before_future_confirmation_never_auto_promotes(self):
        frozen=protocol.freeze_candidate(self.plan,'a'*64,{'model.cbm':'b'*64})
        self.assertEqual(frozen['protocol_id'],self.plan['id'])
        self.assertFalse(frozen['automatic_promotion']);self.assertFalse(frozen['confirmation_evaluated'])
        with patch.object(protocol,'_now',return_value=datetime(2026,11,8,tzinfo=timezone.utc)):
            with self.assertRaises(ValueError):protocol.freeze_candidate(self.plan,'a'*64,{'model.cbm':'b'*64})
        with self.assertRaises(ValueError):protocol.freeze_candidate(self.plan,'a'*64,{'model.cbm':'b'*64},frozen_at=NOW-timedelta(days=1))


if __name__ == '__main__':
    unittest.main()
