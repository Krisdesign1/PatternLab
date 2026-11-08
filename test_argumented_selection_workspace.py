from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch,Mock

import argumented_selection_workspace as workspace
import argumented_selection_store as store
from test_argumented_selection_engine import NOW,scan,history


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.base=scan();self.past=history()
        self.sig=self.patch(workspace.api,'data_signature',return_value=('indexed',1,'uuid'))
        self.scan=self.patch(workspace.probabilities,'load_scan',side_effect=lambda **kw:deepcopy(self.base))
        self.history=self.patch(workspace.api,'load_matches',side_effect=lambda *a,**kw:(deepcopy(self.past),{}))
        self.network=self.patch(workspace.api.ApiFootballClient,'fixtures',side_effect=AssertionError('Read-only'))
        self.real_capture=store.capture
        self.capture=self.patch(store,'capture',side_effect=AssertionError('Read-only'))
        workspace._build.cache_clear();self.addCleanup(workspace._build.cache_clear)

    def patch(self,obj,name,*a,**kw):
        p=patch.object(obj,name,*a,**kw);v=p.start();self.addCleanup(p.stop);return v

    def load(self,now=NOW):return workspace.load_report(self.root,now=now)

    def test_indexed_read_is_cached_and_does_not_mutate_or_call_provider(self):
        first=self.load();second=self.load()
        self.assertEqual(len(first['rows']),1)
        self.assertEqual(first,second)
        self.scan.assert_called_once();self.history.assert_called_once()
        self.assertEqual(self.history.call_args.kwargs['leagues'],{'61'})
        self.assertEqual(self.history.call_args.kwargs['end'],'2026-10-22')
        self.assertEqual(list(self.root.iterdir()),[])
        self.network.assert_not_called();self.capture.assert_not_called()

    def test_cached_candidate_expires_at_kickoff_without_rebuilding_history(self):
        self.load()
        later=self.load(NOW+timedelta(hours=7))
        self.assertFalse(later['rows'])
        self.assertTrue(any(x['reason']=='kickoff_elapsed' for x in later['excluded']))
        self.history.assert_called_once()

    def test_revision_change_invalidates_cached_recent_proofs(self):
        self.load();self.sig.return_value=('indexed',2,'uuid')
        self.past=[]
        later=self.load()
        self.assertFalse(later['rows'])
        self.assertEqual(self.history.call_count,2)

    def test_unstable_snapshot_is_retried_then_rejected(self):
        self.sig.side_effect=[('db',1),('db',2),('db',2),('db',3),('db',4)]
        with self.assertRaises(ValueError):self.load()
        self.capture.assert_not_called();self.network.assert_not_called()

    def test_disabled_cycle_never_computes_or_spends_requests(self):
        result=workspace.capture_cycle(self.root,clock=lambda:NOW,refresh=self.network)
        self.assertEqual(result['status'],'disabled')
        self.scan.assert_not_called();self.network.assert_not_called()

    def test_enabled_cycle_passes_provider_gate_and_deadline_without_own_api_client(self):
        workspace.enable(self.root)
        self.capture.side_effect=None;self.capture.return_value={'created':1}
        check=Mock();refresh=Mock()
        result=workspace.capture_cycle(self.root,clock=lambda:NOW,refresh=refresh,check_time=check)
        self.assertEqual(result['created'],1)
        self.assertIs(self.capture.call_args.kwargs['refresh'],refresh)
        self.assertIs(self.capture.call_args.kwargs['check_time'],check)
        self.network.assert_not_called()

    def test_service_helper_uses_existing_quota_gate(self):
        import automation_worker as automation
        workspace.enable(self.root)
        gate=Mock();report={}
        with patch.object(workspace,'capture_cycle',return_value={'created':2}) as capture:
            automation._track_argumented_selection(self.root,gate,report,lambda:NOW)
        self.assertEqual(report['argumented_selection']['created'],2)
        self.assertIs(capture.call_args.kwargs['refresh'],gate.call)
        self.assertIs(capture.call_args.kwargs['check_time'],gate.check_time)

    def test_service_pause_remains_resumable(self):
        import automation_worker as automation
        workspace.enable(self.root)
        with patch.object(workspace,'capture_cycle',side_effect=automation._Pause('pause')):
            with self.assertRaises(automation._Pause):
                automation._track_argumented_selection(self.root,Mock(),{},lambda:NOW)

    def test_explicit_action_rejects_historical_day_before_network(self):
        with self.assertRaises(ValueError):workspace.capture_now(self.root,day=NOW.date()-timedelta(days=1000))
        self.network.assert_not_called()

    def test_full_workspace_to_immutable_capture_uses_consistent_clock_and_proofs(self):
        from test_high_probability_engine import fixture,history as raw_history
        import high_probability_engine
        raw=[fixture(100001)]
        historical=raw_history(equal=True)
        self.base=high_probability_engine.scan(historical+raw,NOW)
        self.past=[workspace.api.normalize_fixture(row) for row in historical]
        report=self.load()
        self.assertEqual(report['generated_at'],NOW.isoformat())
        result=self.real_capture(report,self.root,clock=lambda:NOW,refresh=lambda **kwargs:deepcopy(raw))
        self.assertEqual(result['created'],1)
        rows=store.entries(self.root,now=NOW)
        self.assertEqual(rows[0]['fixture']['match_id'],'100001')
        self.assertEqual(rows[0]['recent_home']['matches'],10)
        self.assertEqual(rows[0]['created_at'],NOW.isoformat())
        self.network.assert_not_called()


if __name__=='__main__':unittest.main()
