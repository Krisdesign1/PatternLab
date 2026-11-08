"""Whole adaptive sets are frozen once before kickoff and assessed separately."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import adaptive_scenario_proposals as policy
from adaptive_scenario_calibration import VERSION, context_key, digest
from adaptive_scenario_selection import select
import prediction_journal as journal
import prospective_models as models
import scenario_outcomes
from test_prediction_journal import BEFORE, AFTER, raw_fixtures
from test_catboost_scenarios import sample, fixtures


def artifact(methods, size, threshold, now=BEFORE):
    metric=dict(groups=30,hits=15,proposals=90,precision=1/6,f05=18.75/97.5)
    contexts={context_key(name,method,size):dict(status='qualified',threshold=threshold,
        tuning_groups=40,test_groups=30,tuning_days=4,test_days=3,
        tuning=metric,test=metric,reference_top3=metric,
        test_first_day='2025-03-01',tuning_labels_before='2025-03-01T00:00:00+00:00')
        for name,method in methods.items()}
    value=dict(schema=VERSION,created_at=(now-timedelta(hours=1)).isoformat(),contexts=contexts)
    value['sha256']=digest(value)
    return value


class AdaptiveJournalTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name)/'journal.sqlite3'
        self.fixtures=fixtures()[:2]
        with patch.object(models,'_catboost',side_effect=models.SnapshotError('absent')), \
             patch.object(models,'_context_neighbors',side_effect=models.SnapshotError('absent')):
            self.methods=models.build_methods(sample(),self.fixtures,decision_at=BEFORE)['methods']
        # All >=fourth rows, including any ties, are explicit proposals.
        rows=select(self.methods['clubs'],.000001)
        self.threshold=rows[3]['probability']
        self.artifact=artifact(self.methods,2,self.threshold)

    def record(self):
        with patch.object(policy,'is_enabled',return_value=True), \
             patch.object(policy,'load_artifact',return_value=self.artifact), \
             patch.object(policy,'capture',side_effect=policy.legacy_capture):
            return journal.record(self.fixtures,self.methods,now=BEFORE,path=self.path,
                                  raw_fixtures=raw_fixtures(self.fixtures))

    def test_selected_tail_survives_record_export_reconcile_and_reference_metrics_unchanged(self):
        saved=self.record()
        chosen=policy.effective_methods(saved)['clubs']['ranking']
        self.assertGreater(len(chosen),3)
        actual=chosen[3]['sequence']
        self.assertNotIn(actual,[item['sequence'] for item in self.methods['clubs']['ranking']])
        raw=raw_fixtures(self.fixtures,status='FT')
        for item,label in zip(raw,actual):
            ht,ft={'1st':((1,0),(1,0)),'2nd':((0,0),(1,0)),'Égal':((0,0),(0,0))}[label]
            item['score']=dict(halftime=dict(home=ht[0],away=ht[1]),fulltime=dict(home=ft[0],away=ft[1]))
        journal.reconcile(raw,now=AFTER,path=self.path)
        reread=json.loads(json.dumps(journal.list_entries(self.path)[0]))
        self.assertEqual(reread['proposal_selection'],saved['proposal_selection'])
        self.assertFalse(reread['evaluations']['clubs']['top3'])
        self.assertTrue(reread['evaluations']['clubs']['selected_proposals']['hit'])
        self.assertEqual(scenario_outcomes.summarize(reread)['state'],'hit')
        self.assertEqual(reread['methods'],saved['methods'])

    def test_first_snapshot_is_reused_without_today_policy_or_reselection(self):
        first=self.record()
        with patch.object(policy,'capture',side_effect=AssertionError('First snapshot immutable')):
            second=self.record()
        self.assertFalse(second['created'])
        self.assertEqual(second['proposal_selection'],first['proposal_selection'])

    def test_disabled_migration_preserves_legacy_snapshots_without_backfill(self):
        legacy=journal.record(self.fixtures,self.methods,now=BEFORE,path=self.path,
                              raw_fixtures=raw_fixtures(self.fixtures))
        self.assertNotIn('proposal_selection',legacy)
        self.assertNotIn('proposal_selection',self.record())

    def test_mutating_result_or_cached_file_does_not_mutate_calibration(self):
        original=deepcopy(self.artifact)
        envelope=policy.legacy_capture(self.methods,self.fixtures,now=BEFORE,artifact=self.artifact)
        envelope['methods']['clubs']['calibration']['test']['hits']=0
        self.assertEqual(self.artifact,original)
        filename=Path(self.folder.name)/'calibration.json'
        filename.write_text(json.dumps(self.artifact))
        loaded=policy.load_artifact(filename)
        loaded['contexts'].clear()
        self.assertEqual(policy.load_artifact(filename),self.artifact)

    def test_missing_invalid_or_future_calibration_abstains_instead_of_three(self):
        for value in (dict(self.artifact,sha256='bad'),artifact(self.methods,2,self.threshold,now=AFTER)):
            captured=policy.legacy_capture(self.methods,self.fixtures,now=BEFORE,artifact=value)
            self.assertEqual(captured['methods'],{})
            self.assertEqual(set(captured['unavailable']),set(self.methods))

    def test_technical_capacity_error_never_archives_a_prefix(self):
        with patch.object(policy,'MAX_CAPTURE_BYTES',1):
            captured=policy.legacy_capture(self.methods,self.fixtures,now=BEFORE,artifact=self.artifact)
        self.assertEqual(captured['methods'],{})
        self.assertIn('aucun préfixe',captured['unavailable']['clubs'])

    def test_read_validator_rejects_postkickoff_selection_even_with_consistent_digest(self):
        saved=self.record();envelope=saved['proposal_selection']
        envelope['selection_at']=AFTER.isoformat();envelope['sha256']=digest({k:v for k,v in envelope.items() if k!='sha256'})
        with self.assertRaises(ValueError):
            policy.validate(envelope,self.methods,self.fixtures,AFTER.isoformat())


if __name__=='__main__':unittest.main()
